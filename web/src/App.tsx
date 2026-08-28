import { useCallback, useEffect, useRef, useState } from "react";
import { api, openTelemetry } from "./api";
import type { DisplayPrefs, WaveMode } from "./api";
import { SessionsPanel } from "./components/SessionsPanel";
import { CalibrationPanel } from "./components/CalibrationPanel";
import type { BoardConfig, Catalog, Status, Telemetry } from "./types";
import { ChannelGrid } from "./components/ChannelGrid";
import { BankPanel } from "./components/BankPanel";
import { SettingsList } from "./components/SettingsList";
import { Collapsible } from "./components/Collapsible";
import { ConfigPanel } from "./components/ConfigPanel";
import { Toasts, useToasts } from "./components/Toasts";
import { RunsPanel } from "./components/RunsPanel";
import { Elapsed } from "./components/Elapsed";
import { Tour } from "./components/Tour";
import { QUICK_USE } from "./quickuse";
import { describeChanges } from "./changes";
import { RateStrip } from "./components/RateStrip";
import { MiniWave } from "./components/MiniWave";
import { ConnectionBadge } from "./components/ConnectionBadge";
import { STATUS_POLL_MS } from "./types";
import { PERSIST_TRACES } from "./waveDensity";
import { BlurInput } from "./components/BlurInput";
import { TR_OFF_SLOPE_COUNTS, TR_THR_COUNTS_PER_LSB, trRelThresholdV,
         trThresholdDacFor, windowVolts } from "./volts";

