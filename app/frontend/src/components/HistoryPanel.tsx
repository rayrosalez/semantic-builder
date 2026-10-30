import { useEffect, useRef, useState } from "react";
import { animate, stagger } from "animejs";
import { api, type ChangeRequest } from "../api";
import { when } from "../theme";
import { applyNote, Avatar, CloseButton, StatusChip, useApplySchedule, useSlideIn } from "./bits";

export interface HistoryTarget {
  catalog: string;
  schema: string;
  table: string;
  column: string | null;
  live: string | null;
}

/** Word-level diff (LCS) — enough for descriptions, which are short. */
export function diffWords(a: string, b: string): { t: string; op: "same" | "del" | "add" }[] {
  const x = a.split(/(\s+)/).filter(Boolean);
  const y = b.split(/(\s+)/).filter(Boolean);
  const m = x.length;
  const n = y.length;
  if (m * n > 250_000) return [{ t: a, op: "del" }, { t: b, op: "add" }];
  const dp = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--)
    for (let j = n - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: { t: string; op: "same" | "del" | "add" }[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (x[i] === y[j]) out.push({ t: x[i++], op: "same" }), j++;
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ t: x[i++], op: "del" });
    else out.push({ t: y[j++], op: "add" });
  }
  while (i < m) out.push({ t: x[i++], op: "del" });
  while (j < n) out.push({ t: y[j++], op: "add" });
  return out;
}

export function Diff({ from, to }: { from: string | null; to: string | null }) {
  if (!from?.trim()) return <div className="diff"><span className="add">{to || "(empty)"}</span></div>;
  return (
    <div className="diff">
      {diffWords(from, to ?? "").map((p, i) => (
        <span key={i} className={p.op}>
          {p.t}
        </span>
      ))}
    </div>
  );
}

export function HistoryPanel({ target, refreshKey, onClose }: { target: HistoryTarget; refreshKey: number; onClose: () => void }) {
  const ref = useSlideIn<HTMLDivElement>("right", [target.table, target.column]);
  const listRef = useRef<HTMLOListElement>(null);
  const [items, setItems] = useState<ChangeRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const schedule = useApplySchedule();

  useEffect(() => {
    setItems(null);
    setError(null);
    api
      .history(target.catalog, target.schema, target.table, target.column)
      .then(setItems)
      .catch((e) => setError(e.message));
  }, [target.catalog, target.schema, target.table, target.column, refreshKey]);

  useEffect(() => {
    const els = listRef.current?.querySelectorAll(".tl-item");
    if (els?.length) animate(els, { opacity: [0, 1], translateX: [16, 0], delay: stagger(60), duration: 500, ease: "outExpo" });
  }, [items]);

  return (
    <aside className="panel right" ref={ref}>
      <header className="panel-head">
        <div>
          <div className="eyebrow">History</div>
          <h2 className="mono-title">{target.column ?? target.table}</h2>
          <div className="muted mono small">
            {target.catalog}.{target.schema}.{target.table}
            {target.column ? `.${target.column}` : ""}
          </div>
        </div>
        <CloseButton onClick={onClose} />
      </header>

      <div className="current">
        <div className="eyebrow">Live in Unity Catalog</div>
        <p className={target.live?.trim() ? "" : "muted"}>{target.live?.trim() || "No description"}</p>
      </div>

      <div className="panel-body">
        {error && <div className="error">{error}</div>}
        {items && items.length === 0 && <div className="empty">No changes have been requested here yet.</div>}
        <ol className="timeline" ref={listRef}>
          {items?.map((r) => (
            <li key={r.request_id} className={`tl-item st-${r.status.toLowerCase()}`}>
              <div className="tl-rail">
                <Avatar email={r.requested_by} />
              </div>
              <div className="tl-card">
                <div className="tl-head">
                  <b>{r.requested_by.split("@")[0]}</b>
                  <span className="muted">proposed · {when(r.requested_at)}</span>
                  <span className="spacer" />
                  <StatusChip status={r.status} />
                </div>
                <Diff from={r.previous_value} to={r.proposed_value} />
                {r.note && <div className="tl-note">"{r.note}"</div>}
                <ul className="tl-events">
                  {r.reviewed_by && (
                    <li>
                      <span className={`ev-dot ${r.status === "REJECTED" ? "red" : "blue"}`} />
                      {r.status === "REJECTED" ? "Rejected" : "Approved"} by {r.reviewed_by.split("@")[0]}{" "}
                      <span className="muted">{when(r.reviewed_at)}</span>
                      {r.review_note && <div className="tl-note">"{r.review_note}"</div>}
                    </li>
                  )}
                  {!!r.deferrals && (
                    <li>
                      <span className="ev-dot gold" />
                      Deferred {r.deferrals}× while the table was busy
                    </li>
                  )}
                  {r.force_apply && r.status === "APPROVED" && (
                    <li>
                      <span className="ev-dot brown" />
                      Marked to apply even if busy
                    </li>
                  )}
                  {r.applied_at && (
                    <li>
                      <span className="ev-dot blue" />
                      Applied to Unity Catalog <span className="muted">{when(r.applied_at)}</span>
                    </li>
                  )}
                  {(() => {
                    const note = applyNote(r, schedule);
                    return note && note.text !== "Applied" ? (
                      <li className="muted small" title={note.title}>
                        <span className={`ev-dot ${r.status === "FAILED" ? "red" : "fog"}`} />
                        {note.text}
                      </li>
                    ) : null;
                  })()}
                </ul>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </aside>
  );
}
