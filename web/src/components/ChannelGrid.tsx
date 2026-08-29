import { useState } from "react";
import type { BoardConfig, Catalog, Telemetry } from "../types";
import { MiniWave } from "./MiniWave";
import { BlurInput } from "./BlurInput";
import { countsPerLsb, dacToVolts, voltsToDac, zeroCounts } from "../volts";

interface Props {
  catalog: Catalog;
  config: BoardConfig;
  tele: Telemetry | null;
  onDcOffset: (ch: number, dac: number) => void;
  onName: (ch: number, name: string) => void;
  /** Per-channel display range in volts; a missing entry means the default. */
  yRanges: Record<number, [number, number]>;
  onYRange: (ch: number, range: [number, number] | null, all: boolean) => void;
  waveMode: "avg" | "overlay" | "scope";
  clearEpoch: number;
  /** The settings lock, keyed "ch:<n>" per channel offset. */
  locked?: (key: string) => boolean;
  onUnlock?: (key: string) => void;
}

// DEAD keys off a SINGLE event's peak-to-peak, not the average: averaging
// erases dark pulses and noise alike, so quiet-but-alive channels (SiPMs
// seeing only darks) were branded DEAD. A live channel always shows its
// noise floor in a single event (~10 counts on this unit); flat below this
// means electronics genuinely silent - check the cable, not the source.
const DEAD_LAST_VPP = 5;
const RAIL_LO = 5, RAIL_HI = 4090;  // 12-bit corrected range clip guards

