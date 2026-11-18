/**
 * Logic rules for chip pads driven by a logic part from board GPIOs.
 *
 * A chip hosted in a QEMU worker learns a pad's voltage only from the tab's
 * solve (`pad_volts`), and the solve runs in wall-clock windows. A pad behind
 * an L293D whose inputs a firmware software-PWMs therefore reads sporadic
 * samples of the PWM, and two motors on the same duty read different speeds.
 * Its digital level (vx_pin_read) was 0 outright: the pad has neither a GPIO
 * nor a chip net, the only two digital channels the worker had.
 *
 * The L293D is memoryless logic (componentToSpice: OUT = u(EN)·u(IN)·VCC2),
 * so the worker can evaluate it itself: this module tells it, per pad,
 * "high (at VCC2) while GPIO a AND GPIO b are high". The worker evaluates the
 * rule on the guest's own GPIO edges (wasm_chip_runtime update_pad_logic), so
 * the pad's level, analog read and watches are exact on the guest clock.
 *
 * Only rules that are fully decidable from board GPIOs are emitted: an input
 * held by anything else (a switch, another chip) leaves the pad to the solve.
 */
import { useElectricalStore } from '../../store/useElectricalStore';
import { useSimulatorStore } from '../../store/useSimulatorStore';
import { isBoardComponent, boardPinToNumber } from '../../utils/boardPinMapping';

export interface PadLogicRule {
  /** The pad is high while every one of these board GPIOs is high. */
  all: number[];
  /** Volts on the pad while high / low. */
  high_v: number;
  low_v: number;
}

export type PadLogicTable = Record<string, PadLogicRule | null>;

/** Output pin -> the enable and input pins that gate it, per logic part. */
const DRIVER_GATES: Record<string, Record<string, { en: string; input: string }>> = {
  'motor-driver-l293d': {
    OUT1: { en: 'EN1', input: 'IN1' },
    OUT2: { en: 'EN1', input: 'IN2' },
    OUT3: { en: 'EN2', input: 'IN3' },
    OUT4: { en: 'EN2', input: 'IN4' },
  },
};
const DRIVER_SUPPLY: Record<string, string[]> = {
  'motor-driver-l293d': ['VCC2', '+VS', 'VS'],
};

function splitKey(key: string): { componentId: string; pin: string } {
  const i = key.lastIndexOf(':');
  return { componentId: key.slice(0, i), pin: key.slice(i + 1) };
}

/** Round so a rail that wobbles in the last digits does not republish. */
function volts(v: number): number {
  return Math.round(v * 100) / 100;
}

/**
 * The pad logic table for one chip: every chip pin gets a rule or null, so a
 * removed wire reaches the worker as an unwiring and not as silence.
 */
export function padLogicFor(componentId: string, pins: Iterable<string>): PadLogicTable {
  const { pinNetMap, nodeVoltages } = useElectricalStore.getState();
  const { components, boards } = useSimulatorStore.getState() as {
    components: Array<{ id: string; metadataId: string }>;
    boards: Array<{ id: string; boardKind: string }>;
  };
  const kindOf = new Map(components.map((c) => [c.id, c.metadataId]));
  const boardKind = new Map(boards.map((b) => [b.id, b.boardKind as string]));

  // net -> members
  const members = new Map<string, Array<{ componentId: string; pin: string }>>();
  for (const [key, net] of pinNetMap) {
    let list = members.get(net);
    if (!list) members.set(net, (list = []));
    list.push(splitKey(key));
  }

  const gpioOnNet = (net: string | undefined): number | null => {
    if (!net) return null;
    for (const m of members.get(net) ?? []) {
      const kind = boardKind.get(m.componentId) ?? (isBoardComponent(m.componentId) ? m.componentId : null);
      if (!kind) continue;
      const n = boardPinToNumber(kind, m.pin);
      if (n !== null && n >= 0) return n;
    }
    return null;
  };
  const netOf = (cid: string, pin: string) => pinNetMap.get(`${cid}:${pin}`);
  const solved = (net: string | undefined): number | null => {
    if (!net) return null;
    if (net === '0') return 0;
    const v = nodeVoltages[net];
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  };

  const out: PadLogicTable = {};
  for (const pad of pins) {
    if (!pad) continue;
    out[pad] = null;
    const net = netOf(componentId, pad);
    if (!net) continue;
    // Exactly one logic-part output may drive the net, and no board pin:
    // a GPIO on the net already gives the pad a real level.
    if (gpioOnNet(net) !== null) continue;
    const drivers = (members.get(net) ?? []).filter((m) => {
      const kind = kindOf.get(m.componentId);
      return kind !== undefined && DRIVER_GATES[kind]?.[m.pin] !== undefined;
    });
    if (drivers.length !== 1) continue;
    const drv = drivers[0];
    const kind = kindOf.get(drv.componentId)!;
    const gate = DRIVER_GATES[kind][drv.pin];

    const all: number[] = [];
    let decidable = true;
    for (const pin of [gate.en, gate.input]) {
      const gNet = netOf(drv.componentId, pin);
      const gpio = gpioOnNet(gNet);
      if (gpio !== null) {
        all.push(gpio);
        continue;
      }
      // Not a GPIO: a rail the solve holds high is a constant true term;
      // anything else (unwired, a switch, another chip) is not ours to decide.
      const v = solved(gNet);
      // (A chip on that net could still move it, so only a net with no
      // custom chip on it counts as a fixed rail.)
      const rail = nodeVoltages['vcc_rail'];
      const threshold = (typeof rail === 'number' && Number.isFinite(rail) ? rail : 3.3) / 2;
      const chipOnNet = (members.get(gNet ?? '') ?? []).some((m) => kindOf.get(m.componentId) === 'custom-chip');
      if (v !== null && v > threshold && !chipOnNet) continue;
      decidable = false;
      break;
    }
    if (!decidable || all.length === 0) continue;

    let high: number | null = null;
    for (const supply of DRIVER_SUPPLY[kind] ?? []) {
      const sNet = netOf(drv.componentId, supply);
      if (sNet) {
        high = solved(sNet);
        break;
      }
    }
    if (high === null) {
      // Unwired supply: the SPICE card falls back to the logic rail.
      const rail = nodeVoltages['vcc_rail'];
      high = typeof rail === 'number' && Number.isFinite(rail) ? rail : null;
    }
    if (high === null) continue; // no solve yet: try again on the next one
    out[pad] = { all, high_v: volts(high), low_v: 0 };
  }
  return out;
}

export function samePadLogic(a: PadLogicTable | null, b: PadLogicTable): boolean {
  return a !== null && JSON.stringify(a) === JSON.stringify(b);
}
