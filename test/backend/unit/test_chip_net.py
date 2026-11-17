"""Unit tests for the custom-chip HTTP egress (backend/app/services/chip_net.py).

The policy is the part that has to be right: a chip is a user-supplied `.wasm`
running inside every Velxio deployment's backend process, so the URL it asks
for decides whether a socket may open at all. Each case below is a way a
request could leave the allowlist:

  * a host the list does not name
  * a named host on the wrong port, or the wrong scheme
  * a wildcard that does not cover the bare name, or covers a *suffix* lookalike
  * an allowed NAME that resolves to a private / link-local address
  * a scheme that is not http(s)
  * a deployment that switched chip egress off

The transport cases use the `fetch` seam so no socket is opened: they pin the
one-request-in-flight rule, the non-blocking `request`, and the `step` wait.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services import chip_net
from app.services.chip_net import (
    ChipNet,
    NetPolicy,
    NetResult,
    VX_NET_BADARG,
    VX_NET_BUSY,
    VX_NET_DENIED,
    VX_NET_EMPTY,
    VX_NET_FAILED,
    VX_NET_UNAVAILABLE,
    parse_allow_spec,
)


# ── The shipped allowlist ────────────────────────────────────────────────────

def shipped() -> NetPolicy:
    return NetPolicy.from_defaults()


@pytest.mark.parametrize('url', [
    'https://apps.htl-neufelden.at/robot/pose',
    'https://a.apps.htl-neufelden.at:443/x',
    'https://deep.a.apps.htl-neufelden.at/',
    'http://localhost:3000/pose',
    'http://localhost/pose',
    'https://localhost:8443/',
    'http://127.0.0.1:8001/api',
    'http://[::1]:9000/x',
])
def test_the_default_list_allows_the_school_servers_and_a_local_simulator(url):
    assert shipped().check(url).allowed is True


@pytest.mark.parametrize('url', [
    # 443 is the only port granted on the school's hosts, and https the only scheme.
    'http://apps.htl-neufelden.at/x',
    'https://apps.htl-neufelden.at:8443/x',
    'https://apps.htl-neufelden.at:80/x',
    # A host somewhere else.
    'https://example.com/',
    'https://evil.apps.htl-neufelden.at.evil.com/',
    'https://apps.htl-neufelden.at.evil.com/',
    'https://notapps.htl-neufelden.at/',
    # A scheme that is not an HTTP request at all.
    'file:///etc/passwd',
    'ftp://localhost/x',
    # LAN-only names: loopback is granted, RFC1918 is not.
    'http://192.168.4.2/',
    'http://10.0.0.5:3000/',
    'http://0.0.0.0:3000/',
    'http://169.254.169.254/latest/meta-data',
])
def test_the_default_list_refuses_everything_else(url):
    decision = shipped().check(url)
    assert decision.allowed is False
    assert decision.reason


def test_a_lookalike_suffix_does_not_match_the_wildcard():
    # `*.apps.htl-neufelden.at` is a prefix label match, not a substring match.
    assert shipped().check('https://apps.htl-neufelden.at.evil.com/').allowed is False
    assert shipped().check('https://xapps.htl-neufelden.at/').allowed is False


# ── Parsing the override ─────────────────────────────────────────────────────

def test_parse_reads_ports_schemes_and_wildcards():
    rules = parse_allow_spec(
        '*.apps.example.at:443=https,'
        'localhost:*=http,https,'
        '[::1]:8000=http,'
        'plain.example:8080'
    )
    assert len(rules) == 4
    wild, local, v6, plain = rules
    assert wild.host == '*.apps.example.at' and wild.ports == (443,) and wild.schemes == ('https',)
    assert local.host == 'localhost' and local.ports is None
    assert v6.host == '::1' and v6.ports == (8000,) and v6.schemes == ('http',)
    assert plain.host == 'plain.example' and plain.ports == (8080,) and plain.schemes == ('https',)


def test_parse_rejects_a_malformed_entry_loudly():
    with pytest.raises(ValueError):
        parse_allow_spec('localhost:not-a-port')
    with pytest.raises(ValueError):
        parse_allow_spec('localhost:99999')
    with pytest.raises(ValueError):
        parse_allow_spec('localhost:80=ftp')


def test_env_off_disables_egress_entirely():
    policy = NetPolicy.from_env({'VELXIO_CHIP_NET': 'off'})
    assert policy.enabled is False
    assert policy.check('https://apps.htl-neufelden.at/').allowed is False
    assert policy.check('http://localhost:3000/').allowed is False


def test_env_allow_replaces_the_default_list():
    policy = NetPolicy.from_env({'VELXIO_CHIP_NET_ALLOW': 'example.test:443=https'})
    assert policy.check('https://example.test/').allowed is True
    # Replaced, not extended.
    assert policy.check('http://localhost:3000/').allowed is False


def test_default_policy_cache_is_resettable(monkeypatch):
    chip_net.reset_policy_cache()
    first = chip_net.default_policy()
    assert chip_net.default_policy() is first
    monkeypatch.setenv('VELXIO_CHIP_NET', 'off')
    chip_net.reset_policy_cache()
    try:
        assert chip_net.default_policy().enabled is False
    finally:
        chip_net.reset_policy_cache()


# ── The address guard ────────────────────────────────────────────────────────

def test_an_allowed_name_that_resolves_private_is_refused(monkeypatch):
    """A name on the list must not be able to point at the LAN.

    `docs.example.test` is granted on 443, but if it resolves to an RFC1918
    address the request is a request to the LAN wearing an allowed name.
    """
    policy = NetPolicy(parse_allow_spec('docs.example.test:443=https'))
    decision = policy.check('https://docs.example.test/x')
    assert decision.allowed is True

    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, '', ('10.1.2.3', port))]

    monkeypatch.setattr(chip_net.socket, 'getaddrinfo', fake_getaddrinfo)
    assert policy.guard_resolved(decision) is not None


def test_an_allowed_name_that_resolves_public_passes(monkeypatch):
    policy = NetPolicy(parse_allow_spec('docs.example.test:443=https'))
    decision = policy.check('https://docs.example.test/x')

    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, '', ('93.184.216.34', port))]

    monkeypatch.setattr(chip_net.socket, 'getaddrinfo', fake_getaddrinfo)
    assert policy.guard_resolved(decision) is None


def test_a_loopback_entry_still_refuses_a_lan_address(monkeypatch):
    """`localhost` grants loopback, not every local range.

    A deployment that maps `localhost` to 192.168.4.2 has not granted the LAN.
    """
    policy = NetPolicy.from_defaults()
    decision = policy.check('http://localhost:3000/x')
    assert decision.allowed is True

    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, '', ('192.168.4.2', port))]

    monkeypatch.setattr(chip_net.socket, 'getaddrinfo', fake_getaddrinfo)
    assert policy.guard_resolved(decision) is not None


# ── The transport seam ───────────────────────────────────────────────────────

def _policy() -> NetPolicy:
    return NetPolicy(parse_allow_spec('sim.test:443=https'))


def test_request_is_dispatched_and_polled_once():
    seen: list[tuple] = []
    release = threading.Event()

    def fake_fetch(method, url, ctype, body, timeout, max_bytes):
        seen.append((method, url, ctype, body, timeout, max_bytes))
        release.wait(2)
        return NetResult(status=200, body=b'{"x":1}')

    net = ChipNet(_policy(), fetch=fake_fetch, timeout=1.0, max_bytes=4096)
    assert net.available() is True
    assert net.request('GET', 'https://sim.test/pose', '', b'') == 0
    # A second request while one is out is refused, not queued.
    assert net.request('GET', 'https://sim.test/pose', '', b'') == VX_NET_BUSY
    assert net.poll()[0] == VX_NET_BUSY
    assert net.busy() is True
    release.set()
    deadline = time.monotonic() + 2
    code = VX_NET_BUSY
    while time.monotonic() < deadline:
        code, status, body = net.poll()
        if code >= 0:
            break
        time.sleep(0.01)
    assert (code, status, body) == (7, 200, b'{"x":1}')
    # Handed out once.
    assert net.poll()[0] == VX_NET_EMPTY
    assert net.last_status == 200
    assert seen == [('GET', 'https://sim.test/pose', '', b'', 1.0, 4096)]


def test_a_refused_url_never_reaches_the_transport():
    def explode(*_a, **_k):
        raise AssertionError('the transport must not run for a refused URL')

    net = ChipNet(_policy(), fetch=explode)
    assert net.request('GET', 'https://elsewhere.test/x', '', b'') == VX_NET_DENIED
    assert net.last_status == VX_NET_DENIED


@pytest.mark.parametrize('method', ['CONNECT', 'TRACE', 'BREW', ''])
def test_a_method_a_chip_may_not_use_is_refused_before_the_policy(method):
    net = ChipNet(_policy(), fetch=lambda *a, **k: NetResult(status=200))
    assert net.request(method, 'https://sim.test/x', '', b'') == VX_NET_BADARG


def test_a_failed_fetch_reports_failed_and_clears_the_slot():
    net = ChipNet(_policy(), fetch=lambda *a, **k: NetResult(error='connect refused'))
    net.request('GET', 'https://sim.test/x', '', b'')
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and net.busy():
        time.sleep(0.005)
    assert net.poll()[0] == VX_NET_EMPTY
    assert net.last_status == VX_NET_FAILED
    # And the chip may try again.
    assert net.request('GET', 'https://sim.test/x', '', b'') == 0


def test_a_new_request_drops_an_uncollected_reply():
    """A chip that never polls must not be handed the OLD answer mid-flight.

    `poll` answers BUSY while a request is out, so keeping the previous reply
    around would make a caller that ignores BUSY read stale data as if it were
    the new reply.
    """
    release = threading.Event()
    calls = {'n': 0}

    def fetch(*_a, **_k):
        calls['n'] += 1
        if calls['n'] == 1:
            return NetResult(status=200, body=b'first')
        release.wait(2)
        return NetResult(status=200, body=b'second')

    net = ChipNet(_policy(), fetch=fetch)
    net.request('GET', 'https://sim.test/x', '', b'')
    while not net.busy():
        time.sleep(0.001)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and net.poll()[0] < 0:
        time.sleep(0.005)
    # 'first' was waiting but never taken; start the second request.
    assert net.request('GET', 'https://sim.test/x', '', b'') == 0
    assert net.poll()[0] == VX_NET_BUSY
    release.set()
    deadline = time.monotonic() + 2
    code, status, body = -1, 0, b''
    while time.monotonic() < deadline:
        code, status, body = net.poll()
        if code >= 0:
            break
        time.sleep(0.005)
    assert (status, body) == (200, b'second')


def test_step_waits_only_as_long_as_it_is_told_to():
    release = threading.Event()

    def slow(*_a, **_k):
        release.wait(5)
        return NetResult(status=204)

    net = ChipNet(_policy(), fetch=slow)
    started = time.monotonic()
    code, _status, _body = net.step('GET', 'https://sim.test/x', '', b'', wait_ms=60)
    elapsed = time.monotonic() - started
    assert code == VX_NET_BUSY
    assert 0.04 <= elapsed < 1.0
    release.set()


def test_step_returns_the_reply_when_it_lands_inside_the_wait():
    net = ChipNet(_policy(), fetch=lambda *a, **k: NetResult(status=201, body=b'ok'))
    code, status, body = net.step('POST', 'https://sim.test/x', 'application/json',
                                  b'{"a":1}', wait_ms=2000)
    assert (code, status, body) == (2, 201, b'ok')


def test_step_zero_wait_collects_reply_on_the_next_call_instead_of_dropping_it():
    """The net-bridge.c idiom: `vx_net_step(..., wait_ms=0)` dispatches on the
    first tick and returns BUSY; once the worker thread publishes the reply,
    the next `vx_net_step(..., wait_ms=0)` call must hand back that reply
    rather than starting a new request and wiping `_reply`."""
    release = threading.Event()
    calls = {'n': 0}

    def fetch(*_a, **_k):
        calls['n'] += 1
        release.wait(2)
        return NetResult(status=200, body=f'reply-{calls["n"]}'.encode())

    net = ChipNet(_policy(), fetch=fetch)
    assert net.step('GET', 'https://sim.test/x', '', b'', wait_ms=0)[0] == VX_NET_BUSY
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and net.busy():
        time.sleep(0.005)
    # The request settled between ticks; the next zero-wait step collects it.
    code, status, body = net.step('GET', 'https://sim.test/x', '', b'', wait_ms=0)
    assert (code, status, body) == (7, 200, b'reply-1')
    assert calls['n'] == 1


def test_an_explicit_private_ip_rule_grants_that_ip_only():
    policy = NetPolicy(parse_allow_spec('172.17.0.1:3000=http'))
    decision = policy.check('http://172.17.0.1:3000/pose')
    assert decision.allowed is True
    assert policy.guard_resolved(decision) is None
    assert policy.check('http://172.17.0.2:3000/pose').allowed is False
    # Link-local metadata is refused even if named explicitly.
    meta = NetPolicy(parse_allow_spec('169.254.169.254:80=http'))
    assert meta.check('http://169.254.169.254/latest/meta-data').allowed is False


def test_a_disabled_policy_answers_unavailable():
    net = ChipNet(NetPolicy((), enabled=False), fetch=lambda *a, **k: NetResult(status=200))
    assert net.available() is False
    # UNAVAILABLE, not DENIED: the host has no egress, so the URL is not what
    # was refused.
    assert net.request('GET', 'https://sim.test/x', '', b'') == VX_NET_UNAVAILABLE
    assert net.last_status == VX_NET_UNAVAILABLE
    # And the chip was told in setup, so the log stays quiet.
    assert net.poll()[0] == VX_NET_EMPTY


# ── The real transport, on loopback only ─────────────────────────────────────
# A local HTTP server, the real fetch_url, the real policy. No external host is
# contacted: the policy grants loopback and the server binds 127.0.0.1 on a
# port the OS picks. This is the one test that would catch httpx's API moving
# under us, or the allowlist refusing the host it is supposed to grant.

@pytest.fixture()
def local_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[tuple] = []
    routes = {
        '/pose': (200, b'{"pose":[1,2,3]}', 'application/json'),
        '/cmd': (201, b'ok', 'text/plain'),
        '/redirect': (302, b'', 'text/plain'),
        '/big': (200, b'x' * 8192, 'application/octet-stream'),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0]
            seen.append(('GET', self.path))
            if path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'http://169.254.169.254/latest/meta-data')
                self.end_headers()
                return
            status, body, ctype = routes.get(path, (404, b'nope', 'text/plain'))
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            data = self.rfile.read(length)
            seen.append(('POST', self.path, self.headers.get('Content-Type'), data))
            self.send_response(201)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *_a):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_a_real_get_and_post_over_loopback(local_server):
    port, seen = local_server
    policy = NetPolicy(parse_allow_spec('localhost:*=http,127.0.0.1:*=http'))
    net = ChipNet(policy, timeout=5.0, max_bytes=64 * 1024)

    code, status, body = net.step(
        'GET', f'http://localhost:{port}/pose?joint=1', '', b'', wait_ms=5000)
    assert (code, status, body) == (len(b'{"pose":[1,2,3]}'), 200, b'{"pose":[1,2,3]}')

    payload = b'{"joint":3,"n":0.4}'
    code, status, body = net.step(
        'POST', f'http://127.0.0.1:{port}/cmd', 'application/json', payload, wait_ms=5000)
    assert (code, status, body) == (2, 201, b'ok')
    assert ('POST', '/cmd', 'application/json', payload) in seen


def test_a_redirect_is_not_followed(local_server):
    """An allowed host answering 302 to a metadata endpoint must not carry the
    request off the allowlist. The 302 itself comes back; the Location is not
    visited."""
    port, seen = local_server
    policy = NetPolicy(parse_allow_spec('127.0.0.1:*=http'))
    net = ChipNet(policy, timeout=5.0)
    code, status, _body = net.step(
        'GET', f'http://127.0.0.1:{port}/redirect', '', b'', wait_ms=5000)
    assert (code, status) == (0, 302)
    # Only the one request reached the server.
    assert seen == [('GET', '/redirect')]


def test_a_reply_over_the_cap_is_cut_not_failed(local_server):
    port, _seen = local_server
    policy = NetPolicy(parse_allow_spec('127.0.0.1:*=http'))
    net = ChipNet(policy, timeout=5.0, max_bytes=100)
    code, status, body = net.step(
        'GET', f'http://127.0.0.1:{port}/big', '', b'', wait_ms=5000)
    assert status == 200
    assert code == 100 and len(body) == 100


def test_a_closed_port_reports_failed_not_a_hang():
    # Port 1 on loopback: nothing listens, and the policy grants it, so the
    # failure comes from the transport. A refused connect must reach the chip
    # as FAILED and must not hold the slot.
    policy = NetPolicy(parse_allow_spec('127.0.0.1:*=http'))
    net = ChipNet(policy, timeout=2.0)
    started = time.monotonic()
    code, _status, _body = net.step('GET', 'http://127.0.0.1:1/x', '', b'', wait_ms=4000)
    assert code in (VX_NET_BUSY, VX_NET_EMPTY)
    assert time.monotonic() - started < 4.0
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and net.busy():
        time.sleep(0.01)
    assert net.busy() is False
    assert net.last_status == VX_NET_FAILED


# ── The browser relay (requests to a simulator on the student's machine) ─────
#
# The worker runs on the server / in a container, so `localhost` there is not
# the student's PC. Loopback URLs are performed by the tab instead: the relay
# emits a `chip_http_request` event and the tab answers `chip_http_response`.


class _FakeTab:
    """Stands in for the browser: records events, answers on a thread."""

    def __init__(self, answer=None, delay=0.0):
        self.events: list[dict] = []
        self.relay: chip_net.BrowserRelay | None = None
        self._answer = answer
        self._delay = delay

    def send(self, event: dict) -> None:
        self.events.append(event)
        if self._answer is None:
            return
        def reply():
            time.sleep(self._delay)
            assert self.relay is not None
            self.relay.resolve({'id': event['id'], **self._answer(event)})
        threading.Thread(target=reply, daemon=True).start()


def _relay_chip(policy: NetPolicy, tab: _FakeTab, server_fetch=None) -> ChipNet:
    tab.relay = chip_net.BrowserRelay(tab.send)
    return ChipNet(policy, timeout=1.0, relay=tab.relay,
                   fetch=server_fetch or (lambda *a: pytest.fail('server path used')))


def _wait_reply(net: ChipNet, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, status, body = net.poll()
        if code != VX_NET_BUSY:
            return code, status, body
        time.sleep(0.01)
    pytest.fail('no reply')


def test_a_loopback_url_is_performed_by_the_browser():
    import base64
    tab = _FakeTab(answer=lambda ev: {
        'status': 201, 'body_b64': base64.b64encode(b'{"ok":1}').decode()})
    net = _relay_chip(shipped(), tab)
    assert net.request('POST', 'http://localhost:3000/pose', 'application/json', b'{"j":1}') == 0
    code, status, body = _wait_reply(net)
    assert (code, status, body) == (8, 201, b'{"ok":1}')
    ev = tab.events[0]
    assert ev['type'] == 'chip_http_request'
    assert ev['method'] == 'POST' and ev['url'] == 'http://localhost:3000/pose'
    assert ev['content_type'] == 'application/json'
    assert base64.b64decode(ev['body_b64']) == b'{"j":1}'
    assert ev['timeout_ms'] == 1000


def test_auto_route_keeps_non_loopback_urls_on_the_server(monkeypatch):
    monkeypatch.setattr(chip_net, '_resolve', lambda h, p: [(2, 1, 6, '', ('93.184.216.34', p))])
    tab = _FakeTab()
    net = _relay_chip(shipped(), tab, server_fetch=lambda *a: NetResult(status=200, body=b'srv'))
    assert net.request('GET', 'https://a.apps.htl-neufelden.at/x', '', b'') == 0
    assert _wait_reply(net)[2] == b'srv'
    assert tab.events == []


def test_route_server_never_uses_the_browser():
    tab = _FakeTab()
    policy = NetPolicy(parse_allow_spec('localhost:*=http'), route='server')
    net = _relay_chip(policy, tab, server_fetch=lambda *a: NetResult(status=200, body=b'srv'))
    assert net.request('GET', 'http://localhost:3000/', '', b'') == 0
    assert _wait_reply(net)[2] == b'srv'
    assert tab.events == []


def test_route_browser_sends_every_allowed_url_to_the_browser():
    tab = _FakeTab(answer=lambda ev: {'status': 200, 'body_b64': ''})
    policy = NetPolicy(parse_allow_spec('*.apps.htl-neufelden.at:443=https'), route='browser')
    net = _relay_chip(policy, tab)
    assert net.request('GET', 'https://a.apps.htl-neufelden.at/x', '', b'') == 0
    assert _wait_reply(net)[:2] == (0, 200)
    assert len(tab.events) == 1


def test_the_allowlist_still_applies_before_the_browser_is_asked():
    tab = _FakeTab()
    net = _relay_chip(shipped(), tab)
    assert net.request('GET', 'http://evil.example.com/', '', b'') == VX_NET_DENIED
    assert tab.events == []


def test_a_browser_error_reports_failed():
    tab = _FakeTab(answer=lambda ev: {'status': 0, 'error': 'TypeError: Failed to fetch'})
    net = _relay_chip(shipped(), tab)
    net.request('GET', 'http://localhost:3000/', '', b'')
    assert _wait_reply(net)[0] == VX_NET_EMPTY
    assert net.last_status == VX_NET_FAILED


def test_a_tab_that_never_answers_costs_one_timeout_then_failed(monkeypatch):
    monkeypatch.setattr(chip_net.BrowserRelay, 'ROUND_TRIP_SLACK', 0.0)
    tab = _FakeTab()   # never answers
    tab.relay = chip_net.BrowserRelay(tab.send)
    net = ChipNet(shipped(), timeout=0.2, relay=tab.relay)
    net.request('GET', 'http://localhost:3000/', '', b'')
    assert _wait_reply(net)[0] == VX_NET_EMPTY
    assert net.last_status == VX_NET_FAILED
    assert not net.busy()


def test_closing_the_relay_releases_a_waiting_request():
    tab = _FakeTab()
    net = _relay_chip(shipped(), tab)
    net.request('GET', 'http://localhost:3000/', '', b'')
    time.sleep(0.05)
    t0 = time.monotonic()
    assert tab.relay is not None
    tab.relay.close()
    _wait_reply(net)
    assert time.monotonic() - t0 < 0.5
    assert net.last_status == VX_NET_FAILED


def test_a_late_or_unknown_reply_is_ignored():
    relay = chip_net.BrowserRelay(lambda ev: None)
    assert relay.resolve({'id': 999, 'status': 200, 'body_b64': ''}) is False
    assert relay.resolve({'id': 'x'}) is False


def test_env_route_is_read_and_validated():
    assert NetPolicy.from_env({}).route == 'auto'
    assert NetPolicy.from_env({'VELXIO_CHIP_NET_ROUTE': 'server'}).route == 'server'
    with pytest.raises(ValueError):
        NetPolicy.from_env({'VELXIO_CHIP_NET_ROUTE': 'sideways'})
