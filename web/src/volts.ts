import type { Catalog } from "./types";

export type Geom = Catalog["geometry"];

/** The DC offset is a uint16 DAC word on the wire (that is what CAEN's API
 *  takes). Humans think in volts, so every human-facing control converts here
 *  and nowhere else. Midscale = no shift = 0 V.
 *
 *  The DAC spans +/-1 V while the ADC window is only 1 Vpp, and increasing the
 *  DAC LOWERS the baseline - both measured on the board, both the opposite of
 *  what the obvious guess would be. */
export function dacToVolts(dac: number, g: Geom) {
  return -((dac - g.dc_offset_mid) / g.dc_offset_mid) * (g.dc_offset_range_v / 2);
}

export function voltsToDac(v: number, g: Geom) {
  const dac = Math.round(g.dc_offset_mid * (1 - v / (g.dc_offset_range_v / 2)));
  return Math.min(g.dc_offset_max, Math.max(0, dac));
}

/** ADC counts per DAC LSB: negative, and the offset range is twice the window,
 *  so a full DAC sweep drags the baseline across the window twice over. */
export function countsPerLsb(g: Geom) {
  return -(g.dc_offset_range_v / (g.dc_offset_max + 1)) * ((g.adc_max + 1) / g.input_range_vpp);
}

/** TR path constants (RADiCAL bench calibration; provisional until the
 *  comparator-domain experiment refines them). Both in window ADC counts. */
export const TR_OFF_SLOPE_COUNTS = -0.19;              // per offset-DAC LSB
export const TR_THR_ZERO_DAC = 25448;
export const TR_THR_COUNTS_PER_LSB = 0.0329 * 4.096;   // mV/LSB -> counts/LSB

export function trBaselineCounts(offDac: number): number {
  return 2048 + (offDac - 32768) * TR_OFF_SLOPE_COUNTS;
}

export function trThresholdCounts(thrDac: number): number {
  return 2048 + (thrDac - TR_THR_ZERO_DAC) * TR_THR_COUNTS_PER_LSB;
}

/** The MANUAL's threshold arithmetic (UM4270 rev 12, sec 9.8.3-9.8.4): the
 *  TR0 comparator spans 0-2.5 V behind a x2 input attenuator; its DAC moves
 *  13.2 counts per connector-mV, and DAC 0x6666 = 26214 is the signal's
 *  0-Volt WHEN THE TR DC OFFSET SITS AT MIDSCALE (0x8000). CAEN's worked
 *  example: a -400 mV NIM trigger is 26214 - 400*13.2 = 20934 - the value
 *  that worked here on day one. The manual states outright that no simple
 *  formula exists for other offset values, which is why the offset belongs
 *  at midscale and the UI warns when it is not. */
export const TR_THR_MID_DAC = 26214;
export const TR_THR_MV_PER_LSB = 1 / 13.2;
export const TR_OFF_MID_DAC = 32768;

export function trAbsThresholdV(thrDac: number): number {
  return ((thrDac - TR_THR_MID_DAC) * TR_THR_MV_PER_LSB) / 1000;
}

export function trThresholdDacForAbs(absV: number): number {
  const dac = Math.round(TR_THR_MID_DAC + (absV * 1000) / TR_THR_MV_PER_LSB);
  return Math.min(0xFFFF, Math.max(0, dac));
}

/** Where 0 V lands in ADC counts for a given DC offset. */
export function zeroCounts(dac: number, g: Geom) {
  return (g.adc_max + 1) / 2 + (dac - g.dc_offset_mid) * countsPerLsb(g);
}

export function voltsAtCount(counts: number, dac: number, g: Geom) {
  return (counts - zeroCounts(dac, g)) * (g.input_range_vpp / (g.adc_max + 1));
}

/** Window-referenced volts: the ADC always reads its fixed 1 Vpp window, and
 *  the DC offset moves the SIGNAL within it - so the display frame is the
 *  window itself, 0 at its centre. Counts map to fixed screen positions,
 *  which is what makes recorded history immovable: no knob turned today can
 *  shift what was measured a moment ago. No calibration model involved. */
export function windowVolts(counts: number, g: Geom): number {
  return (counts - (g.adc_max + 1) / 2) / (g.adc_max + 1) * g.input_range_vpp;
}

/** Default display range: the full 1 Vpp window. The plot edges ARE the ADC
 *  rails - a clipped signal sits pinned against them. */
export const DEFAULT_Y: [number, number] = [-0.5, 0.5];

/** A setting's own DAC<->volts line, when its catalog entry carries one
 *  (lsb_v/zero_dac - the TR path); the channel-input model otherwise. EVERY
 *  place that shows a volts-typed setting must convert through these two, or
 *  the field and the change toast quote different voltages for one DAC word. */
export function defDacToVolts(
  def: { lsb_v?: number; zero_dac?: number }, dac: number, g: Geom,
): number {
  if (def.lsb_v != null && def.zero_dac != null) {
    return (dac - def.zero_dac) * def.lsb_v;
  }
  return dacToVolts(dac, g);
}

export function defVoltsToDac(
  def: { lsb_v?: number; zero_dac?: number }, v: number, g: Geom,
): number {
  if (def.lsb_v != null && def.zero_dac != null) {
    return Math.min(0xFFFF, Math.max(0, Math.round(def.zero_dac + v / def.lsb_v)));
  }
  return voltsToDac(v, g);
}

/** Signed volts, e.g. "+0.500 V". */
export function fmtV(v: number) {
  const mag = Math.abs(v) < 5e-4 ? "0.000" : Math.abs(v).toFixed(3);
  return (v < 0 ? "-" : "+") + mag + " V";
}
