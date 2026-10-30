import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { animate, stagger } from "animejs";
import { api, type AppConfig, type ApplyRun, type Batch, type ChangeRequest, type Me } from "../api";
import { when } from "../theme";
import { applyNote, Avatar, CloseButton, formatWindow, Info, StatusChip, useApplySchedule, useSlideIn } from "./bits";
import { Diff } from "./HistoryPanel";

type Tab = "review" | "queued" | "applied" | "closed" | "mine";

const TABS: { key: Tab; label: string; status: string; mine?: boolean }[] = [
  { key: "review", label: "To review", status: "PENDING" },
  { key: "queued", label: "Queued", status: "APPROVED" },
  { key: "applied", label: "Applied", status: "APPLIED" },
  { key: "closed", label: "Rejected & failed", status: "REJECTED,FAILED" },
  { key: "mine", label: "Mine", status: "", mine: true },
];

interface Props {
  me: Me;
  config: AppConfig;
  counts: Record<string, number>;
  onChanged: () => void;
  onOpen: (r: ChangeRequest) => void;
  onClose: () => void;
}

export function ReviewQueue({ me, config, counts, onChanged, onOpen, onClose }: Props) {
  const ref = useSlideIn<HTMLDivElement>("right");
  const listRef = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState<Tab>(me.is_approver ? "review" : "mine");
  const [items, setItems] = useState<ChangeRequest[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [runs, setRuns] = useState<ApplyRun[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [batchFilter, setBatchFilter] = useState<string | null>(null);
  const schedule = useApplySchedule();
  const windowMode = schedule?.mode === "window";
  const nextWindow = formatWindow(schedule);

  const load = useCallback(() => {
    const t = TABS.find((x) => x.key === tab)!;
    setItems(null);
    api
      .changes(t.status, !!t.mine, tab === "review" ? (batchFilter ?? undefined) : undefined)
      .then(setItems)
      .catch((e) => setMsg(e.message));
    if (tab === "review") api.batches().then(setBatches).catch(() => setBatches([]));
    if (tab === "queued" || tab === "applied") api.applyRuns().then(setRuns).catch(() => setRuns([]));
  }, [tab, batchFilter]);

  useEffect(() => {
    setSelected(new Set());
    load();
  }, [load]);

  useEffect(() => {
    const els = listRef.current?.querySelectorAll(".q-group");
    if (els?.length) animate(els, { opacity: [0, 1], translateY: [10, 0], delay: stagger(40), duration: 450, ease: "outExpo" });
  }, [items]);

  const groups = useMemo(() => {
    const g = new Map<string, ChangeRequest[]>();
    for (const r of items ?? []) {
      const k = `${r.catalog_name}.${r.schema_name}.${r.table_name}`;
      g.set(k, [...(g.get(k) ?? []), r]);
    }
    return [...g.entries()];
  }, [items]);

  const canReview = (r: ChangeRequest) =>
    me.is_approver && r.status === "PENDING" && (config.allow_self_approval || r.requested_by !== me.email);

  async function act(fn: () => Promise<unknown>, done: string) {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg(done);
      setSelected(new Set());
      setNote("");
      load();
      onChanged();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // A batch is one save in the app, or one dictionary import (batch_id = plan_id).
  // Single-request batches are reviewed like any other request.
  const bigBatches = batches.filter((b) => b.requests > 1);
  const canReviewBatch = (b: Batch) => me.is_approver && (config.allow_self_approval || b.requested_by !== me.email);

  function reviewBatch(b: Batch, decision: "approve" | "reject") {
    const verb = decision === "approve" ? "Approve" : "Reject";
    if (!window.confirm(`${verb} all ${b.requests.toLocaleString()} pending requests in ${batchLabel(b)}?`)) return;
    act(async () => {
      const r = await api.reviewBatch(b.batch_id, decision, note);
      setBatchFilter(null);
      return r;
    }, decision === "approve" ? `Approved ${b.requests.toLocaleString()} requests — queued for the apply job.` : "Batch rejected.");
  }

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const lastRun = runs[0];

  return (
    <aside className="panel right wide" ref={ref}>
      <header className="panel-head">
        <div>
          <div className="eyebrow">Change queue</div>
          <h2>Review &amp; apply</h2>
        </div>
        <CloseButton onClick={onClose} />
      </header>

      <nav className="tabs">
        {TABS.map((t) => {
          const n = t.mine ? undefined : t.status.split(",").reduce((a, s) => a + (counts[s] ?? 0), 0);
          return (
            <button key={t.key} className={tab === t.key ? "on" : ""} onClick={() => setTab(t.key)}>
              {t.label}
              {!!n && <span className="tab-n">{n}</span>}
            </button>
          );
        })}
      </nav>

      {(tab === "queued" || tab === "applied") && (
        <div className="apply-bar">
          <div>
            <div className="eyebrow">
              Apply job
              <Info>
                {windowMode
                  ? "Approved changes are applied in a scheduled maintenance window. The job pauses the jobs that " +
                    "write each table, waits for runs already in progress, applies, then restores every job to how " +
                    "it was. All of a table's edits land in a single commit."
                  : "Approved changes are applied on a schedule. For each table the job checks lineage for jobs or " +
                    "pipelines that write to it and waits while any are running, so a description change never " +
                    "collides with a write. All of a table's edits land in a single commit."}
              </Info>
            </div>
            {windowMode && (
              <div className="small">
                {nextWindow ? <>Next window: {nextWindow}</> : <span className="muted">Window schedule is paused</span>}
              </div>
            )}
            <div className="small">
              {lastRun ? (
                <>
                  Last run{" "}
                  <a href={lastRun.url} target="_blank" rel="noreferrer">
                    {lastRun.result_state?.toLowerCase() ?? lastRun.life_cycle_state?.toLowerCase()}
                  </a>{" "}
                  · {when(new Date(lastRun.start_time).toISOString())}
                </>
              ) : (
                <span className="muted">No runs yet</span>
              )}
            </div>
          </div>
          {me.is_approver && config.apply_job_id && (
            <button
              className="secondary"
              disabled={busy || lastRun?.life_cycle_state === "RUNNING" || lastRun?.life_cycle_state === "PENDING"}
              onClick={() => {
                if (
                  windowMode &&
                  !window.confirm(
                    "Run the maintenance window now? It pauses the jobs that write these tables until the changes are applied, then restores them.",
                  )
                )
                  return;
                act(() => api.applyNow(), "Apply job started — refresh in a minute to see results.");
              }}
            >
              Run now
            </button>
          )}
        </div>
      )}

      {msg && <div className="toast-inline">{msg}</div>}

      <div className="panel-body" ref={listRef}>
        {tab === "review" && bigBatches.length > 0 && (
          <section className="batches">
            {bigBatches.map((b) => (
              <div key={b.batch_id} className={`batch-card ${batchFilter === b.batch_id ? "on" : ""}`}>
                <div className="batch-main">
                  <div className="batch-title">{batchLabel(b)}</div>
                  <div className="q-meta muted">
                    {b.requests.toLocaleString()} pending · {b.tables.toLocaleString()} table{b.tables === 1 ? "" : "s"}
                    {" · "}
                    {b.columns.toLocaleString()} column{b.columns === 1 ? "" : "s"}
                    {b.table_descriptions > 0 && <> · {b.table_descriptions.toLocaleString()} table descriptions</>}
                    {" · "}
                    {when(b.first_requested_at)}
                  </div>
                  <div className="q-actions">
                    <button className="link-btn" onClick={() => setBatchFilter(batchFilter === b.batch_id ? null : b.batch_id)}>
                      {batchFilter === b.batch_id ? "Show all requests" : "Show only this batch"}
                    </button>
                  </div>
                </div>
                {me.is_approver && (
                  <div className="batch-buttons">
                    <button
                      className="danger"
                      disabled={busy || !canReviewBatch(b)}
                      title={canReviewBatch(b) ? "" : "You can't approve your own requests"}
                      onClick={() => reviewBatch(b, "reject")}
                    >
                      Reject batch
                    </button>
                    <button
                      className="primary"
                      disabled={busy || !canReviewBatch(b)}
                      title={canReviewBatch(b) ? "" : "You can't approve your own requests"}
                      onClick={() => reviewBatch(b, "approve")}
                    >
                      Approve batch
                    </button>
                  </div>
                )}
              </div>
            ))}
          </section>
        )}
        {items === null && <div className="loading">Loading…</div>}
        {items?.length === 0 && <div className="empty">Nothing here.</div>}
        {groups.map(([table, reqs]) => (
          <section key={table} className="q-group">
            <div className="q-table mono">
              <button className="link-btn" onClick={() => onOpen(reqs[0])}>
                {table}
              </button>
              {tab === "review" && me.is_approver && (
                <button
                  className="link-btn small"
                  onClick={() =>
                    setSelected((s) => {
                      const n = new Set(s);
                      reqs.filter(canReview).forEach((r) => n.add(r.request_id));
                      return n;
                    })
                  }
                >
                  select all
                </button>
              )}
            </div>
            {reqs.map((r) => (
              <div key={r.request_id} className={`q-item ${selected.has(r.request_id) ? "sel" : ""}`}>
                {tab === "review" && (
                  <input
                    type="checkbox"
                    disabled={!canReview(r)}
                    checked={selected.has(r.request_id)}
                    onChange={() => toggle(r.request_id)}
                    title={canReview(r) ? "" : "You can't approve your own request"}
                  />
                )}
                <div className="q-main">
                  <div className="q-head">
                    <span className="mono">{r.column_name ?? "table description"}</span>
                    <span className="spacer" />
                    {tab === "mine" && <StatusChip status={r.status} />}
                  </div>
                  <Diff from={r.previous_value} to={r.proposed_value} />
                  <div className="q-meta">
                    <Avatar email={r.requested_by} size={18} />
                    {r.requested_by.split("@")[0]} · {when(r.requested_at)}
                    {r.note && <span className="tl-note"> "{r.note}"</span>}
                  </div>
                  {r.reviewed_by && r.status !== "PENDING" && (
                    <div className="q-meta muted">
                      {r.status === "REJECTED" ? "Rejected" : "Approved"} by {r.reviewed_by.split("@")[0]}{" "}
                      {when(r.reviewed_at)}
                      {r.review_note && <> · "{r.review_note}"</>}
                    </div>
                  )}
                  {(() => {
                    const n = applyNote(r, schedule);
                    return n ? (
                      <div className={`q-msg ${r.status === "FAILED" ? "bad" : ""}`} title={n.title}>
                        {n.text}
                      </div>
                    ) : null;
                  })()}
                  <div className="q-actions">
                    {r.status === "APPROVED" && me.is_approver && !!r.deferrals && !r.force_apply && (
                      <button className="link-btn" disabled={busy} onClick={() => act(() => api.force(r.request_id), "Will apply on the next run even if the table is busy.")}>
                        Apply even if busy
                      </button>
                    )}
                    {r.status === "FAILED" && me.is_approver && (
                      <button className="link-btn" disabled={busy} onClick={() => act(() => api.retry(r.request_id), "Re-queued.")}>
                        Retry
                      </button>
                    )}
                    {r.status === "PENDING" && r.requested_by === me.email && (
                      <button className="link-btn" disabled={busy} onClick={() => act(() => api.withdraw(r.request_id), "Withdrawn.")}>
                        Withdraw
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </section>
        ))}
      </div>

      {tab === "review" && me.is_approver && (
        <footer className={`panel-foot ${selected.size ? "show" : ""}`}>
          <input className="note" placeholder="Review note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="foot-row">
            <span className="muted small">{selected.size} selected</span>
            <span className="spacer" />
            <button className="danger" disabled={!selected.size || busy} onClick={() => act(() => api.review([...selected], "reject", note), "Rejected.")}>
              Reject
            </button>
            <button className="primary" disabled={!selected.size || busy} onClick={() => act(() => api.review([...selected], "approve", note), "Approved — queued for the apply job.")}>
              Approve
            </button>
          </div>
        </footer>
      )}
    </aside>
  );
}

/** "Dictionary import · week_of 2026-10-26", or who saved the batch in the app. */
function batchLabel(b: Batch): string {
  if (b.requested_by === "dictionary-import") {
    return `Dictionary import · ${(b.note ?? "").replace(/^Dictionary import,\s*/, "") || b.batch_id}`;
  }
  return `Saved together by ${b.requested_by.split("@")[0]}`;
}
