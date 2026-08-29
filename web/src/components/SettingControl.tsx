import type { Catalog, SettingDef } from "../types";
import { BlurInput } from "./BlurInput";
import { defDacToVolts, defVoltsToDac } from "../volts";
import { StepControl } from "./StepControl";

interface Props {
  def: SettingDef;
  value: any;
  geom: Catalog["geometry"];
  /** Value of the setting this one's reachable range depends on. */
  dependsOn?: any;
  /** An optional setting whose checkbox is off renders inert. */
  disabled?: boolean;
  onChange: (v: any) => void;
}

/** Whatever was typed lands inside [min, max]. The HTML attributes alone only
 *  style the spinner - they do not stop a typed 5000 from reaching a field
 *  whose hardware ceiling is 1023, so the commit path is where the bound is
 *  enforced. NaN (a cleared field) falls back to the nearer of min or 0. */
function clamp(raw: number, min?: number, max?: number): number {
  let v = Number.isFinite(raw) ? raw : (min ?? 0);
  if (min !== undefined) v = Math.max(min, v);
  if (max !== undefined) v = Math.min(max, v);
  return v;
}

/** Renders one setting from its catalog definition. Anything that is physically
 *  a voltage is edited as volts; the DAC word never reaches the operator. */
export function SettingControl({ def, value, geom, dependsOn, disabled, onChange }: Props) {
  if (def.type === "steps") {
    if (disabled) return <span className="muted mono">{String(value ?? "—")}</span>;
    const steps = def.values_by_freq?.[String(dependsOn)] ?? [];
    return steps.length
      ? <StepControl steps={steps} value={Number(value ?? 0)} onChange={onChange} />
      : <span className="muted">—</span>;
  }

  if (def.type === "bool") {
    return <input type="checkbox" checked={!!value} disabled={disabled}
      onChange={(e) => onChange(e.target.checked)} />;
  }

  if (def.type === "enum") {
    return (
      <select value={String(value)} disabled={disabled} onChange={(e) => {
        const raw = e.target.value;
        const num = def.choices?.find((c) => String(c.value) === raw)?.value;
        onChange(typeof num === "number" ? num : raw);
      }}>
        {def.choices?.map((c) => (
          <option key={String(c.value)} value={String(c.value)}>{c.label}</option>
        ))}
      </select>
    );
  }

  if (def.type === "volts") {
    // The setting's own calibration (lsb_v/zero_dac, the TR path) or the
    // channel-input model - through the shared pair in volts.ts, the same
    // pair the change toast formats with, so field and toast can never quote
    // different voltages for one DAC word. Bounds are the DAC endpoints
    // mapped through the same line.
    const ends = [defDacToVolts(def, 0, geom), defDacToVolts(def, 0xFFFF, geom)];
    const lo = Math.min(...ends), hi = Math.max(...ends);
    const mid = def.zero_dac ?? geom.dc_offset_mid;
    return (
      <span className="field">
        <BlurInput
          type="number" step={def.lsb_v != null ? 0.001 : 0.005}
          min={lo} max={hi} selectOnFocus
          disabled={disabled}
          value={defDacToVolts(def, Number(value ?? mid), geom).toFixed(3)}
          onCommit={(v) => onChange(defVoltsToDac(def,
            clamp(num(v, defDacToVolts(def, Number(value ?? mid), geom)), lo, hi), geom))}
        />
        <span className="unit">V</span>
      </span>
    );
  }

  return (
    <span className="field">
      <BlurInput
        type="number" min={def.min} max={def.max} selectOnFocus
        disabled={disabled}
        value={value ?? 0}
        // The min/max attributes only drive the spinner - they do not stop
        // anything being typed. Clamping here keeps a value the catalog already
        // says is out of range from being sent for the board to reject.
        onCommit={(v) => onChange(clamp(num(v, Number(value ?? 0)), def.min, def.max))}
      />
      {def.unit ? <span className="unit">{def.unit}</span> : null}
    </span>
  );
}

/** A finite number, or `fallback`. NaN used to reach JSON.stringify, arrive at
 *  the server as null, and fail there with a type error instead of a message. */
function num(raw: string, fallback: number) {
  const v = Number(raw);
  return raw.trim() !== "" && Number.isFinite(v) ? v : fallback;
}
