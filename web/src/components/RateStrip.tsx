import { useMemo } from "react";
import type { Telemetry } from "../types";

/** Trigger-rate strip: a bare filled area with no x axis at all, y scaled to the
 *  visible peak. Drawn directly rather than with uPlot — every requirement here
 *  (no x axis, zero pinned to the bottom edge, exactly two y labels, the top one
 *  being the true peak) is something uPlot's axis layout works against. */
export function RateStrip({ tele }: { tele: Telemetry | null }) {
  const rate = tele?.rate.rate ?? [];
  const last = rate.length ? rate[rate.length - 1] : 0;
  const peak = rate.length ? Math.max(...rate) : 0;
  const count = tele?.rate.total ?? 0;

  const paths = useMemo(() => {
    const n = rate.length;
    if (n < 2) return null;
    const H = 100;
    const w = n - 1;
    // Flat along the bottom until something actually triggers.
    const y = (v: number) => (peak > 0 ? H - (v / peak) * H : H);
    const pts = rate.map((v, i) => `${i} ${y(v)}`);
    return {
      w,
      line: `M ${pts.join(" L ")}`,
      area: `M 0 ${H} L ${pts.join(" L ")} L ${w} ${H} Z`,
    };
  }, [rate, peak]);

  return (
    <div className="rate-wrap">
      <div className="rate-num">
        <span className="big mono">{fmtSI(last)}</span>
        <span className="unit">triggers/s</span>
        <span className="total mono"
          title="Every trigger seen since acquisition was armed - runs or no runs. Watching and recording are separate: this keeps counting between runs, so it will exceed any one run's event count.">
          Seen: {fmtSI(count)}</span>
      </div>

      <div className="rate-plot">
        {paths ? (
          <svg
            className="rate-svg"
            viewBox={`0 0 ${paths.w} 100`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <path className="rate-area" d={paths.area} />
            <path className="rate-line" d={paths.line} vectorEffect="non-scaling-stroke" />
          </svg>
        ) : null}
        {/* Top label only once there is a real peak to name. */}
        {peak > 0 ? <span className="rate-y top mono">{fmtSI(peak)}</span> : null}
        <span className="rate-y zero mono">0</span>
      </div>
    </div>
  );
}

const SI_UNITS = ["K", "M", "G", "T"];

/** Rates and counts reach the millions, so scale into SI once past 999.
 *
 *  Below 1000 the value is a whole number (a bucket count times the update
 *  frequency), so it prints with no decimal at all. Above that it is always
 *  1-3 digits and exactly one decimal: 1.5K, 999.9K, 12.3M.
 */
function fmtSI(v: number) {
  const a = Math.abs(v);
  if (a < 1000) return Number.isInteger(v) ? String(v) : v.toFixed(1);
  let x = v / 1000;
  let i = 0;
  // 999_999 would render as "1000.0K"; promote it to "1.0M" instead so the
  // mantissa never grows a fourth digit.
  while (Math.abs(x) >= 999.95 && i < SI_UNITS.length - 1) {
    x /= 1000;
    i++;
  }
  return x.toFixed(1) + SI_UNITS[i];
}
