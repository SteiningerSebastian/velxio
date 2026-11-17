/**
 * The browser host's side of the chip HTTP API (vx_net_*).
 *
 * Only the QEMU worker performs requests (backend/app/services/chip_net.py).
 * Every other host still has to LOAD a chip that imports the group, and has to
 * answer it honestly: vx_net_available() == 0 so the chip models itself
 * offline, and VX_NET_UNAVAILABLE from the calls it then never makes.
 *
 * Two things this pins:
 *
 *  - the imports exist, so `_checkImports` does not reject a chip built
 *    against velxio-chip.h on the canvas (the failure mode would be
 *    "Chip WASM imports missing in host", thrown before instantiation);
 *  - vx_net_available answers 0, not a negative code. A chip tests it as a
 *    boolean, and -6 is truthy in C — a stub returning the code would tell
 *    every chip the canvas had egress and send it looking for a socket.
 *
 * The import table is asked of the real ChipRuntime, so the names and arities
 * checked here are the ones the runtime actually registers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ChipInstance } from '../simulation/customChips/ChipRuntime';
import { resetBusNets } from '../simulation/customChips/busNets';

const VX_NET_UNAVAILABLE = -6;

type HostFns = Record<string, (...args: never[]) => number>;

/**
 * A ChipInstance whose host imports are built but whose WASM never runs.
 *
 * `_buildVelxioImports` only reads the instance fields when a host function is
 * CALLED, and the calls this test makes (the net group) read none of them. So
 * the stand-in carries just enough for the two that capture `this.memory` /
 * `this.wasi` at build time.
 */
function importTable(): HostFns {
  const stub = {
    memory: new WebAssembly.Memory({ initial: 1 }),
    _romBytes: new Uint8Array(0),
    wasi: { writeStdout: () => {} },
    _blob_size: () => 0,
    _blob_read: () => 0,
    _blob_write: () => 0,
    _rom_read: () => {},
  };
  const build = (
    ChipInstance.prototype as unknown as {
      _buildVelxioImports: (this: typeof stub) => HostFns;
    }
  )._buildVelxioImports;
  return build.call(stub);
}

let table: HostFns;

beforeEach(() => {
  resetBusNets();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  table = importTable();
});

afterEach(() => {
  resetBusNets();
  vi.restoreAllMocks();
});

describe('the browser host and vx_net_*', () => {
  it('registers the whole group, so a chip built against the header loads', () => {
    // A missing import throws at instantiation (`_checkImports`), and the chip
    // would simply never attach in the tab.
    for (const name of [
      'vx_net_available',
      'vx_net_request',
      'vx_net_poll',
      'vx_net_step',
      'vx_net_resp_len',
      'vx_net_resp_read',
      'vx_net_status',
      'vx_net_busy',
    ]) {
      expect(typeof table[name]).toBe('function');
    }
  });

  it('answers vx_net_available() with 0, not with a negative code', () => {
    expect(table.vx_net_available()).toBe(0);
  });

  it('answers the request calls with VX_NET_UNAVAILABLE', () => {
    expect(table.vx_net_request()).toBe(VX_NET_UNAVAILABLE);
    expect(table.vx_net_step()).toBe(VX_NET_UNAVAILABLE);
    expect(table.vx_net_poll()).toBe(VX_NET_UNAVAILABLE);
    expect(table.vx_net_status()).toBe(VX_NET_UNAVAILABLE);
  });

  it('reports an empty, idle line for the read side', () => {
    expect(table.vx_net_resp_len()).toBe(0);
    expect(table.vx_net_resp_read()).toBe(0);
    expect(table.vx_net_busy()).toBe(0);
  });

  it('does not claim egress even when asked through the group', () => {
    // The invariant a chip relies on: the boolean gate and the codes agree,
    // so `if (!vx_net_available()) return;` in setup is a complete guard.
    expect(table.vx_net_available()).toBe(0);
    expect(table.vx_net_request()).toBeLessThan(0);
  });
});
