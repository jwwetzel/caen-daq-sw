import type { BoardConfig, Catalog, Status, Telemetry } from "./types";

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    // FastAPI puts the reason in `detail`; "500 Internal Server Error" on its
    // own tells the operator nothing they can act on.
    let detail = "";
    try {
      detail = (await r.json())?.detail ?? "";
    } catch { /* not JSON - the status line is all there is */ }
    throw new Error(detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

export interface ConfigResult {
  ok: boolean;
  config: BoardConfig;
  errors: string[];
  connected: boolean;
  /** The server's config revision after this call - the tab's new base. */
  config_rev?: number;
  /** True when the write was refused because the tab's config predates the
   *  server's current state; `config` then carries the current truth. */
  stale?: boolean;
}

export const api = {
  status: () => fetch("/api/status").then(j<Status>),
  catalog: () => fetch("/api/catalog").then(j<Catalog>),
  getConfig: () => fetch("/api/config").then(j<BoardConfig>),
  setConfig: (cfg: BoardConfig, baseRev?: number) =>
    fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...cfg, base_rev: baseRev ?? null }),
    }).then(j<ConfigResult>),
  resetDefault: () =>
    fetch("/api/config/default", { method: "POST" }).then(j<ConfigResult>),
  reconnect: () => fetch("/api/board/reconnect", { method: "POST" }).then(j<Status>),
  start: () =>
    fetch("/api/acq/start", { method: "POST" }).then(j<Status & { started: boolean }>),
  recStart: (name: string, timestamp: boolean, runNumber?: number | null,
             maxEvents?: number | null, note?: string, intoExisting?: boolean) =>
    fetch("/api/rec/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, timestamp, run_number: runNumber ?? null,
                             max_events: maxEvents ?? null, note: note ?? "",
                             into_existing: intoExisting ?? false }),
    }).then(j<{ ok: boolean; error?: string; run?: string; status: Status }>),
  runs: () =>
    fetch("/api/runs").then(j<{ runs: { id: string }[]; data_dir: string }>),
  recStop: () =>
    fetch("/api/rec/stop", { method: "POST" })
      .then(j<{ ok: boolean; error?: string; run?: string; status: Status }>),
  stop: () => fetch("/api/acq/stop", { method: "POST" }).then(j<Status>),
  trigger: (count: number, rateHz: number) =>
    fetch("/api/trigger", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count, rate_hz: rateHz }),
    }).then(j<{ ok: boolean; error?: string; queued?: number; status: Status }>),

  calibrate: (mode: "baseline" | "fit", events?: number | null) =>
    fetch(`/api/calibrate/${mode}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: events ?? null }),
    }).then(j<{ ok: boolean; status: Status }>),
  calibrateStatus: () =>
    fetch("/api/calibrate").then(j<CalibrationStatus>),
  calibrateCancel: () =>
    fetch("/api/calibrate/cancel", { method: "POST" }).then(j<{ ok: boolean }>),
  scope: (on: boolean, rateHz?: number, trigger?: ScopeTrigger | null) =>
    fetch("/api/scope", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on, rate_hz: rateHz ?? 2, trigger: trigger ?? null }),
    }).then(j<{ ok: boolean; error?: string; scope_hz: number | null;
                scope_trigger: ScopeTrigger | null; status: Status }>),

  getDisplay: () => fetch("/api/display").then(j<DisplayPrefs>),
  setDisplay: (d: DisplayPrefs) =>
    fetch("/api/display", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(d),
    }).then(j<{ ok: boolean }>),
  listSessions: () =>
    fetch("/api/sessions").then(j<{ sessions: SessionInfo[] }>),
  saveSession: (name: string) =>
    fetch(`/api/sessions/${encodeURIComponent(name)}`, { method: "POST" })
      .then(j<{ ok: boolean; name: string; saved_at: number }>),
  applySession: (name: string) =>
    fetch(`/api/sessions/${encodeURIComponent(name)}/apply`, { method: "POST" })
      .then(j<{ ok: boolean; config: BoardConfig; display: DisplayPrefs;
                errors: string[]; connected: boolean }>),
  deleteSession: (name: string) =>
    fetch(`/api/sessions/${encodeURIComponent(name)}`, { method: "DELETE" })
      .then(j<{ ok: boolean }>),
};

export interface SessionInfo { name: string; saved_at: number | null; }

export interface CalibrationRow {
  channel: string;
  dac: number;
  baseline_mv: number | null;
  below_mv: number;
  above_mv: number;
  status: "ok" | "adjusting" | "unreachable" | "no_fit" | "clipped";
}

export interface CalibrationStatus {
  active: boolean;
  phase: "baseline" | "fit" | null;
  message: string;
  iteration: number;
  report: CalibrationRow[];
  error: string | null;
}

/** The scope's software display trigger: only events where this channel's
 *  trace crosses level_mv (relative to its own median baseline) refresh the
 *  display. The x742 has NO hardware channel trigger - every channel-trigger
 *  call answers -17 - so this filters the randomly-sampled windows; rare
 *  pulses still need the signal physically routed into TR0. */
export interface ScopeTrigger {
  channel: number;
  level_mv: number;
  edge: "rising" | "falling";
}

/** The channel plots' display mode: "avg" (rolling average), "overlay"
 *  (persistence density), or "scope" (the newest single trace alone,
 *  replaced event by event - the line-noise debugging view). */
export type WaveMode = "avg" | "overlay" | "scope";

/** UI state that persists across restarts, keyed however the UI likes.
 *  y_ranges: per-channel waveform display range in volts, [min, max]. */
export interface DisplayPrefs {
  y_ranges?: Record<string, [number, number]>;
  wave_mode?: WaveMode;
}

/** Subscribe to telemetry; auto-reconnects. Returns an unsubscribe fn. */
export function openTelemetry(onData: (t: Telemetry) => void): () => void {
  let ws: WebSocket | null = null;
  let retry: number | undefined;
  let closed = false;
  const connect = () => {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
    ws.onmessage = (e) => {
      try {
        onData(JSON.parse(e.data));
      } catch (err) {
        console.error("unreadable telemetry frame", err);
      }
    };
    ws.onclose = () => { if (!closed) retry = window.setTimeout(connect, 1000); };
    ws.onerror = () => ws?.close();
  };
  connect();
  // Cancel the pending reconnect too. Without this, unsubscribing between a
  // close and its retry left a second socket open with no handle on it - which
  // React's development double-mount does on every page load.
  return () => { closed = true; window.clearTimeout(retry); ws?.close(); };
}