export function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [config, setConfig] = useState<BoardConfig | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [tele, setTele] = useState<Telemetry | null>(null);
  const [serverUp, setServerUp] = useState(true);
  const [reconnecting, setReconnecting] = useState(false);
  const [runName, setRunName] = useState("");
  // Empty = let the server infer the next number from the data directory.
  const [runNo, setRunNo] = useState("");
  const [stampRun, setStampRun] = useState(true);
  const [tour, setTour] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runsKey, setRunsKey] = useState(0);   // bump to re-list runs
  // Per-channel waveform display ranges (volts). Persisted server-side so a
  // daq restart or a different browser comes back to the same view.
  const [yRanges, setYRanges] = useState<Record<number, [number, number]>>({});
  // "avg": the 1 s rolling mean. "overlay": the last N single events piled
  // into a density picture. "scope": the newest single trace alone, fed by
  // free-running software triggers. Persisted with the display state.
  const [waveMode, setWaveMode] = useState<WaveMode>("avg");
  // Scope mode's software-trigger rate; committed via /api/scope.
  const [scopeHz, setScopeHz] = useState("2");
  // The scope's channel-trigger: "" = show every event; a channel number =
  // only events where that trace crosses the level refresh the display.
  const [scopeTrigCh, setScopeTrigCh] = useState("");
  const [scopeTrigMv, setScopeTrigMv] = useState("20");
  const [scopeTrigEdge, setScopeTrigEdge] = useState<"rising" | "falling">("falling");
  const [testN, setTestN] = useState("100");
  // Blank = record until stopped; a number = auto-close the run at N events.
  const [recMax, setRecMax] = useState("");
  // The run-notes dialog: Record opens it, and the note it collects lands in
  // run_metadata.json - what was tested, beam energy, the context no
  // register readback can supply. Cleared after each run starts: a note
  // describes ONE run, and a stale one silently attached to the next run
  // would be worse than none.
  const [recDialog, setRecDialog] = useState(false);
  const [recNote, setRecNote] = useState("");
  // The TR0 baseline marker's memory: the last MEASURED baseline and the
  // offset DAC it was measured under. The measured and bench-predicted
  // baselines disagree by ~200 mV on this unit, so falling back to
  // prediction whenever the 1 s averaging window drained made the marker
  // jump on every trigger pause. Hold the measurement instead, shifted by
  // the predicted DELTA when the offset moves - deltas are robust even
  // where the absolute calibration is not.
  const trBaseMem = useRef<{ counts: number; dac: number } | null>(null);
  // Bumped to wipe every channel's persistence pile: on recording start and
  // on calibration start, so each pile tells one coherent story - a
  // calibration's profile stays on screen for review until the next thing
  // that would muddle it begins.
  const [wipeEpoch, setWipeEpoch] = useState(0);
  const saveTimer = useRef<number | undefined>(undefined);
  const displayTimer = useRef<number | undefined>(undefined);
  // The config the unit last confirmed - what a change gets measured against.
  const confirmed = useRef<BoardConfig | null>(null);
  const { toasts, push, dismiss } = useToasts();

  const loadOnce = useCallback(async () => {
    setLoadError(null);
    try {
      const [cat, cfg, st] = await Promise.all([api.catalog(), api.getConfig(), api.status()]);
      setCatalog(cat); setConfig(cfg); setStatus(st);
      confirmed.current = cfg;
    } catch (e) {
      // Leaving this to console.error left the page reading "Loading..." for
      // ever, with nothing on screen to say the server had not answered.
      setLoadError(e instanceof Error ? e.message : String(e));
    }
    // Display prefs restore on their own - they never touch the hardware.
    api.getDisplay().then((d) => {
      setYRanges(fromPrefs(d));
      // The display mode restores; the scope's trigger firing does NOT start
      // on page load - status.scope_hz says whether a scope is already live.
      setWaveMode(asWaveMode(d.wave_mode));
    }).catch(() => {});
  }, []);

  useEffect(() => { loadOnce(); }, [loadOnce]);

  const asWaveMode = (v: DisplayPrefs["wave_mode"]): WaveMode =>
    v === "overlay" || v === "scope" ? v : "avg";

  const fromPrefs = (d: DisplayPrefs): Record<number, [number, number]> => {
    const out: Record<number, [number, number]> = {};
    for (const [k, v] of Object.entries(d.y_ranges ?? {})) {
      // Window-referenced volts: nothing outside the 1 Vpp window is ever
      // readable, and ranges saved under the older input-referred frame
      // (up to +/-1 V) are silently retired by the same check.
      if (Array.isArray(v) && v.length === 2 && v[0] < v[1]
          && v[0] >= -0.501 && v[1] <= 0.501) {
        out[Number(k)] = [v[0], v[1]];
      }
    }
    return out;
  };

  const saveDisplay = (ranges: Record<number, [number, number]>,
                       mode: WaveMode) => {
    window.clearTimeout(displayTimer.current);
    displayTimer.current = window.setTimeout(() => {
      const y_ranges: Record<string, [number, number]> = {};
      for (const [k, v] of Object.entries(ranges)) y_ranges[k] = v;
      api.setDisplay({ y_ranges, wave_mode: mode }).catch(() => {});
    }, 400);
  };

  const applyYRanges = (next: Record<number, [number, number]>) => {
    setYRanges(next);
    saveDisplay(next, waveMode);
  };

  const changeWaveMode = (mode: WaveMode) => {
    // Entering scope starts the free-running software triggers; leaving it
    // stops them - the display mode and the trigger source are one gesture,
    // so a scope never silently fires with nobody watching it.
    if (mode === "scope" && waveMode !== "scope") {
      applyScope(scopeHz, scopeTrigCh, scopeTrigMv, scopeTrigEdge);
    } else if (mode !== "scope" && waveMode === "scope") {
      api.scope(false).then((r) => setStatus(r.status)).catch(() => {});
    }
    setWaveMode(mode);
    saveDisplay(yRanges, mode);
  };

  /** Push the whole scope state - rate and channel-trigger - in one call, so
   *  the server never holds a mix of old and new pieces. */
  const applyScope = async (hzRaw: string, trigCh: string, trigMv: string,
                            edge: "rising" | "falling") => {
    const hz = Math.min(20, Math.max(0.1, Number(hzRaw) || 2));
    setScopeHz(String(hz));
    const trigger = trigCh === "" ? null : {
      channel: Number(trigCh),
      level_mv: Math.min(500, Math.max(1, Number(trigMv) || 20)),
      edge,
    };
    try {
      const r = await api.scope(true, hz, trigger);
      setStatus(r.status);
      if (!r.ok) push("err", "Could not start the scope", [r.error ?? ""]);
    } catch (e) {
      push("err", "Could not start the scope",
           [e instanceof Error ? e.message : String(e)]);
    }
  };

  const changeYRange = (ch: number, range: [number, number] | null, all: boolean) => {
    const next = { ...yRanges };
    const targets = all && catalog
      ? Array.from({ length: catalog.geometry.num_channels }, (_, i) => i) : [ch];
    for (const t of targets) {
      if (range === null) delete next[t];
      else next[t] = range;
    }
    applyYRanges(next);
  };

  const catalogRef = useRef<Catalog | null>(null);
  catalogRef.current = catalog;

  useEffect(() => openTelemetry(setTele), []);

  // Poll status: the board can vanish (unit switched off) or come back at any
  // time, and only /api/status actually pokes it.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await api.status();
        if (!cancelled) { setStatus(st); setServerUp(true); }
      } catch {
        if (!cancelled) setServerUp(false);
      }
    };
    tick();
    const id = window.setInterval(tick, STATUS_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const pushConfig = (next: BoardConfig) => {
    setConfig(next);                       // optimistic, for input responsiveness
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      // Whatever the board reports wins - a rejected write must not leave the
      // UI showing a value the hardware never took.
      api.setConfig(next)
        .then((r) => {
          setConfig(r.config);
          const prev = confirmed.current ?? r.config;
          const lines = catalogRef.current
            ? describeChanges(prev, r.config, next, catalogRef.current) : [];
          confirmed.current = r.config;
          if (r.connected === false) {
            // Nothing was sent; the fields have just snapped back.
            push("warn", "No unit connected", ["Nothing was sent. Reconnect, then try again."]);
          } else if (r.errors?.length) {
            setStatus((st) => st && { ...st, errors: [...st.errors, ...r.errors] });
            push("err", "Unit rejected a setting", r.errors);
          } else if (lines.length) {
            push("ok", "Applied and read back from unit", lines);
          }
        })
        .catch(() => push("err", "Could not reach the DAQ server"));
    }, 250);
  };
  const updateBoard = (key: string, value: any) =>
    config && pushConfig({ ...config, [key]: value });
  const updateGroup = (g: number, key: string, value: any) => {
    if (!config) return;
    const groups = config.groups.map((gc, i) => (i === g ? { ...gc, [key]: value } : gc));
    pushConfig({ ...config, groups });
  };
  // The DT5742B has ONE TR0 split to both mezzanines; each keeps its own
  // registers, but there is no sensible reason for them to differ, so the
  // TR0 panel writes every bank at once. Divergence (an old config, a raw
  // register poke) is surfaced below the panel, never silently masked.
  const updateTrBoth = (key: string, value: any) => {
    if (!config) return;
    if (key === "fast_trigger_dc_offset") {
      // The comparator lives in the window frame, so moving the offset moves
      // the baseline RELATIVE to a fixed threshold. Operators think
      // baseline-relative ("trigger at -100 mV"), so the threshold DAC
      // follows the offset by the predicted baseline shift - the margin is
      // preserved, and calibration can never strand the trigger.
      const old = config.groups[0].fast_trigger_dc_offset;
      const dThr = Math.round(
        TR_OFF_SLOPE_COUNTS * (value - old) / TR_THR_COUNTS_PER_LSB);
      const groups = config.groups.map((gc) => ({
        ...gc,
        fast_trigger_dc_offset: value,
        fast_trigger_threshold: Math.min(0xFFFF, Math.max(0,
          gc.fast_trigger_threshold + dThr)),
      }));
      pushConfig({ ...config, groups });
      return;
    }
    pushConfig({ ...config, groups: config.groups.map((gc) => ({ ...gc, [key]: value })) });
  };
  const updateChannel = (ch: number, patch: Partial<BoardConfig["channels"][number]>) => {
    if (!config) return;
    const channels = config.channels.map((c, i) => (i === ch ? { ...c, ...patch } : c));
    pushConfig({ ...config, channels });
  };

  const failed = (what: string) => (e: unknown) =>
    push("err", what, [e instanceof Error ? e.message : String(e)]);

  const start = async () => {
    try {
      const st = await api.start();
      setStatus(st);
      // The server refuses rather than raising, so a 200 does not mean it
      // started. The reason is already in the errors panel; the toast points
      // at it instead of leaving the button looking inert.
      if (!st.started) {
        const why = st.errors.slice(-2);
        push("err", "Acquisition did not start",
             why.length ? why : ["See the Errors panel."]);
      }
    } catch (e) {
      failed("Could not start acquisition")(e);
    }
  };
  const stop = async () => {
    try {
      setStatus(await api.stop());
    } catch (e) {
      failed("Could not stop acquisition")(e);
    }
  };
  const fireTest = async () => {
    // The bench source: the 742 has no channel self-trigger, so with nothing
    // on TRG-IN/TR0 this is how events happen. Starts acquisition on its own.
    try {
      const n = Math.max(1, Math.round(Number(testN) || 100));
      const r = await api.trigger(n, 10);
      setStatus(r.status);
      if (!r.ok) push("err", "Could not fire test triggers", [r.error ?? ""]);
      else push("ok", `Firing ${r.queued} test triggers at 10 Hz`);
    } catch (e) {
      failed("Could not fire test triggers")(e);
    }
  };
  const startRec = async () => {
    setRecDialog(false);
    try {
      const n = runNo.trim() === "" ? null : Number(runNo);
      const m = recMax.trim() === "" ? null : Number(recMax);
      const r = await api.recStart(runName, stampRun,
                                   Number.isFinite(n as number) ? n : null,
                                   Number.isFinite(m as number) ? m : null,
                                   recNote.trim());
      setStatus(r.status);
      if (!r.ok) push("err", "Could not start recording", [r.error ?? "no reason given"]);
      else {
        push("ok", "Recording", [`${r.run}`]);
        setRunNo("");            // the next number is inferred again
        setRecNote("");          // a note describes one run, never the next
        setRunsKey((k) => k + 1);
      }
    } catch (e) {
      failed("Could not start recording")(e);
    }
  };
  const stopRec = async () => {
    try {
      const r = await api.recStop();
      setStatus(r.status);
      push(r.ok ? "ok" : "warn",
           r.ok ? "Recording stopped" : "Nothing was recording",
           [r.ok ? `${r.run}` : (r.error ?? "")]);
      setRunsKey((k) => k + 1);
    } catch (e) {
      failed("Could not stop the recording")(e);
    }
  };
  const reconnect = async () => {
    setReconnecting(true);
    try {
      setStatus(await api.reconnect());
      setServerUp(true);
    } catch {
      setServerUp(false);
    } finally {
      setReconnecting(false);
    }
  };

  // Recording start wipes the persistence piles (calibration start does too,
  // via the panel's onStarted).
  const recordingNow = tele?.recording ?? status?.recording ?? false;
  const prevRecording = useRef(false);
  useEffect(() => {
    if (recordingNow && !prevRecording.current) setWipeEpoch((e) => e + 1);
    prevRecording.current = recordingNow;
  }, [recordingNow]);

  if (!catalog || !config) {
    return (
      <div className="loading">
        {loadError ? (
          <>
            <p>Could not reach the DAQ server.</p>
            <p className="mono err">{loadError}</p>
            <p className="muted">
              Check it is running (<code>daq status</code>), then try again.
            </p>
            <button className="primary" onClick={loadOnce}>Retry</button>
          </>
        ) : "Loading\u2026"}
      </div>
    );
  }
  const running = tele?.running ?? status?.running ?? false;
  const connected = serverUp && !!status?.opened;
  const recording = recordingNow;
  const acqState = recording ? "recording" : running ? "acquiring" : "idle";

  return (
    <div className="app">
      <header>
        <h1>DT5742B DAQ</h1>
        <ConnectionBadge status={status} serverUp={serverUp}
          busy={reconnecting} onReconnect={reconnect} />
        {/* Acquisition lives beside the connection state, away from the
            Record cluster: enabling it watches, and only Record writes -
            keeping the two apart is what stops "for N ev" reading as an
            acquisition option. */}
        <div className="acq-group">
          {!running ? (
            <button className="primary" onClick={start} disabled={!connected}
              title={connected ? "Watch live — nothing is written to disk"
                               : "No unit connected"}>
              Enable Acquisition
            </button>
          ) : null}
          {/* Hidden while recording: disabling acquisition there would end the
              run, and "Stop recording" is the button you actually want. */}
          {running && !recording ? (
            <button onClick={stop}>Disable Acquisition</button>
          ) : null}
        </div>
        <span className={"pill state " + acqState}>{acqState}</span>
        <span className="pill mono">{tele?.events_seen ?? 0} events</span>
        <div className="spacer" />
        <div className="run-controls">
          <div className={"rec-group" + (recording ? " on" : "")}>
            {recording ? (
              <>
                <span className="rec-dot" />
                <span className="rec-name mono">{tele?.run_id ?? status?.run_id}</span>
                <span className="rec-count mono">
                  <Elapsed since={tele?.run_started ?? status?.run_started ?? null} />
                  {" · "}{tele?.recorded ?? 0} ev
                </span>
                <button className="danger" onClick={stopRec}>Stop recording</button>
              </>
            ) : (
              <>
                <label className="rec-label" htmlFor="runname">Run name</label>
                <input id="runname" className="rec-input" placeholder="e.g. cosmics" value={runName}
                  disabled={!connected}
                  onChange={(e) => setRunName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") setRecDialog(true); }} />
                <label className="rec-label" htmlFor="runno"
                  title="The analysis-facing run number (run_N.root). Prefilled with one past the highest number in the data directory; type to override.">
                  Run #
                </label>
                <input id="runno" className="rec-input rec-no" type="number" min={1}
                  placeholder={String(status?.next_run_number ?? "")}
                  value={runNo} disabled={!connected}
                  onChange={(e) => setRunNo(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") setRecDialog(true); }} />
                <label className="rec-label" htmlFor="recmax"
                  title="Stop the recording automatically after this many events. Blank = record until stopped. Acquisition keeps running either way.">
                  for
                </label>
                <input id="recmax" className="rec-input rec-no" type="number" min={1}
                  placeholder="&#8734; ev" value={recMax} disabled={!connected}
                  onChange={(e) => setRecMax(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") setRecDialog(true); }} />
                <label className="rec-stamp" title="Append the date and time, so runs of the same name never collide">
                  <input type="checkbox" checked={stampRun} disabled={!connected}
                    onChange={(e) => setStampRun(e.target.checked)} />
                  Include timestamp
                </label>
                <button className="record" onClick={() => setRecDialog(true)}
                  disabled={!connected}
                  title="Start writing this run to disk (asks for a run note first)">
                  <span className="rec-dot" />Record
                </button>
              </>
            )}
          </div>
        </div>
        <button className="help-btn" onClick={() => setTour(true)}
          title="Quick use" aria-label="Quick use">?</button>
      </header>

      <div className="body">
        <fieldset className="hw-lock" disabled={!connected}
          title={connected ? undefined : "No unit connected"}>
        <main>
          <div className="grid-head">
            <h2>Channels <span className="sub">
              {waveMode === "avg"
                ? `all 16 · avg ${tele?.avg_window_s ?? 1}s window · click a title to rename`
                : waveMode === "scope"
                ? `all 16 · newest single trace, full resolution · click a title to rename`
                : `all 16 · last ${PERSIST_TRACES} events, density-shaded · click a title to rename`}
            </span></h2>
            <div className="wave-mode" role="group" aria-label="Waveform display mode">
              <button className={waveMode === "avg" ? "on" : ""}
                title={`Rolling mean of the last ${tele?.avg_window_s ?? 1}s of events`}
                onClick={() => changeWaveMode("avg")}>Avg</button>
              <button className={waveMode === "overlay" ? "on" : ""}
                title={`The last ${PERSIST_TRACES} single events stacked, brightness = how often a path is taken`}
                onClick={() => changeWaveMode("overlay")}>Overlay</button>
              <button className={waveMode === "scope" ? "on" : ""}
                disabled={!connected}
                title="One full-resolution trace at a time, fed by free-running software triggers - for studying the noise on a line"
                onClick={() => changeWaveMode("scope")}>Scope</button>
              {waveMode === "scope" ? (
                <>
                  <label className="scope-rate" title="Software-trigger rate, 0.1-20 Hz">
                    <input type="number" min={0.1} max={20} step={0.1} value={scopeHz}
                      onChange={(e) => setScopeHz(e.target.value)}
                      onBlur={(e) => applyScope(e.target.value, scopeTrigCh, scopeTrigMv, scopeTrigEdge)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter")
                          applyScope((e.target as HTMLInputElement).value,
                                     scopeTrigCh, scopeTrigMv, scopeTrigEdge);
                      }} />
                    Hz
                  </label>
                  <span className="scope-trig"
                    title="Software display trigger: only events where this channel crosses the level (vs its own baseline) refresh the traces. The x742 has no hardware channel trigger, so this samples the line at the scope rate - features present in most windows show; rare pulses still need the signal on TR0.">
                    trig
                    <select value={scopeTrigCh}
                      onChange={(e) => {
                        setScopeTrigCh(e.target.value);
                        applyScope(scopeHz, e.target.value, scopeTrigMv, scopeTrigEdge);
                      }}>
                      <option value="">any</option>
                      {Array.from({ length: catalog.geometry.num_channels }, (_, i) => (
                        <option key={i} value={i}>CH {i}</option>
                      ))}
                      <option value={16}>TR0</option>
                    </select>
                    {scopeTrigCh !== "" ? (
                      <>
                        <input type="number" min={1} max={500} value={scopeTrigMv}
                          onChange={(e) => setScopeTrigMv(e.target.value)}
                          onBlur={(e) => applyScope(scopeHz, scopeTrigCh, e.target.value, scopeTrigEdge)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter")
                              applyScope(scopeHz, scopeTrigCh,
                                         (e.target as HTMLInputElement).value, scopeTrigEdge);
                          }} />
                        mV
                        <button
                          title={scopeTrigEdge === "falling"
                            ? "Triggering on a dip below baseline - click for rising"
                            : "Triggering on a rise above baseline - click for falling"}
                          onClick={() => {
                            const next = scopeTrigEdge === "falling" ? "rising" : "falling";
                            setScopeTrigEdge(next);
                            applyScope(scopeHz, scopeTrigCh, scopeTrigMv, next);
                          }}>
                          {scopeTrigEdge === "falling" ? "↘" : "↗"}
                        </button>
                      </>
                    ) : null}
                  </span>
                </>
              ) : null}
            </div>
            <div className="legend">
              <span className="lg live" title="Showing at least its noise floor - a quiet channel seeing only dark counts is live, not dead">live</span>
              <span className="lg dead" title="A single event flatter than any real noise floor: the electronics are silent - check the cable, not the source">dead</span>
              <span className="lg clip" title="The average touches an ADC rail - part of the signal is outside the window">clip</span>
              <span className="lg off">bank off</span>
            </div>
          </div>
          <ChannelGrid catalog={catalog} config={config} tele={tele}
            onDcOffset={(ch, dac) => updateChannel(ch, { dc_offset: dac })}
            onName={(ch, name) => updateChannel(ch, { name })}
            yRanges={yRanges} onYRange={changeYRange} waveMode={waveMode}
            clearEpoch={wipeEpoch} />
        </main>

        <aside>
          {(() => {
            // The digitized TR0 trace, when TR digitizing is on: the same
            // signal both groups see, shown once (group 0's copy, else 1's).
            const trCh = tele?.channels["16"] ? 16 : tele?.channels["17"] ? 17 : null;
            const tr = trCh != null ? tele!.channels[String(trCh)] : null;
            if (!config.fast_trigger_digitizing || !tr) return null;
            const trGroup = config.groups[trCh! - 16];
            // Baseline: the held measurement (see trBaseMem), predictively
            // shifted if the offset moved since; bench prediction only
            // before anything was ever measured.
            if (tr.baseline != null) {
              trBaseMem.current = { counts: tr.baseline,
                                    dac: trGroup.fast_trigger_dc_offset };
            }
            const mem = trBaseMem.current;
            const baseCounts = mem
              ? mem.counts + TR_OFF_SLOPE_COUNTS
                  * (trGroup.fast_trigger_dc_offset - mem.dac)
              : 2048 + (trGroup.fast_trigger_dc_offset - 32768) * TR_OFF_SLOPE_COUNTS;
            // Trigger: the comparator lives in the WINDOW frame - deduced
            // from the observed fact that moving the TR offset changes the
            // trigger margin - so its line is a function of the threshold
            // DAC alone (RADiCAL cal: 0.0329 mV/LSB, zero at 25448),
            // independent of the offset. Constants provisional until the
            // comparator-domain experiment refines them.
            const markers = [
              { v: windowVolts(baseCounts, catalog.geometry),
                label: "baseline", color: "#4ac776" },
              { v: (trGroup.fast_trigger_threshold - 25448) * 0.0329 / 1000,
                label: "trigger", color: "#f85149" },
            ];
            return (
              <div className="card">
                <h2>TR0 <span className="sub">fast trigger</span></h2>
                <MiniWave wave={tr.wave}
                  geom={catalog.geometry}
                  windowNs={tele ? tele.sample_period_ns * tele.record_length : undefined}
                  postTriggerPct={config.post_trigger}
                  color="#e3b341" height={110}
                  markers={markers}
                  yRange={yRanges[trCh!]}
                  onYRange={(range, all) => changeYRange(trCh!, range, all)}
                  mode={waveMode} lastWave={tr.last} lastId={tr.last_index}
                  clearEpoch={wipeEpoch} />
              </div>
            );
          })()}
          <div className="card">
            <h2>Trigger rate</h2>
            <RateStrip tele={tele} />
            <div className="test-trigger"
              title="Software triggers - the bench source when nothing external can trigger the board. Starts acquisition if it is not running.">
              <button onClick={fireTest} disabled={!connected}>Fire</button>
              <input type="number" min={1} value={testN}
                disabled={!connected}
                onChange={(e) => setTestN(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") fireTest(); }} />
              <span className="muted">test triggers @ 10 Hz</span>
              {status?.sw_triggers_pending ? (
                <span className="pending mono">{status.sw_triggers_pending} left</span>
              ) : null}
            </div>
          </div>
          <Collapsible title="Unit Settings" defaultOpen>
            <SettingsList defs={catalog.unit} geom={catalog.geometry}
              get={(k) => (config as any)[k]} onChange={updateBoard} />
          </Collapsible>
          <Collapsible title="TR0 Trigger" defaultOpen>
            {(() => {
              const offDefs = catalog.bank.filter((d) =>
                d.key === "fast_trigger_dc_offset");
              const [g0, g1] = config.groups;
              const diverged = ["fast_trigger_threshold", "fast_trigger_dc_offset"]
                .some((k) => (g0 as any)[k] !== (g1 as any)[k]);
              const relV = trRelThresholdV(
                g0.fast_trigger_threshold, g0.fast_trigger_dc_offset,
                catalog.geometry);
              return (
                <>
                  <div className="setting-row"
                    title={"Trigger level RELATIVE to the baseline - the way pulses are thought about (an MCP pulse of -100 mV wants a threshold around -50 to -80 mV). Kept relative automatically: moving the TR DC offset re-computes the absolute threshold so the margin never changes underneath you.\n\nCAEN_DGTZ_SetGroupFastTriggerThreshold"}>
                    <label>TR threshold <span className="muted">vs baseline</span></label>
                    <span className="field">
                      <BlurInput type="number" step={0.005} min={-0.5} max={0.5}
                        selectOnFocus value={relV.toFixed(3)}
                        onCommit={(v) => {
                          const rel = Math.min(0.5, Math.max(-0.5, Number(v) || 0));
                          updateTrBoth("fast_trigger_threshold",
                            trThresholdDacFor(rel, g0.fast_trigger_dc_offset,
                                              catalog.geometry));
                        }} />
                      <span className="unit">V</span>
                    </span>
                  </div>
                  <SettingsList defs={offDefs} geom={catalog.geometry}
                    get={(k) => (g0 as any)[k]} onChange={updateTrBoth} />
                  {diverged ? (
                    <div className="tr-diverged">
                      The two banks' TR0 registers differ (bank 1 has its own
                      values). Editing here writes both;{" "}
                      <button onClick={() => {
                        const groups = config.groups.map((gc) => ({
                          ...gc,
                          fast_trigger_threshold: g0.fast_trigger_threshold,
                          fast_trigger_dc_offset: g0.fast_trigger_dc_offset,
                        }));
                        pushConfig({ ...config, groups });
                      }}>sync bank 1 to bank 0</button>
                    </div>
                  ) : null}
                  <p className="muted">
                    One input, split to both banks; this panel writes both
                    together. The threshold is relative to the baseline and
                    follows the offset automatically.
                  </p>
                </>
              );
            })()}
          </Collapsible>
          <Collapsible title="Bank Settings" defaultOpen>
            <BankPanel catalog={catalog} config={config} onGroupChange={updateGroup} />
          </Collapsible>
          <CalibrationPanel
            connected={connected} recording={recording}
            onStarted={() => setWipeEpoch((e) => e + 1)}
            onError={(title, lines) => push("err", title, lines)}
            onFinished={async (st) => {
              // The server steered the DACs underneath the UI: re-adopt what
              // the board holds now, exactly as after any other write.
              const cfg = await api.getConfig();
              setConfig(cfg); confirmed.current = cfg;
              const bad = st.report.filter((r) => r.status !== "ok");
              if (st.error) push("err", "Calibration failed", [st.error]);
              else if (st.message === "cancelled") {
                push("warn", "Calibration cancelled",
                     ["The board keeps the last completed pass."]);
              } else if (bad.length) {
                push("warn", "Calibration finished with findings",
                     bad.map((r) => `${r.channel}: ${r.status}`));
              } else {
                push("ok", `Calibration done - ${st.report.length} channels ok`);
              }
            }} />
          <SessionsPanel
            recording={recording}
            onSaved={(name) => push("ok", `Session "${name}" saved`)}
            onError={(title, lines) => push("err", title, lines)}
            onApplied={(cfg, display, errors, connected, name) => {
              setConfig(cfg); confirmed.current = cfg;
              setYRanges(fromPrefs(display));
              setWaveMode(asWaveMode(display.wave_mode));
              if (!connected) {
                push("warn", `Session "${name}": display restored`,
                     ["No unit connected - hardware settings were not written."]);
              } else if (errors.length) {
                push("err", `Session "${name}" applied with errors`, errors);
              } else {
                push("ok", `Session "${name}" applied and read back from unit`);
              }
            }} />
          <ConfigPanel
            onReset={async () => {
              try {
                const r = await api.resetDefault();
                setConfig(r.config); confirmed.current = r.config;
                if (r.connected === false) {
                  push("warn", "No unit connected", ["Nothing was sent."]);
                } else if (r.errors?.length) {
                  push("err", "Unit rejected part of the reset", r.errors);
                } else {
                  push("ok", "Defaults applied and read back from unit");
                }
              } catch (e) {
                failed("Could not reset the settings")(e);
              }
            }}
            onLoaded={({ config: cfg, notes, errors, restart, connected: up, running }) => {
              // Adopt what the unit reported even when it refused something:
              // that IS its state now, and showing the old values instead would
              // be the one thing this app must never do.
              setConfig(cfg); confirmed.current = cfg;
              if (!up) {
                push("warn", "No unit connected", ["The file was read, but nothing was sent."]);
              } else if (errors.length) {
                push("err", "Unit rejected a setting from the file",
                     [...errors, ...notes]);
              } else {
                push(notes.length ? "warn" : "ok",
                     "Config loaded and read back from unit", notes);
              }
              if (restart.length && running) {
                const what = restart.join(", ");
                if (confirm(`${what} only take effect when the unit is re-armed.\n\nRestart acquisition now?`)) {
                  api.stop().then(() => api.start()).then(setStatus)
                    .catch(failed("Could not re-arm the unit"));
                }
              }
            }} />
          {status?.errors?.length ? (
            <div className="card errors">
              <h2>Errors</h2>
              {status.errors.slice(-6).map((e, i) => <div key={i} className="mono err">{e}</div>)}
            </div>
          ) : null}
          <RunsPanel status={status} refreshKey={runsKey} />
        </aside>
        </fieldset>
      </div>
      <Toasts toasts={toasts} onDismiss={dismiss} />
      {recDialog ? (
        <div className="modal-backdrop" onClick={() => setRecDialog(false)}>
          <div className="modal rec-modal" onClick={(e) => e.stopPropagation()}
            role="dialog" aria-label="Run notes">
            <h3>
              Run {runNo.trim() || status?.next_run_number || "?"}
              {runName.trim() ? ` · ${runName.trim()}` : ""}
              {recMax.trim() ? ` · ${recMax.trim()} events` : ""}
            </h3>
            <textarea autoFocus rows={4} maxLength={2000} value={recNote}
              placeholder="What is this run? Tested device, beam energy, HV, conditions..."
              onChange={(e) => setRecNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRec();
                if (e.key === "Escape") setRecDialog(false);
              }} />
            <p className="muted">
              Saved as "note" in run_metadata.json and shown in the run list.
              Ctrl+Enter records.
            </p>
            <div className="modal-btns">
              <button onClick={() => setRecDialog(false)}>Cancel</button>
              <button className="record" onClick={startRec}>
                <span className="rec-dot" />Record
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {tour ? <Tour steps={QUICK_USE} onClose={() => setTour(false)} /> : null}
    </div>
  );
}
