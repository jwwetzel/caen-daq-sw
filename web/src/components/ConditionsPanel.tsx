import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Condition } from "../api";

interface Props {
  onError: (title: string, lines?: string[]) => void;
}

/** The operator's key=value experiment facts - beam energy, SiPM bias, which
 *  capillaries. The DAQ carries them and snapshots them BY VALUE into every
 *  run's metadata at record time; it never interprets them. Server-backed,
 *  so they survive restarts and every window sees the same list. */
export function ConditionsPanel({ onError }: Props) {
  const [items, setItems] = useState<Condition[]>([]);
  const [loaded, setLoaded] = useState(false);
  const saveTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    api.conditions()
      .then((r) => { setItems(r.items); setLoaded(true); })
      .catch(() => onError("Could not load the experiment conditions"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const push = (next: Condition[]) => {
    setItems(next);
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      // Blank-key rows are drafts; the server drops them, the form keeps
      // them until the operator fills or removes them.
      api.setConditions(next).catch(() =>
        onError("Could not save the experiment conditions"));
    }, 500);
  };

  const edit = (i: number, patch: Partial<Condition>) =>
    push(items.map((c, k) => (k === i ? { ...c, ...patch } : c)));
  const remove = (i: number) => push(items.filter((_, k) => k !== i));
  const add = () => push([...items, { key: "", value: "" }]);

  return (
    <div className="card">
      <h2>Experiment Conditions</h2>
      <p className="muted">
        key = value facts about this setup. Snapshotted into every run's
        metadata the moment you hit Record - editing them later never changes
        already-taken runs.
      </p>
      {!loaded ? <p className="muted">Loading…</p> : (
        <div className="cond-list">
          {items.map((c, i) => (
            <div className="cond-row" key={i}>
              <input className="cond-key" placeholder="e.g. Capillary 1"
                value={c.key} onChange={(e) => edit(i, { key: e.target.value })} />
              <span className="cond-eq">=</span>
              <input className="cond-val" placeholder="e.g. DSB1 1911"
                value={c.value} onChange={(e) => edit(i, { value: e.target.value })} />
              <button className="mini danger" title="Remove this row"
                onClick={() => remove(i)}>×</button>
            </div>
          ))}
          <button className="cond-add" onClick={add}>+ add condition</button>
        </div>
      )}
    </div>
  );
}
