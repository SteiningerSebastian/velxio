"""chip_net — the HTTP egress a Velxio custom chip gets, and the allowlist it obeys.

A custom chip is a WebAssembly module whose only view of the world is the host
functions in `backend/sdk/velxio-chip.h`. Until this module the ABI had no way
out to the network at all: a chip that models a robot arm could read its pins
and answer its buses, and nothing else. A chip that has to ask a physics
server "where is the arm now?" had no way to ask.

This module is that way out, for the hosts that can offer it (the QEMU worker
process — see `WasmChipRuntime._define_velxio`). The browser runtime answers
the same calls with `VX_NET_UNAVAILABLE` (ChipRuntime.ts), so one chip binary
still loads on every host and can say why it cannot run.

Two rules shape the design:

**A chip never blocks.** The chip runs on QEMU's IO thread, inside a bus
callback or a timer. A blocking connect there would stall the guest for the
length of a DNS lookup, and would deadlock outright when the server the chip
wants to talk to is a process this same worker has to keep serving. So a
request is handed to a host thread and the chip collects the reply on a later
call (`vx_net_poll`, or `vx_net_step` which does both). One request in flight
per chip instance.

**A chip cannot reach anywhere it was not granted.** A user-supplied `.wasm`
runs in the backend process of every Velxio deployment; the URL it asks for is
not a value we may simply fetch. `NetPolicy` decides, before a socket opens,
and the default policy is the one this host was built for:

    *.apps.htl-neufelden.at  on 443, https     (the school's simulation servers)
    localhost                on any port       (a simulator running beside Velxio)

Both are overridable or switchable-off per deployment with `VELXIO_CHIP_NET`
and `VELXIO_CHIP_NET_ALLOW`; see `NetPolicy.from_env`.

The allowlist is not the only guard. A name on the list is *resolved* before
the request goes out and every address it resolves to is checked against the
same rule, so an allowed name cannot be pointed at a metadata endpoint or a
private RFC1918 host. Redirects are not followed, because an allowed host
answering `302 http://169.254.169.254/` would otherwise leave the list behind.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

try:  # requirements.txt pins httpx (the IoT gateway and MCP use it too).
    import httpx
except Exception:  # pragma: no cover - a host without httpx answers FAILED
    httpx = None


# ── Result codes ─────────────────────────────────────────────────────────────
# Mirrored in velxio-chip.h (VX_NET_*) and in WasiShim's sibling list for the
# browser stubs. A non-negative return is always a byte count.

VX_NET_BUSY = -1         # a request is still in flight
VX_NET_EMPTY = -2        # nothing to hand back yet
VX_NET_DENIED = -3       # the URL is outside this host's allowlist
VX_NET_BADARG = -4       # malformed URL, method not allowed, bad pointer
VX_NET_FAILED = -5       # DNS / connect / TLS / timeout / truncated-by-peer
VX_NET_UNAVAILABLE = -6  # this host offers no egress to chips at all

#: Methods a chip may use. CONNECT and TRACE are absent on purpose: the first
#: would tunnel anywhere, the second is a reflection primitive, and neither is
#: something a device model needs.
ALLOWED_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})

#: Response body cap, in bytes, when the deployment sets no env override.
DEFAULT_MAX_BYTES = 1 << 20          # 1 MiB
#: Request timeout in seconds when the deployment sets no env override.
DEFAULT_TIMEOUT = 5.0
#: Connect timeout: a name that will not answer must not hold the slot long.
CONNECT_TIMEOUT = 2.0

#: The allowlist this host ships with: the school's simulation servers on 443,
#: and a simulator running beside Velxio on any local port.
DEFAULT_ALLOW_SPEC = (
    '*.apps.htl-neufelden.at:443=https,'
    'localhost:*=http,https,'
    '127.0.0.1:*=http,https,'
    '[::1]:*=http,https'
)


# ── The allowlist ────────────────────────────────────────────────────────────


def _norm_host(host: str) -> str:
    """Lower-case, no trailing dot, no IPv6 brackets."""
    h = (host or '').strip().lower().rstrip('.')
    if h.startswith('[') and h.endswith(']'):
        h = h[1:-1]
    return h


def _host_is_loopback(host: str) -> bool:
    h = _norm_host(host)
    if h in ('localhost', 'localhost.localdomain'):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _addr_is_local(ip) -> bool:
    """An address a request must not reach unless its rule says loopback.

    `is_global` is the clean test, but it is False for a few ranges a chip has
    no business reaching anyway; naming them keeps the intent readable and
    keeps the answer stable across the Python versions the workers run on.
    """
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


@dataclass(frozen=True)
class NetRule:
    """One entry of the allowlist.

    `host` is either an exact name (`localhost`, `127.0.0.1`, `::1`) or a
    wildcard (`*.apps.example.at`), which matches the bare name as well as any
    depth of subdomain — `apps.example.at`, `a.apps.example.at`,
    `a.b.apps.example.at`. `ports` of None means any port. `schemes` is the
    set of URL schemes the entry covers.
    """

    host: str
    ports: Optional[tuple[int, ...]] = None
    schemes: tuple[str, ...] = ('https',)

    def matches_host(self, host: str) -> bool:
        h = _norm_host(host)
        pat = _norm_host(self.host)
        if pat.startswith('*.'):
            base = pat[2:]
            return bool(base) and (h == base or h.endswith('.' + base))
        return h == pat

    def covers(self, scheme: str, port: int, host: str) -> bool:
        if scheme not in self.schemes:
            return False
        if self.ports is not None and port not in self.ports:
            return False
        return self.matches_host(host)

    @property
    def loopback(self) -> bool:
        return _host_is_loopback(self.host)

    def describe(self) -> str:
        ports = 'any port' if self.ports is None else '/'.join(str(p) for p in self.ports)
        return f'{self.host} on {ports} ({",".join(self.schemes)})'


def parse_allow_spec(spec: str) -> list[NetRule]:
    """Parse `VELXIO_CHIP_NET_ALLOW` into rules.

    Grammar, one entry per comma:

        HOST                     any port, https only
        HOST:PORT                that port, https only
        HOST:*                  any port, https only
        HOST:PORT=SCHEME[,SCHEME...]   that port, those schemes

    The scheme list is the one place a comma is not an entry separator, so a
    fragment that is exactly a scheme name continues the entry before it
    (`localhost:*=http,https` is ONE entry).
    `HOST` may be `*.example.com` for a wildcard, or an IPv6 literal in
    brackets (`[::1]:8000=http`). An unparsable entry raises ValueError with
    the entry quoted, so a typo in a deployment is loud instead of silently
    widening (or narrowing) the list.
    """
    entries: list[str] = []
    for raw in (spec or '').replace('\n', ',').split(','):
        piece = raw.strip()
        if not piece:
            continue
        # A bare scheme name after an entry that already has "=" belongs to
        # that entry's scheme list, not to a new entry.
        if entries and '=' in entries[-1] and piece.lower() in ('http', 'https', 'ws', 'wss'):
            entries[-1] = f'{entries[-1]},{piece}'
            continue
        entries.append(piece)
    rules: list[NetRule] = []
    for entry in entries:
        raw = entry
        schemes: tuple[str, ...] = ('https',)
        if '=' in entry:
            entry, _, scheme_spec = entry.partition('=')
            entry = entry.strip()
            schemes = tuple(s.strip().lower() for s in scheme_spec.split(',') if s.strip())
            if not schemes:
                raise ValueError(f'chip-net allowlist entry {raw!r} names no scheme')
            bad = [s for s in schemes if s not in ('http', 'https')]
            if bad:
                raise ValueError(f'chip-net allowlist entry {raw!r} names an unknown scheme {bad[0]!r}')
        ports: Optional[tuple[int, ...]] = None
        host = entry
        if entry.startswith('['):                      # [::1]:8000
            close = entry.find(']')
            if close < 0:
                raise ValueError(f'chip-net allowlist entry {raw!r} has an unclosed "["')
            host = entry[: close + 1]
            rest = entry[close + 1 :]
            if rest.startswith(':'):
                ports = _parse_ports(raw, rest[1:])
        else:
            head, sep, tail = entry.rpartition(':')
            if sep and head:
                ports = _parse_ports(raw, tail)
                host = head
        host = _norm_host(host)
        if not host:
            raise ValueError(f'chip-net allowlist entry {raw!r} names no host')
        rules.append(NetRule(host=host, ports=ports, schemes=schemes))
    return rules


def _parse_ports(raw: str, port_spec: str) -> Optional[tuple[int, ...]]:
    if port_spec.strip() in ('*', ''):
        return None
    ports: list[int] = []
    for piece in port_spec.split('+'):
        try:
            port = int(piece)
        except ValueError:
            raise ValueError(f'chip-net allowlist entry {raw!r} has a bad port {piece!r}') from None
        if not (0 < port < 65536):
            raise ValueError(f'chip-net allowlist entry {raw!r} has an out-of-range port {port}')
        ports.append(port)
    return tuple(ports) or None


@dataclass(frozen=True)
class NetDecision:
    """What the policy says about one URL, and why."""

    allowed: bool
    scheme: str
    host: str
    port: int
    rule: Optional[NetRule] = None
    reason: str = ''


_default_rules: Optional[tuple[NetRule, ...]] = None


def _default_list_text() -> str:
    """The default allowlist, described, parsed once.

    Built lazily and cached: `check` calls this only on a refusal, and a chip
    that retries a refused URL every millisecond must not re-parse the list
    behind each one.
    """
    global _default_rules
    if _default_rules is None:
        _default_rules = tuple(parse_allow_spec(DEFAULT_ALLOW_SPEC))
    return ', '.join(r.describe() for r in _default_rules)


class NetPolicy:
    """The allowlist, plus the address guard that goes with it."""

    def __init__(self, rules: Iterable[NetRule], *, enabled: bool = True) -> None:
        self.rules: tuple[NetRule, ...] = tuple(rules)
        self.enabled = enabled

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> 'NetPolicy':
        """The policy this process runs with.

        `VELXIO_CHIP_NET=off` (or `0` / `false` / `no`) turns chip egress off
        entirely: every call answers VX_NET_UNAVAILABLE, which a chip reports
        instead of hanging. Anything else leaves it on.

        `VELXIO_CHIP_NET_ALLOW` replaces the default list (see
        `parse_allow_spec`). It REPLACES rather than extends: a deployment that
        wants the defaults plus one more names both.
        """
        e = os.environ if env is None else env
        flag = str(e.get('VELXIO_CHIP_NET', '') or '').strip().lower()
        if flag in ('off', '0', 'false', 'no', 'disabled'):
            return cls((), enabled=False)
        spec = e.get('VELXIO_CHIP_NET_ALLOW')
        rules = parse_allow_spec(DEFAULT_ALLOW_SPEC if spec is None else str(spec))
        return cls(rules, enabled=True)

    # -- use ---------------------------------------------------------------

    def check(self, url: str) -> NetDecision:
        """Decide one URL without opening a socket."""
        if not self.enabled:
            return NetDecision(False, '', '', 0, reason='networking is disabled on this host')
        try:
            parts = urlsplit(str(url))
            scheme = (parts.scheme or '').lower()
            host = _norm_host(parts.hostname or '')
            port = parts.port or (443 if scheme == 'https' else 80)
        except ValueError as e:
            return NetDecision(False, '', '', 0, reason=f'unparsable URL ({e})')
        if scheme not in ('http', 'https'):
            return NetDecision(
                False, scheme, host, port,
                reason=f'scheme {scheme or "(none)"!r} is not http or https',
            )
        if not host:
            return NetDecision(False, scheme, host, port, reason='URL names no host')
        for rule in self.rules:
            if not rule.covers(scheme, port, host):
                continue
            if not rule.loopback and _literal_is_local(host):
                return NetDecision(
                    False, scheme, host, port, rule,
                    reason=f'{host} is a local address and {rule.host} is not a loopback entry',
                )
            return NetDecision(True, scheme, host, port, rule)
        wanted = f'{host} on port {port} ({scheme})'
        return NetDecision(
            False, scheme, host, port,
            reason=f'{wanted} is not on this host\'s chip allowlist'
                   f' (VELXIO_CHIP_NET_ALLOW; default: {_default_list_text()})',
        )

    @classmethod
    def from_defaults(cls) -> 'NetPolicy':
        return cls(parse_allow_spec(DEFAULT_ALLOW_SPEC))

    def guard_resolved(self, decision: NetDecision) -> Optional[str]:
        """Resolve the host and refuse if any address leaves the rule's scope.

        Returns None when the addresses are acceptable, else the sentence to
        log. Runs on the requesting thread, before the socket opens: a name
        that resolves to a link-local or RFC1918 address is a name pointed
        somewhere it was not granted.

        A loopback ENTRY grants loopback and nothing else. The test is on the
        resolved ADDRESSES, not on the name, because `localhost` is answered by
        the host's own resolver (a hosts file, a container's DNS) and the name
        alone says nothing about where it points.
        """
        if not decision.allowed:
            return 'not allowed'
        rule = decision.rule
        if rule is None:                       # pragma: no cover - check() sets it
            return None
        loopback_entry = rule.loopback
        addrs = [ip for ip in (_addr_of(f, s) for f, _, _, _, s in _resolve(decision.host, decision.port)) if ip]
        for ip in addrs:
            if loopback_entry:
                if ip.is_loopback:
                    continue
                return (f'{decision.host} resolves to {ip}, but {rule.host} grants loopback only'
                        f' (a simulator on this machine, not the LAN)')
            if _addr_is_local(ip):
                return (f'{decision.host} resolves to the local address {ip}, '
                        f'which {rule.host} does not grant')
        return None


def _literal_is_local(host: str) -> bool:
    try:
        return _addr_is_local(ipaddress.ip_address(host))
    except ValueError:
        return False


def _resolve(host: str, port: int):
    try:
        return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []


def _addr_of(family: int, sockaddr):
    try:
        return ipaddress.ip_address(sockaddr[0])
    except (ValueError, IndexError):
        return None


# ── Transport ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NetResult:
    """What a request produced. `error` empty means the exchange happened."""

    status: int = 0
    body: bytes = b''
    error: str = ''
    truncated: bool = False


#: A fetch callable: the seam the unit tests drive instead of a socket.
FetchFn = Callable[[str, str, str, bytes, float, int], NetResult]


def fetch_url(
    method: str,
    url: str,
    content_type: str,
    body: bytes,
    timeout: float,
    max_bytes: int,
) -> NetResult:
    """Perform one request. Blocking — call it on a host thread, never on the
    thread the guest runs on."""
    if httpx is None:
        return NetResult(error='httpx is not installed in this deployment')
    headers = {'Accept': '*/*', 'User-Agent': 'velxio-custom-chip/1'}
    if content_type:
        headers['Content-Type'] = content_type
    limits = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)
    try:
        # follow_redirects=False is a security decision, not a default: an
        # allowed host that answers 302 to another origin would carry the
        # request off the allowlist. See the module docstring.
        with httpx.Client(timeout=limits, follow_redirects=False) as client:
            with client.stream(method, url, content=body, headers=headers) as resp:
                chunks: list[bytes] = []
                total = 0
                truncated = False
                for chunk in resp.iter_bytes():
                    if total + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - total])
                        total = max_bytes
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return NetResult(status=resp.status_code, body=b''.join(chunks), truncated=truncated)
    except httpx.HTTPError as e:
        return NetResult(error=f'{type(e).__name__}: {e}')
    except Exception as e:  # noqa: BLE001 - a chip must never crash the worker
        return NetResult(error=f'{type(e).__name__}: {e}')


# ── One chip's line ───────────────────────────────────────────────────────────


_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vx-net')
        return _executor


@dataclass
class _Reply:
    status: int
    body: bytes
    truncated: bool


class ChipNet:
    """The HTTP line one chip instance owns: one request in flight, at most.

    The shape — request, then collect on a later call — is what `vx_net_step`
    exposes to C, and it exists because the chip cannot block (module
    docstring). Nothing here touches guest memory: the runtime reads the body
    out of the returned tuple and writes it into the chip's linear memory on
    the thread that called in, which is the only thread allowed to touch it.
    """

    def __init__(
        self,
        policy: Optional[NetPolicy] = None,
        emit: Optional[Callable[[dict], None]] = None,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
        fetch: Optional[FetchFn] = None,
    ) -> None:
        self._policy = policy if policy is not None else default_policy()
        self._emit = emit
        self._timeout = timeout if timeout is not None else _env_float('VELXIO_CHIP_NET_TIMEOUT', DEFAULT_TIMEOUT)
        self._max_bytes = int(max_bytes if max_bytes is not None else _env_int('VELXIO_CHIP_NET_MAX_BYTES', DEFAULT_MAX_BYTES))
        self._fetch: FetchFn = fetch or fetch_url
        self._lock = threading.Lock()
        self._inflight = False
        self._reply: Optional[_Reply] = None
        self._last_body = b''
        self._last_status = VX_NET_EMPTY
        self._last_len = 0
        self._closed = False
        #: Released when a request settles (reply published, or the attempt
        #: failed). `step` waits on this, never on the network itself.
        self._settled = threading.Event()
        #: Reasons already written to the chip log, so a chip that retries a
        #: refused URL every tick does not flood the worker's log.
        self._said: set[str] = set()

    # -- state ------------------------------------------------------------

    @property
    def policy(self) -> NetPolicy:
        return self._policy

    def available(self) -> bool:
        """vx_net_available: does this host offer egress at all?"""
        return bool(self._policy.enabled) and not self._closed

    @property
    def last_status(self) -> int:
        """vx_net_status: the HTTP status of the last reply handed out, or the
        reason the last request was refused / failed."""
        with self._lock:
            return self._last_status

    @property
    def last_len(self) -> int:
        """vx_net_resp_len: bytes in the last reply before truncation."""
        with self._lock:
            return self._last_len

    def resp_body(self) -> bytes:
        """The body the last `poll` handed out (what `vx_net_resp_read` copies
        from). Empty before the first reply.

        Kept here rather than in the runtime so there is one copy of a value
        read from two threads, and it never sees a half-published reply.
        """
        with self._lock:
            return self._last_body

    def busy(self) -> bool:
        with self._lock:
            return self._inflight

    # -- use -------------------------------------------------------------

    def request(self, method: str, url: str, content_type: str, body: bytes) -> int:
        """Dispatch. Non-blocking. See VX_NET_* for the return codes."""
        method = (method or '').strip().upper()
        if method not in ALLOWED_METHODS:
            self._note(VX_NET_BADARG)
            self._say_once(f'method {method or "(none)"!r} is not one a chip may use')
            return VX_NET_BADARG
        if not self._policy.enabled:
            # Not a refusal of this URL: this host offers chips no egress at
            # all, which is what vx_net_available already said. The chip was
            # told in setup; answering DENIED here would blame the destination.
            self._note(VX_NET_UNAVAILABLE)
            return VX_NET_UNAVAILABLE
        decision = self._policy.check(url)
        if not decision.allowed:
            self._note(VX_NET_DENIED)
            self._say_once(decision.reason)
            return VX_NET_DENIED
        with self._lock:
            if self._closed:
                return VX_NET_FAILED
            if self._inflight:
                return VX_NET_BUSY
            # A chip calling request() is done with the previous answer, even
            # if it never polled for it. Dropping it here is what keeps poll()
            # from handing out a STALE reply while this request is in flight.
            self._reply = None
            self._inflight = True
            self._settled.clear()
        try:
            _pool().submit(self._run, method, url, content_type, body, decision)
        except Exception as e:  # noqa: BLE001 - an executor that refuses
            with self._lock:
                self._inflight = False
            self._note(VX_NET_FAILED)
            self._say_once(f'could not start the request: {e!r}')
            return VX_NET_FAILED
        return 0

    def poll(self) -> tuple[int, int, bytes]:
        """Take the reply, if one is waiting.

        Returns `(code, status, body)` where code is the body length, or
        VX_NET_BUSY while a request runs, or VX_NET_EMPTY when nothing has
        arrived. A reply is handed out once; `last_status` / `last_len` keep
        answering afterwards, so a chip can check the code and the size.
        """
        with self._lock:
            reply = self._reply
            if reply is not None:
                self._reply = None
                self._last_body = reply.body
                self._last_len = len(reply.body)
                return len(reply.body), reply.status, reply.body
            if self._inflight:
                return VX_NET_BUSY, self._last_status, b''
            return VX_NET_EMPTY, self._last_status, b''

    def step(
        self,
        method: str,
        url: str,
        content_type: str,
        body: bytes,
        wait_ms: int = 0,
    ) -> tuple[int, int, bytes]:
        """`request` + a bounded wait + `poll`, the shorthand C gets.

        `wait_ms` waits on the CALLER's thread, which for a chip is QEMU's IO
        thread: the guest stands still for as long as this waits. Callers pass
        0 when they will retry on the simulation's own cadence. See
        `vx_net_step` in velxio-chip.h.
        """
        with self._lock:
            already = self._inflight
        if not already:
            started = self.request(method, url, content_type, body)
            if started < 0:
                return started, self.last_status, b''
        if wait_ms > 0:
            deadline = time.monotonic() + (wait_ms / 1000.0)
            while True:
                with self._lock:
                    done = self._reply is not None or not self._inflight
                if done:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Waits OUTSIDE the lock: the host thread has to be able to
                # take it to publish the reply. The timeout bounds the wait,
                # and the re-check above keeps a spurious wake harmless.
                self._settled.wait(remaining)
        return self.poll()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._reply = None
            self._last_body = b''
            # Releases a `step` waiting for a reply that will never land.
            self._settled.set()

    # -- internals -------------------------------------------------------

    def _note(self, status: int) -> None:
        with self._lock:
            self._last_status = status
            self._last_len = 0
            # A refusal or a failure is not "the reply is still the old one":
            # resp_read must not hand back the previous body to a chip that
            # saw a negative code and asked anyway.
            self._last_body = b''

    def _say_once(self, reason: str) -> None:
        if not reason or self._emit is None:
            return
        with self._lock:
            if reason in self._said:
                return
            self._said.add(reason)
        self._emit({'type': 'chip_log', 'text': f'[net] {reason}\n'})

    def _run(self, method: str, url: str, content_type: str, body: bytes, decision: NetDecision) -> None:
        """The host thread: resolve, guard, fetch, publish."""
        reason = ''
        try:
            blocked = self._policy.guard_resolved(decision)
            if blocked:
                with self._lock:
                    self._inflight = False
                    self._last_status = VX_NET_DENIED
                    self._last_len = 0
                    self._last_body = b''
                    self._settled.set()
                self._say_once(blocked)
                return
            result = self._fetch(method, url, content_type, body, self._timeout, self._max_bytes)
        except Exception as e:  # noqa: BLE001 - the chip must never crash the host
            result = NetResult(error=f'{type(e).__name__}: {e}')
        with self._lock:
            self._inflight = False
            if result.error:
                self._reply = None
                self._last_status = VX_NET_FAILED
                self._last_len = 0
                self._last_body = b''
                reason = f'{method} {url} failed: {result.error}'
            else:
                self._reply = _Reply(result.status, result.body, result.truncated)
                self._last_status = result.status
                self._last_len = len(result.body)
            self._settled.set()
        if reason:
            self._say_once(reason)
        if result.truncated and self._emit is not None:
            self._emit({
                'type': 'chip_log',
                'text': f'[net] {method} {url}: reply cut at {len(result.body)} bytes '
                        f'(VELXIO_CHIP_NET_MAX_BYTES)\n',
            })


# ── The process-wide policy ───────────────────────────────────────────────────

_policy_cache: Optional[NetPolicy] = None
_policy_cache_lock = threading.Lock()


def default_policy() -> NetPolicy:
    """The policy this process runs with, read once from the environment."""
    global _policy_cache
    with _policy_cache_lock:
        if _policy_cache is None:
            _policy_cache = NetPolicy.from_env()
        return _policy_cache


def reset_policy_cache() -> None:
    """Forget the cached policy — a test that changes the environment calls
    this so the next `default_policy()` sees the new values."""
    global _policy_cache
    with _policy_cache_lock:
        _policy_cache = None


def _env_float(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, '') or fallback)
    except ValueError:
        return fallback


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, '') or fallback)
    except ValueError:
        return fallback
