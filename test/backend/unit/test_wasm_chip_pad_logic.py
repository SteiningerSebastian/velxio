"""Pad logic rules in the WASM chip runtime (update_pad_logic).

A chip pin with neither a board GPIO nor a chip net (an L293D output wired to
the pad) has no digital channel of its own. A pad logic rule gives it one:
"high (at high_v volts) while every listed GPIO is high". These cases drive
the rule through notify_pin_change and read it back through the chip's own
vx_pin_read / vx_pin_read_analog / vx_pin_wired / vx_pin_watch.

The chip is a hand-written WAT model (no wasi-sdk needed): it registers a
pad `PAD` (no GPIO) and a pin `G` (GPIO 5), watches both on both edges, and
logs every callback as (handle, level) into its memory.

Run from the repo root:
    pytest test/backend/unit/test_wasm_chip_pad_logic.py -v
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'backend'))

wasmtime = pytest.importorskip('wasmtime', reason='chip runtime needs wasmtime')

from app.services.wasm_chip_runtime import WasmChipRuntime  # noqa: E402

LOG_COUNT = 1000
LOG_BASE = 1024

_WAT = r'''
(module
  (import "env" "memory" (memory 2))
  (import "env" "vx_pin_register" (func $reg (param i32 i32) (result i32)))
  (import "env" "vx_pin_watch" (func $watch (param i32 i32 i32 i32)))
  (import "env" "vx_pin_read" (func $read (param i32) (result i32)))
  (import "env" "vx_pin_read_analog" (func $analog (param i32) (result f64)))
  (import "env" "vx_pin_wired" (func $wired (param i32) (result i32)))
  (table (export "__indirect_function_table") 2 funcref)
  (elem (i32.const 1) $on_edge)
  (data (i32.const 16) "PAD\00")
  (data (i32.const 32) "G\00")
  (func (export "chip_setup")
    (local $pad i32) (local $g i32)
    (local.set $pad (call $reg (i32.const 16) (i32.const 0)))
    (local.set $g (call $reg (i32.const 32) (i32.const 0)))
    (call $watch (local.get $pad) (i32.const 3) (i32.const 1) (i32.const 0))
    (call $watch (local.get $g) (i32.const 3) (i32.const 1) (i32.const 0)))
  (func $on_edge (param $ud i32) (param $h i32) (param $v i32)
    (local $n i32) (local $at i32)
    (local.set $n (i32.load (i32.const 1000)))
    (local.set $at (i32.add (i32.const 1024) (i32.mul (local.get $n) (i32.const 8))))
    (i32.store (local.get $at) (local.get $h))
    (i32.store offset=4 (local.get $at) (local.get $v))
    (i32.store (i32.const 1000) (i32.add (local.get $n) (i32.const 1))))
  (func (export "read") (param i32) (result i32) (call $read (local.get 0)))
  (func (export "analog") (param i32) (result f64) (call $analog (local.get 0)))
  (func (export "wired") (param i32) (result i32) (call $wired (local.get 0))))
'''

PAD, G = 0, 1
GPIO_EN, GPIO_IN, GPIO_G = 10, 11, 5
RULE = {'PAD': {'all': [GPIO_EN, GPIO_IN], 'high_v': 5.0, 'low_v': 0.0}}


class _Chip:
    def __init__(self, pad_logic=None, pin_reader=None):
        wasm = wasmtime.wat2wasm(_WAT)
        self.rt = WasmChipRuntime(
            wasm, {}, lambda _p: None,
            pin_map={'G': GPIO_G},
            pin_reader=pin_reader,
            pad_logic=pad_logic,
        )
        self.rt.run_chip_setup()

    def read(self, h):
        return self.rt._exports['read'](self.rt._store, h)

    def analog(self, h):
        return self.rt._exports['analog'](self.rt._store, h)

    def wired(self, h):
        return self.rt._exports['wired'](self.rt._store, h)

    def events(self):
        mem = self.rt._memory
        n = struct.unpack('<I', bytes(mem.read(self.rt._store, LOG_COUNT, LOG_COUNT + 4)))[0]
        raw = bytes(mem.read(self.rt._store, LOG_BASE, LOG_BASE + 8 * n))
        return [struct.unpack_from('<II', raw, 8 * i) for i in range(n)]

    def set(self, gpio, v):
        self.rt.notify_pin_change(gpio, v)


def test_level_and_analog_follow_the_rule():
    c = _Chip(RULE)
    assert (c.read(PAD), c.analog(PAD)) == (0, 0.0)
    c.set(GPIO_EN, 1)
    assert (c.read(PAD), c.analog(PAD)) == (0, 0.0)   # IN still low
    c.set(GPIO_IN, 1)
    assert (c.read(PAD), c.analog(PAD)) == (1, 5.0)
    c.set(GPIO_EN, 0)
    assert (c.read(PAD), c.analog(PAD)) == (0, 0.0)
    assert c.wired(PAD) == 1


def test_pin_reader_is_preferred_over_seen_levels():
    levels = {GPIO_EN: 1, GPIO_IN: 1}
    c = _Chip(RULE, pin_reader=lambda g: levels.get(g))
    assert c.read(PAD) == 1
    levels[GPIO_IN] = 0
    assert c.read(PAD) == 0
    levels.pop(GPIO_IN)               # reader has no answer: falls back to seen
    c.set(GPIO_IN, 0)
    assert c.read(PAD) == 0


def test_watch_fires_rising_and_falling_on_the_right_edges_only():
    c = _Chip(RULE)
    c.set(GPIO_IN, 1)                 # EN low: pad stays low, no edge
    assert c.events() == []
    c.set(GPIO_EN, 1)                 # both high: rising
    assert c.events() == [(PAD, 1)]
    c.set(GPIO_EN, 1)                 # repeated level: nothing
    assert c.events() == [(PAD, 1)]
    c.set(GPIO_IN, 0)                 # falling
    assert c.events() == [(PAD, 1), (PAD, 0)]
    c.set(GPIO_EN, 0)                 # already low: nothing
    c.set(99, 1)                      # unrelated GPIO: nothing
    assert c.events() == [(PAD, 1), (PAD, 0)]


def test_no_rule_reads_zero_and_never_fires():
    c = _Chip()
    assert c.read(PAD) == 0 and c.analog(PAD) == 0.0
    assert c.wired(PAD) == 0
    assert c.rt.has_pin_watches()     # the pad watch is held, waiting for a rule
    for g in (GPIO_EN, GPIO_IN):
        c.set(g, 1)
        c.set(g, 0)
    assert c.events() == []


def test_rule_arriving_after_setup_works():
    c = _Chip()
    c.set(GPIO_EN, 1)                 # seen before the rule exists
    c.rt.update_pad_logic(RULE)
    assert c.read(PAD) == 0
    c.set(GPIO_IN, 1)
    assert c.read(PAD) == 1 and c.analog(PAD) == 5.0
    assert c.events() == [(PAD, 1)]


def test_update_pad_logic_resync_does_not_fire():
    c = _Chip(RULE)
    c.set(GPIO_EN, 1)
    c.set(GPIO_IN, 1)
    assert c.events() == [(PAD, 1)]
    # Rewire so the pad is high right now (new rule: only EN) and low (new
    # rule: an unseen GPIO). Neither change is an edge the chip should see.
    c.rt.update_pad_logic({'PAD': {'all': [GPIO_EN], 'high_v': 3.3, 'low_v': 0.0}})
    c.rt.update_pad_logic({'PAD': {'all': [77], 'high_v': 3.3, 'low_v': 0.0}})
    assert c.events() == [(PAD, 1)]
    assert c.read(PAD) == 0
    c.set(77, 1)                      # a real edge on the new rule does fire
    assert c.events() == [(PAD, 1), (PAD, 1)]


def test_pin_with_a_gpio_ignores_rules():
    rules = dict(RULE)
    rules['G'] = {'all': [GPIO_EN], 'high_v': 5.0, 'low_v': 0.0}
    c = _Chip(rules, pin_reader=lambda g: 1 if g == GPIO_G else None)
    c.set(GPIO_EN, 1)
    assert c.read(G) == 1             # the GPIO's own level, from the reader
    c2 = _Chip(rules, pin_reader=lambda g: 0 if g == GPIO_G else None)
    c2.set(GPIO_EN, 1)
    assert c2.read(G) == 0            # EN high would make the rule say 1
    # Its watch fires from its own GPIO, not from the rule's GPIO.
    assert (G, 1) not in c2.events()
    c2.set(GPIO_G, 1)
    assert (G, 1) in c2.events()
