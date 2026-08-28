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

/** The comparator's TRUE response, measured on serial 53364 (2026-08-28) by
 *  the spectrum-edge method: record runs at two threshold DACs and read the
 *  sharp lower edge of the accepted MCP pulse-depth distribution - the one
 *  observable the analog comparator has. DAC 33731 cut at -222 mV below
 *  baseline and DAC 37983 at -201 mV, so the real scale is ~0.005 mV/LSB -
 *  SEVEN times more compressed than the bench constant above. The bench
 *  numbers remain only for the window-frame plot marker; the operator-facing
 *  relative-threshold field speaks the measured truth. Anchored at TR offset
 *  DAC 25815; re-measure the anchor if the offset moves far from there. */
export const TR_THR_TRUE_MV_PER_LSB = 0.00494;
export const TR_THR_ANCHOR_DAC = 33731;
export const TR_THR_ANCHOR_REL_MV = -222;
/** Threshold-DAC LSBs per offset-DAC LSB that hold the operator's margin
 *  when the TR offset moves: the offset shifts the input by -0.0464 mV/LSB,
 *  and each threshold LSB is worth only 0.00494 true mV - so the threshold
 *  must chase ~9.4x as many LSBs as the offset moved. */
export const TR_COUPLING_LSB_PER_OFF_LSB = -0.0464 / TR_THR_TRUE_MV_PER_LSB;

/** The threshold DAC that puts the trigger `relV` volts from the baseline -
 *  how the operator thinks ("trigger at -140 mV") - through the MEASURED
 *  comparator response, not the bench model that once armed -235 mV when
 *  -140 was typed. */
export function trThresholdDacFor(relV: number, _offDac: number, _g: Geom): number {
  const dac = Math.round(TR_THR_ANCHOR_DAC
    + (relV * 1000 - TR_THR_ANCHOR_REL_MV) / TR_THR_TRUE_MV_PER_LSB);
  return Math.min(0xFFFF, Math.max(0, dac));
}

export function trRelThresholdV(thrDac: number, _offDac: number, _g: Geom): number {
  return (TR_THR_ANCHOR_REL_MV
    + (thrDac - TR_THR_ANCHOR_DAC) * TR_THR_TRUE_MV_PER_LSB) / 1000;
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
