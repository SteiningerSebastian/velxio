/**
 * padLogicFor: the rule a chip pad behind an L293D output gets, derived from
 * the solved diagram (pinNetMap / nodeVoltages) and the simulator store.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { padLogicFor } from '../simulation/customChips/padLogic';
import { useElectricalStore } from '../store/useElectricalStore';
import { useSimulatorStore } from '../store/useSimulatorStore';

const CHIP = 'chip1';
const DRV = 'l293d1';
const BOARD = 'board1';

function setup(nets: Record<string, string>, volts: Record<string, number>) {
  useSimulatorStore.setState({
    components: [
      { id: CHIP, metadataId: 'custom-chip', x: 0, y: 0, properties: {} },
      { id: DRV, metadataId: 'motor-driver-l293d', x: 0, y: 0, properties: {} },
    ],
    boards: [{ id: BOARD, boardKind: 'esp32-s3' }],
  } as never);
  useElectricalStore.setState({
    pinNetMap: new Map(Object.entries(nets)),
    nodeVoltages: volts,
  } as never);
}

const base = {
  [`${CHIP}:MOTOR`]: 'nOut',
  [`${DRV}:OUT1`]: 'nOut',
  [`${DRV}:EN1`]: 'nEn',
  [`${BOARD}:21`]: 'nEn',
  [`${DRV}:IN1`]: 'nIn',
  [`${BOARD}:4`]: 'nIn',
  [`${DRV}:VCC2`]: 'nVs',
};

describe('padLogicFor', () => {
  beforeEach(() => {
    useElectricalStore.setState({ pinNetMap: new Map(), nodeVoltages: {} } as never);
  });

  it('OUT1 on the pad, EN1/IN1 on board GPIOs gives an AND rule at the VCC2 voltage', () => {
    setup(base, { nVs: 9 });
    expect(padLogicFor(CHIP, ['MOTOR'])).toEqual({
      MOTOR: { all: [21, 4], high_v: 9, low_v: 0 },
    });
  });

  it('takes the supply from the solved VCC2 net', () => {
    setup(base, { nVs: 5.004 });
    expect(padLogicFor(CHIP, ['MOTOR']).MOTOR?.high_v).toBe(5);
    setup(base, { nVs: 12 });
    expect(padLogicFor(CHIP, ['MOTOR']).MOTOR?.high_v).toBe(12);
  });

  it('an input that is not on a GPIO nor a solved high rail gives null', () => {
    const nets = { ...base, [`${BOARD}:4`]: 'other' };
    setup(nets, { nVs: 9, nIn: 0 });
    expect(padLogicFor(CHIP, ['MOTOR'])).toEqual({ MOTOR: null });
  });

  it('an enable on a solved high rail leaves only the IN gpio', () => {
    const nets: Record<string, string> = { ...base, [`${DRV}:EN1`]: 'nRail' };
    delete nets[`${BOARD}:21`];
    setup(nets, { nVs: 9, nRail: 3.3 });
    expect(padLogicFor(CHIP, ['MOTOR']).MOTOR).toEqual({ all: [4], high_v: 9, low_v: 0 });
  });

  it('a pad with a board GPIO on its net, or no wire, gets null', () => {
    setup({ ...base, [`${BOARD}:7`]: 'nOut' }, { nVs: 9 });
    expect(padLogicFor(CHIP, ['MOTOR', 'FREE'])).toEqual({ MOTOR: null, FREE: null });
  });
});