export function ChannelGrid({ catalog, config, tele, onDcOffset, onName,
                              yRanges, onYRange, waveMode, clearEpoch,
                              locked, onUnlock }: Props) {
  const g = catalog.geometry;
  const gsize = g.group_size;
  // undefined = follow the bank's enabled flag; set = the user overrode it
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const [renaming, setRenaming] = useState<number | null>(null);
  // Slider previews: value shown (and band drawn) while dragging, in volts.
  // The hardware write happens once, on release - a drag must not become a
  // register-write storm on a bus that answers -1 under bursts.
  const [preview, setPreview] = useState<Record<number, number | undefined>>({});

  const windowNs = tele ? tele.sample_period_ns * tele.record_length : undefined;
  const dcDef = catalog.channel.find((d) => d.key === "dc_offset");
  const dcHelp = [dcDef?.help, dcDef?.caen].filter(Boolean).join("\n\n");

  return (
    <div className="banks">
      {config.groups.map((grp, gi) => {
        const on = grp.enabled;
        const shown = open[gi] ?? on;   // disabled banks start collapsed
        const first = gi * gsize;

        return (
          <section key={gi} className={"bank" + (on ? "" : " off")}>
            <button className="bank-head" onClick={() => setOpen((o) => ({ ...o, [gi]: !shown }))}
              aria-expanded={shown}>
              <span className={"chevron" + (shown ? " open" : "")}>&#9656;</span>
              <span className="bank-title">Bank {gi}</span>
              {on ? null : <span className="bank-state">disabled</span>}
              <span className="bank-range">CH {first}&ndash;{first + gsize - 1}</span>
            </button>

            {shown ? (
              <div className="grid16">
                {Array.from({ length: gsize }, (_, i) => {
                  const ch = first + i;
                  const e = tele?.channels[String(ch)];
                  const has = !!e?.wave;
                  const clip = has && (e!.max! >= RAIL_HI || e!.min! <= RAIL_LO);
                  const dead = on && e?.last_vpp != null && e.last_vpp < DEAD_LAST_VPP;
                  const color = !on ? "#3a4150" : dead ? "#8b5cf6" : clip ? "#f0883e" : "#4ac776";
                  let badge = "", bcls = "";
                  if (!on) { badge = "off"; bcls = "off"; }
                  else if (clip) { badge = "CLIP"; bcls = "clip"; }
                  else if (dead) { badge = "DEAD"; bcls = "dead"; }

                  const cc = config.channels[ch];
                  const name = cc?.name ?? "";
                  const dac = cc?.dc_offset ?? g.dc_offset_mid;
                  const pv = preview[ch];
                  const shownV = pv ?? dacToVolts(dac, g);
                  const shownDac = pv != null ? voltsToDac(pv, g) : dac;
                  const vLimit = g.dc_offset_range_v / 2;
                  const commitSlider = () => {
                    if (pv == null) return;
                    setPreview((p) => ({ ...p, [ch]: undefined }));
                    onDcOffset(ch, voltsToDac(pv, g));
                  };

                  return (
                    <div key={ch} className={"tile" + (on ? "" : " disabled")}>
                      <div className="tile-head">
                        {renaming === ch ? (
                          <span className="ch-edit">
                            {/* Prefix is decoration; it never reaches the name. */}
                            <span className="ch-prefix">CH {ch} -&nbsp;</span>
                            <BlurInput
                              value={name} autoFocus placeholder="name"
                              onCommit={(v) => { setRenaming(null); onName(ch, v.trim()); }}
                              onCancel={() => setRenaming(null)}
                            />
                          </span>
                        ) : (
                          <button className="ch" onClick={() => setRenaming(ch)}
                            title="Click to rename">
                            CH {ch}{name ? " - " + name : ""}
                          </button>
                        )}
                        {badge ? <span className={"badge " + bcls}>{badge}</span> : null}
                      </div>

                      <MiniWave wave={on ? e?.wave : undefined}
                        geom={g} windowNs={windowNs} postTriggerPct={config.post_trigger}
                        color={color}
                        yRange={yRanges[ch]}
                        onYRange={(range, all) => onYRange(ch, range, all)}
                        mode={waveMode}
                        lastWave={on ? e?.last : undefined}
                        lastId={on ? e?.last_index : undefined}
                        baselineGuide={on ? zeroCounts(shownDac, g) : undefined}
                        offsetDac={shownDac} offsetSlope={countsPerLsb(g)}
                        clearEpoch={clearEpoch} />

                      {(() => {
                        const chLocked = locked?.(`ch:${ch}`) ?? false;
                        if (!chLocked) return null;
                        return (
                          <button className="lock-chip tile-lock"
                            title="DC offset locked. Click to unlock just this channel."
                            onClick={() => onUnlock?.(`ch:${ch}`)}>🔒</button>
                        );
                      })()}
                      <div className={"tile-dc" + ((locked?.(`ch:${ch}`) ?? false) ? " locked" : "")}
                        title={`${dcHelp}\n\nDAC word: ${shownDac}`}>
                        <label>DC offset</label>
                        {/* Coarse placement by slider (0.01 V steps, previewed
                            live in the band above, written on release); fine
                            trim by typing (1 mV). */}
                        <input className="dc-slider" type="range"
                          min={-vLimit} max={vLimit} step={0.01}
                          value={shownV}
                          onChange={(ev) => setPreview((p) => ({ ...p, [ch]: Number(ev.target.value) }))}
                          onPointerUp={commitSlider}
                          onKeyUp={commitSlider}
                          onBlur={commitSlider} />
                        <span className="field">
                          <BlurInput
                            type="number" step={0.005}
                            min={-vLimit} max={vLimit}
                            value={shownV.toFixed(3)}
                            selectOnFocus
                            onCommit={(v) => {
                              const clamped = Math.min(vLimit, Math.max(-vLimit, Number(v || 0)));
                              onDcOffset(ch, voltsToDac(clamped, g));
                            }}
                          />
                          <span className="unit">V</span>
                        </span>
                      </div>

                      <div className="tile-foot">
                        {/* Always rendered, so the tile never resizes when
                            events start arriving - an empty span has no
                            height, and the grid used to jump. */}
                        <span className="n">n={e?.count ?? 0}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
