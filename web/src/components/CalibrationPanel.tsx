import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CalibrationStatus } from "../api";

interface Props {
  connected: boolean;
  recording: boolean;
  /** The settings lock: calibration steers DC offsets, so it locks too. */
  locked?: boolean;
  onUnlock?: () => void;
  /** A run began - here or in another window; the app wipes the piles. */
  onStarted?: () => void;
  /** Called when a run finishes: the server changed the config underneath the
   *  UI, so the App must re-fetch what the board now holds. */
  onFinished: (st: CalibrationStatus) => void;
  onError: (title: string, lines?: string[]) => void;
}

/** Closed-loop channel setup, polarity-agnostic - the data says where the
 *  pulse goes, not a setting:
 *
 *  Auto-baseline: software triggers, every baseline (TR0 included) servoed to
 *  the window centre. The no-signal starting point.
 *  Fit to signal: with real triggers flowing, each channel's actual
 *  excursions - afterpulses of either sign included - are measured and the
 *  baseline placed so the whole pulse sits in the window with margin. */
export function CalibrationPanel({ connected, recording, locked, onUnlock,
                                   onStarted, onFinished, onError }: Props) {
  const [st, setSt] = useState<CalibrationStatus | null>(null);
  const [fitEvents, setFitEvents] = useState("100");
  const wasActive = useRef(false);

  // Poll while a run is active - also on mount, so a page opened mid-run
  // picks the progress up rather than showing a dead panel.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const s = await api.calibrateStatus();
        if (cancelled) return;
        setSt(s);
        if (s.active) {
          timer = window.setTimeout(tick, 600);
          // A run that began elsewhere (another window) wipes here too.
          if (!wasActive.current) onStarted?.();
        } else if (wasActive.current) {
          wasActive.current = false;
          onFinished(s);
        }
        if (s.active) wasActive.current = true;
      } catch {
        // The server is away - a restart, a network blip. Keep asking: a
        // poll loop that dies here freezes the panel on its last state,
        // which once left an uncancellable ghost spinner after a redeploy
        // killed the server mid-run.
        if (!cancelled) timer = window.setTimeout(tick, 1500);
      }
    };
    tick();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [st?.active]);   // re-arm the poll loop when activity flips

  const run = async (mode: "baseline" | "fit") => {
    try {
      const n = mode === "fit" ? Number(fitEvents) : null;
      await api.calibrate(mode, Number.isFinite(n as number) && (n as number) > 0 ? n : null);
      onStarted?.();
      wasActive.current = true;
      setSt((s) => s ? { ...s, active: true, phase: mode, message: "starting" }
                    : { active: true, phase: mode, message: "starting",
                        iteration: 0, report: [], error: null });
    } catch (e) {
      onError("Could not start the calibration", [String(e)]);
    }
  };

  const cancelRun = async () => {
    // "No calibration is running" is an answer, not a failure: the panel
    // may be showing a run the server no longer knows about. Either way,
    // re-fetch the truth so Cancel always resolves what is on screen.
    try { await api.calibrateCancel(); } catch { /* resolved below */ }
    try { setSt(await api.calibrateStatus()); } catch { /* the poll retries */ }
  };

  const busy = !!st?.active;
  const rows = st?.report ?? [];
  const bad = rows.filter((r) => r.status !== "ok");

  return (
    <div className="card">
      <h2>Calibration</h2>
      <div className="calib-btns">
        {locked ? (
          <button className="lock-chip"
            title="Calibration locked - it steers DC offsets. Click to unlock."
            onClick={onUnlock}>🔒</button>
        ) : null}
        <button disabled={!connected || busy || recording || locked} onClick={() => run("baseline")}
          title="Software triggers; every channel's baseline (TR0 too) is servoed to the window centre. The setup-day tool: works on a dark bench, recovers railed channels, flags sick ones.">
          Center baselines <span className="calib-note">no signal needed</span>
        </button>
        <button disabled={!connected || busy || recording || locked} onClick={() => run("fit")}
          title="Needs real triggers. Measures each channel's actual pulse excursions - afterpulses of either sign included - and places the baseline so everything fits in the window with margin.">
          Fit to pulses <span className="calib-note">needs triggers</span>
        </button>
        <label className="calib-events"
          title="Triggered events per fit measurement. It waits however long they take; it only stops if nothing triggers for 30 s.">
          <input type="number" min={4} value={fitEvents}
            disabled={busy}
            onChange={(e) => setFitEvents(e.target.value)} />
          ev
        </label>
      </div>
      {busy ? (
        <p className="calib-progress">
          <span className="spinner" /> {st!.phase}: {st!.message}
          <button className="calib-cancel"
            title="Stop at the next safe point; the board keeps the last completed pass"
            onClick={cancelRun}>
            Cancel
          </button>
        </p>
      ) : null}
      {st?.error ? <p className="calib-error">{st.error}</p> : null}
      {!busy && rows.length ? (
        <div className="calib-report">
          <div className="calib-sum">
            {rows.length - bad.length} of {rows.length} channels ok
            {bad.length ? ` - attention: ${bad.map((r) => r.channel).join(", ")}` : ""}
          </div>
          <table>
            <tbody>
              {rows.map((r) => (
                <tr key={r.channel} className={r.status !== "ok" ? "bad" : ""}>
                  <td>{r.channel}</td>
                  <td className="mono">{r.baseline_mv != null ? `${r.baseline_mv} mV` : "-"}</td>
                  <td className="mono" title="Measured excursion below / above the baseline">
                    -{r.below_mv}/+{r.above_mv}
                  </td>
                  <td>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="muted">
        Save a session afterwards to give the converged state a name.
      </p>
    </div>
  );
}
