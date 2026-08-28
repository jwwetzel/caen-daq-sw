import { useCallback, useEffect, useState } from "react";
import type { Status } from "../types";

interface RunInfo {
  id: string; started: number;
  files: number; bytes: number; channels: number[]; events: number | null;
  /** The operator's note from record time - what was tested, beam energy. */
  note?: string;
}

/** Recorded runs: what is on the server, downloadable, and deletable.
 *  Deleting destroys data, so it takes a typed confirmation - a second click is
 *  too easy to give by reflex. */
export function RunsPanel({ status, refreshKey }: { status: Status | null; refreshKey: number }) {
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [dir, setDir] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/runs").then((x) => x.json());
      setRuns(r.runs ?? []);
      setDir(r.data_dir ?? "");
      setErr("");
    } catch {
      setErr("could not list runs");
    }
  }, []);

  useEffect(() => { load(); }, [load, refreshKey]);

  const remove = async (id: string) => {
    setBusy(true);
    try {
      const res = await fetch(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (res.ok) {
        setConfirming(null); setTyped(""); await load();
        return;
      }
      // Reading .detail off a body that is not JSON threw inside this try, and
      // the throw skipped every setErr - so a failed delete showed nothing at
      // all and read as a dead button.
      let detail = "";
      try { detail = (await res.json())?.detail ?? ""; } catch { /* not JSON */ }
      setErr(detail || `delete failed (${res.status} ${res.statusText})`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "could not reach the server");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card runs-card">
      <h2>Recorded Runs <span className="sub">{runs.length || "none"}</span></h2>
      {dir ? <p className="muted mono run-dir" title={dir}>{dir}</p> : null}
      {err ? <div className="config-msg err">{err}</div> : null}

      {runs.length === 0 ? (
        <p className="muted">Nothing recorded yet.</p>
      ) : (
        <ul className="runs">
          {runs.map((r) => {
            const live = status?.run_id === r.id;
            return (
              <li key={r.id} className={"run" + (live ? " live" : "")}>
                <div className="run-main">
                  {/* The directory name is the run's only name - the same
                      string the download and the metadata carry. */}
                  <span className="run-name mono">{r.id}</span>
                  <span className="run-meta mono">
                    {when(r.started)} · {r.channels.length} ch ·{" "}
                    {r.events != null ? `${r.events} ev` : "? ev"} · {size(r.bytes)}
                  </span>
                  {r.note ? (
                    <span className="run-note" title={r.note}>{r.note}</span>
                  ) : null}
                </div>
                {live ? (
                  <span className="run-live">recording</span>
                ) : (
                  <span className="run-actions">
                    <a className="mini btn" href={`/api/runs/${encodeURIComponent(r.id)}/download`}
                      download={`${r.id}.zip`} title="Download this run as a zip">Download</a>
                    <button className="mini danger" disabled={busy}
                      onClick={() => { setConfirming(r.id); setTyped(""); setErr(""); }}
                      title="Delete this run from the server">Delete</button>
                  </span>
                )}

                {confirming === r.id ? (
                  <div className="run-confirm">
                    <div>
                      Permanently delete <b className="mono">{r.id}</b> —{" "}
                      {r.events != null ? `${r.events} event${r.events === 1 ? "" : "s"}` : "an unknown number of events"}
                      {" "}across {r.files} file{r.files === 1 ? "" : "s"} ({size(r.bytes)}).
                      This cannot be undone.
                    </div>
                    <div className="run-confirm-row">
                      <span>Type <code>DELETE</code> to confirm:</span>
                      <input autoFocus value={typed} placeholder="DELETE"
                        onChange={(e) => setTyped(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && typed === "DELETE") remove(r.id);
                          if (e.key === "Escape") setConfirming(null);
                        }} />
                      <span className="spacer" />
                      <button className="mini" onClick={() => setConfirming(null)}>Cancel</button>
                      <button className="mini danger" disabled={typed !== "DELETE" || busy}
                        onClick={() => remove(r.id)}>Delete</button>
                    </div>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function size(b: number) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} kB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

function when(unix: number) {
  const d = new Date(unix * 1000);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
