"""The chip ABI's HTTP egress, driven through the real WasmChipRuntime.

`test_chip_net.py` covers the policy and the transport. This file is the seam
between them and the guest: a compiled WASM module that imports the seven
`vx_net_*` host functions must instantiate (a missing import throws at
instantiation, not at call time), must read and write the guest's linear
memory correctly, and must answer the codes the header documents.

The module is built from WAT here with wasmtime's own assembler, so no
wasi-sdk or clang is needed: the exports are exactly the calls a C chip makes
and the memory layout is spelled out in the test.

Skipped wholesale when wasmtime is not installed (the runtime is optional in
the OSS requirements).
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip('wasmtime')
wasmtime = pytest.importorskip('wasmtime')

from app.services.chip_net import ChipNet, NetPolicy, NetResult, parse_allow_spec  # noqa: E402
from app.services.wasm_chip_runtime import WasmChipRuntime  # noqa: E402


# ── The guest under test ─────────────────────────────────────────────────────
# Memory 0..63 is scratch. The probe exports one function per ABI call, so each
# test drives exactly the call it names:
#
#   net_available                     -> i32
#   net_request_get(url, off, len)   -> i32   GET <url>, no body
#   net_step_get(url, off, len, wait)-> i32   GET <url>, bounded wait
#   net_poll / net_status / net_busy  -> i32
#   net_resp_len                      -> i32
#   net_resp_read(off, dst, cap)      -> i32
#   net_request_body(url, ctype, body, blen) -> i32  POST with a JSON body
#
# `url`, `ctype` and the POST body live at fixed addresses the test writes.

WAT = r"""
(module
  (import "env" "memory" (memory 2 16))
  (import "env" "vx_net_available" (func $avail (result i32)))
  (import "env" "vx_net_request" (func $req (param i32 i32 i32 i32 i32) (result i32)))
  (import "env" "vx_net_poll" (func $poll (result i32)))
  (import "env" "vx_net_step" (func $step (param i32 i32 i32 i32 i32 i32) (result i32)))
  (import "env" "vx_net_resp_len" (func $rlen (result i32)))
  (import "env" "vx_net_resp_read" (func $rread (param i32 i32 i32) (result i32)))
  (import "env" "vx_net_status" (func $stat (result i32)))
  (import "env" "vx_net_busy" (func $busy (result i32)))

  (data (i32.const 64) "GET\00")
  (data (i32.const 72) "POST\00")
  (data (i32.const 80) "application/json\00")

  (func (export "chip_setup") (result i32) (call $avail))
  (func (export "net_available") (result i32) (call $avail))
  (func (export "net_poll") (result i32) (call $poll))
  (func (export "net_status") (result i32) (call $stat))
  (func (export "net_busy") (result i32) (call $busy))
  (func (export "net_resp_len") (result i32) (call $rlen))

  (func (export "net_request_get") (param $url i32) (param $off i32) (param $len i32) (result i32)
    (call $req (i32.const 64) (local.get $url) (i32.const 0) (local.get $off) (local.get $len)))

  (func (export "net_step_get") (param $url i32) (param $off i32) (param $len i32) (param $wait i32) (result i32)
    (call $step (i32.const 64) (local.get $url) (i32.const 0) (local.get $off) (local.get $len) (local.get $wait)))

  (func (export "net_request_post") (param $url i32) (param $off i32) (param $len i32) (result i32)
    (call $req (i32.const 72) (local.get $url) (i32.const 80) (local.get $off) (local.get $len)))

  (func (export "net_resp_read") (param $off i32) (param $dst i32) (param $cap i32) (result i32)
    (call $rread (local.get $off) (local.get $dst) (local.get $cap)))
)
"""

URL_ADDR = 1024      # the test writes the URL string here
BODY_ADDR = 2048     # … and the request / reply body
REPLY_ADDR = 3072    # … and reads replies here


class Host:
    """A WasmChipRuntime over the probe, with the transport faked."""

    def __init__(self, policy: NetPolicy | None = None, fetch=None, emit=True):
        self.events: list[dict] = []
        self.net = ChipNet(
            policy if policy is not None else NetPolicy(parse_allow_spec('sim.test:443=https')),
            fetch=fetch or (lambda *a, **k: NetResult(status=200, body=b'{"x":1}')),
            timeout=1.0,
            max_bytes=64 * 1024,
            emit=self.events.append if emit else None,
        )
        self.rt = WasmChipRuntime(
            wasmtime.wat2wasm(WAT),
            emit=self.events.append if emit else None,
            net=self.net,
        )
        self.rt.run_chip_setup()

    def write_str(self, addr: int, text: str) -> int:
        self.rt.write_memory(addr, text.encode() + b'\x00')
        return addr

    def write_memory(self, addr: int, data: bytes) -> None:
        self.rt.write_memory(addr, data)

    def read(self, addr: int, length: int) -> bytes:
        return self.rt.read_memory(addr, length)

    def call(self, name: str, *args: int) -> int:
        return self.rt.export_caller(name)(*args)

    def settle(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.net.busy():
                return
            time.sleep(0.005)
        raise AssertionError('the request never settled')


# ── Instantiation ───────────────────────────────────────────────────────────

def test_a_chip_that_imports_the_net_api_instantiates():
    """Seven new imports, and a chip built against them must not die at
    instantiation on a host that has them."""
    h = Host()
    assert h.call('net_available') == 1
    # chip_setup ran and could ask: a chip decides its offline behaviour there.
    assert h.rt._exports['chip_setup'] is not None


def test_a_host_with_the_policy_off_says_so_instead_of_failing():
    h = Host(policy=NetPolicy((), enabled=False))
    assert h.call('net_available') == 0


# ── The request / poll round trip ────────────────────────────────────────────

def test_a_get_round_trip_through_guest_memory():
    seen: list[tuple] = []

    def fetch(method, url, ctype, body, timeout, max_bytes):
        seen.append((method, url, ctype, body))
        return NetResult(status=200, body=b'{"pose":[1,2,3]}')

    h = Host(fetch=fetch)
    uri = h.write_str(URL_ADDR, 'https://sim.test/pose')
    # The body pointer is 0 and the length 0 for a GET.
    assert h.call('net_request_get', uri, 0, 0) == 0
    h.settle()
    assert h.call('net_poll') == len(b'{"pose":[1,2,3]}')
    assert h.call('net_status') == 200
    assert h.call('net_resp_len') == len(b'{"pose":[1,2,3]}')
    n = h.call('net_resp_read', 0, REPLY_ADDR, 64)
    assert h.read(REPLY_ADDR, n) == b'{"pose":[1,2,3]}'
    assert seen == [('GET', 'https://sim.test/pose', '', b'')]


def test_a_post_carries_the_content_type_and_the_body():
    seen: list[tuple] = []

    def fetch(method, url, ctype, body, timeout, max_bytes):
        seen.append((method, url, ctype, body))
        return NetResult(status=204, body=b'')

    h = Host(fetch=fetch)
    uri = h.write_str(URL_ADDR, 'https://sim.test/torque')
    payload = b'{"joint":3,"n":4}'
    h.write_memory(BODY_ADDR, payload)
    assert h.call('net_request_post', uri, BODY_ADDR, len(payload)) == 0
    h.settle()
    assert seen == [('POST', 'https://sim.test/torque', 'application/json', payload)]


def test_a_reply_is_handed_out_once():
    h = Host()
    uri = h.write_str(URL_ADDR, 'https://sim.test/pose')
    h.call('net_request_get', uri, 0, 0)
    h.settle()
    assert h.call('net_poll') == 7          # b'{"x":1}'
    assert h.call('net_poll') == -2          # VX_NET_EMPTY
    # The body is still readable, which is what a chip does after checking len.
    assert h.call('net_resp_len') == 7


def test_a_refused_url_answers_denied_and_logs_the_reason_once():
    h = Host()
    uri = h.write_str(URL_ADDR, 'http://192.168.1.1/x')
    assert h.call('net_request_get', uri, 0, 0) == -3      # VX_NET_DENIED
    assert h.call('net_status') == -3
    # Refused twice, logged once: a chip that retries every tick must not
    # flood the worker's log.
    assert h.call('net_request_get', uri, 0, 0) == -3
    reasons = [e for e in h.events if e.get('type') == 'chip_log' and '[net]' in e.get('text', '')]
    assert len(reasons) == 1


def test_the_transport_failure_reaches_the_guest_as_failed():
    h = Host(fetch=lambda *a, **k: NetResult(error='connect refused'))
    uri = h.write_str(URL_ADDR, 'https://sim.test/pose')
    h.call('net_request_get', uri, 0, 0)
    h.settle()
    assert h.call('net_poll') == -2          # nothing to hand out …
    assert h.call('net_status') == -5        # … and the reason is FAILED
    # And no stale body from an earlier exchange is left readable: a chip that
    # ignores the code and reads anyway gets zero bytes.
    assert h.call('net_resp_len') == 0
    assert h.call('net_resp_read', 0, REPLY_ADDR, 64) == 0


# ── step ──────────────────────────────────────────────────────────────────────

def test_step_waits_and_returns_the_reply():
    h = Host(fetch=lambda *a, **k: NetResult(status=201, body=b'ok'))
    uri = h.write_str(URL_ADDR, 'https://sim.test/x')
    assert h.call('net_step_get', uri, 0, 0, 2000) == 2
    assert h.call('net_status') == 201


def test_step_with_no_wait_reports_busy_and_leaves_the_request_out():
    release = threading.Event()

    def slow(*_a, **_k):
        release.wait(5)
        return NetResult(status=200, body=b'late')

    h = Host(fetch=slow)
    uri = h.write_str(URL_ADDR, 'https://sim.test/x')
    try:
        assert h.call('net_step_get', uri, 0, 0, 0) == -1     # VX_NET_BUSY
        assert h.call('net_busy') == 1
    finally:
        release.set()
    h.settle()
    assert h.call('net_poll') == 4
    assert h.read(REPLY_ADDR, 0) == b''


# ── Memory safety at the edges ───────────────────────────────────────────────

def test_resp_read_past_the_end_copies_nothing():
    h = Host(fetch=lambda *a, **k: NetResult(status=200, body=b'abc'))
    uri = h.write_str(URL_ADDR, 'https://sim.test/x')
    h.call('net_request_get', uri, 0, 0)
    h.settle()
    assert h.call('net_poll') == 3
    assert h.call('net_resp_read', 3, REPLY_ADDR, 16) == 0
    assert h.call('net_resp_read', 99, REPLY_ADDR, 16) == 0
    assert h.call('net_resp_read', 0, REPLY_ADDR, 0) == 0


def test_a_null_url_is_badarg_not_a_crash():
    h = Host()
    assert h.call('net_request_get', 0, 0, 0) == -4        # VX_NET_BADARG


def test_an_empty_body_length_does_not_read_the_guest():
    seen: list[tuple] = []
    h = Host(fetch=lambda m, u, c, b, t, mx: (seen.append((m, c, b)), NetResult(status=200))[1])
    uri = h.write_str(URL_ADDR, 'https://sim.test/x')
    # A body pointer with a zero length is a POST with no body, not a read of
    # whatever is at that address.
    h.call('net_request_post', uri, BODY_ADDR, 0)
    h.settle()
    assert seen == [('POST', 'application/json', b'')]
