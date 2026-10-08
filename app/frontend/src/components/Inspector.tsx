import { useEffect, useMemo, useRef, useState } from "react";
import { animate, stagger } from "animejs";
import type { ColumnDetail, EntityState, TableDetail } from "../api";
import { api } from "../api";
import { health, healthColor, pct, when } from "../theme";
import { CloseButton, Coverage, HistoryIcon, Info, StatusChip, useSlideIn } from "./bits";

const TABLE_KEY = "__table__";

const TYPE_LABEL: Record<string, string> = {
  MANAGED: "Table",
  EXTERNAL: "External table",
  VIEW: "View",
  MATERIALIZED_VIEW: "Materialized view",
  STREAMING_TABLE: "Streaming table",
};

interface EditorProps {
  detail: TableDetail;
  target: number;
  historyKey: string | null;
  onHistory: (column: string | null) => void;
  onSubmitted: (n: number) => void;
  onClose: () => void;
}

export function TableEditor({ detail, target, historyKey, onHistory, onSubmitted, onClose }: EditorProps) {
  const ref = useSlideIn<HTMLDivElement>("left", [detail.catalog, detail.schema, detail.name]);
  const listRef = useRef<HTMLDivElement>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "missing">("all");

  useEffect(() => {
    setDrafts({});
    setNote("");
    setError(null);
  }, [detail.catalog, detail.schema, detail.name]);

  useEffect(() => {
    const rows = listRef.current?.querySelectorAll(".field");
    if (rows?.length)
      animate(rows, { opacity: [0, 1], translateY: [8, 0], delay: stagger(18), duration: 420, ease: "outQuad" });
  }, [detail.name, filter]);

  // What the user is looking at: their draft, else the pending proposal, else live.
  const shown = (key: string, live: string | null, e: EntityState) =>
    drafts[key] ?? e.open_request?.proposed_value ?? live ?? "";
  const baseline = (live: string | null, e: EntityState) => e.open_request?.proposed_value ?? live ?? "";

  const dirty = useMemo(() => {
    const out: { key: string; value: string }[] = [];
    const tBase = baseline(detail.comment, detail);
    if (drafts[TABLE_KEY] !== undefined && drafts[TABLE_KEY].trim() !== tBase.trim())
      out.push({ key: TABLE_KEY, value: drafts[TABLE_KEY] });
    for (const c of detail.columns) {
      const d = drafts[c.column_name];
      if (d !== undefined && d.trim() !== baseline(c.comment, c).trim()) out.push({ key: c.column_name, value: d });
    }
    return out;
  }, [drafts, detail]);

  const documentedCols = detail.columns.filter((c) => c.comment?.trim()).length;
  const coverage = (documentedCols + (detail.comment?.trim() ? 1 : 0)) / (detail.columns.length + 1);
  const columns =
    filter === "missing" ? detail.columns.filter((c) => !c.comment?.trim() && !c.open_request) : detail.columns;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.submit(
        dirty.map((d) => ({
          catalog: detail.catalog,
          schema: detail.schema,
          table: detail.name,
          column: d.key === TABLE_KEY ? null : d.key,
          proposed_value: d.value,
        })),
        note,
      );
      setDrafts({});
      setNote("");
      onSubmitted(res.submitted);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="panel left" ref={ref}>
      <header className="panel-head">
        <div>
          <div className="eyebrow">
            {detail.catalog}.{detail.schema}
          </div>
          <h2 className="mono-title">{detail.name}</h2>
          <div className="meta-row">
            <span className="pill">{TYPE_LABEL[detail.table_type] ?? detail.table_type}</span>
            {detail.table_owner && <span className="muted">owner {detail.table_owner}</span>}
          </div>
        </div>
        <CloseButton onClick={onClose} />
      </header>

      <Coverage value={coverage} target={target} />
      {(detail.table_type === "MATERIALIZED_VIEW" || detail.table_type === "STREAMING_TABLE") && (
        <div className="callout">
          Pipeline-managed. Changes apply, but the pipeline's own definition wins if it is redeployed.
          <Info>
            Descriptions on materialized views and streaming tables survive refreshes. If the pipeline source
            sets a comment, the next redeploy overwrites what was applied here. The pipeline owner should copy
            approved text into the definition; Semantic Builder flags any reversion as drift.
          </Info>
        </div>
      )}

      <div className="panel-body" ref={listRef}>
        <Field
          label="Description"
          sub="What is this table, and when should someone use it?"
          value={shown(TABLE_KEY, detail.comment, detail)}
          live={detail.comment}
          entity={detail}
          dirty={dirty.some((d) => d.key === TABLE_KEY)}
          active={historyKey === TABLE_KEY}
          big
          onChange={(v) => setDrafts((d) => ({ ...d, [TABLE_KEY]: v }))}
          onHistory={() => onHistory(null)}
        />

        <div className="section-head">
          <span>Columns</span>
          <span className="muted mono">
            {documentedCols}/{detail.columns.length}
          </span>
          <div className="seg">
            <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>
              All
            </button>
            <button className={filter === "missing" ? "on" : ""} onClick={() => setFilter("missing")}>
              Missing
            </button>
          </div>
        </div>

        {columns.map((c: ColumnDetail) => (
          <Field
            key={c.column_name}
            label={c.column_name}
            sub={c.data_type}
            mono
            value={shown(c.column_name, c.comment, c)}
            live={c.comment}
            entity={c}
            dirty={dirty.some((d) => d.key === c.column_name)}
            active={historyKey === c.column_name}
            onChange={(v) => setDrafts((d) => ({ ...d, [c.column_name]: v }))}
            onHistory={() => onHistory(c.column_name)}
          />
        ))}
        {filter === "missing" && columns.length === 0 && <div className="empty">Every column has a description.</div>}
      </div>

      <footer className={`panel-foot ${dirty.length ? "show" : ""}`}>
        {error && <div className="error">{error}</div>}
        <input
          className="note"
          placeholder="Why are you making this change? (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="foot-row">
          <button className="ghost" onClick={() => setDrafts({})} disabled={!dirty.length || busy}>
            Discard
          </button>
          <button className="primary" onClick={submit} disabled={!dirty.length || busy}>
            {busy ? "Submitting…" : `Submit ${dirty.length} for review`}
          </button>
        </div>
      </footer>
    </aside>
  );
}

function Field(props: {
  label: string;
  sub: string;
  value: string;
  live: string | null;
  entity: EntityState;
  dirty: boolean;
  active: boolean;
  mono?: boolean;
  big?: boolean;
  onChange: (v: string) => void;
  onHistory: () => void;
}) {
  const { label, sub, value, live, entity, dirty, active, mono, big, onChange, onHistory } = props;
  const ta = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ta.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    }
  }, [value]);
  const open = entity.open_request;
  const missing = !live?.trim() && !open && !dirty;
  return (
    <div className={`field ${missing ? "missing" : ""} ${dirty ? "dirty" : ""} ${active ? "active" : ""} ${big ? "big" : ""}`}>
      <div className="field-head">
        <span className={mono ? "field-label mono" : "field-label"}>{label}</span>
        <span className="field-sub mono">{sub}</span>
        <span className="spacer" />
        {open && <StatusChip status={open.status} />}
        {entity.drifted && (
          <span className="chip st-drift" title={`Last applied: ${entity.last_applied_value ?? "(empty)"}`}>
            Changed outside
          </span>
        )}
        <button className={`icon-btn ${active ? "on" : ""}`} onClick={onHistory} title="Change history">
          <HistoryIcon />
        </button>
      </div>
      <textarea
        ref={ta}
        rows={1}
        value={value}
        placeholder={missing ? "No description yet" : ""}
        onChange={(e) => onChange(e.target.value)}
      />
      {open && !dirty && (
        <div className="field-foot muted">
          proposed by {open.requested_by.split("@")[0]} {when(open.requested_at)}
          {open.apply_message && open.status === "APPROVED" && <> · {open.apply_message}</>}
        </div>
      )}
    </div>
  );
}

interface Child {
  name: string;
  coverage: number;
  open_changes: number;
  sub: string;
}

/** Catalog / schema summary: the children that need attention first. */
export function Overview({
  kind,
  title,
  eyebrow,
  comment,
  coverage,
  target,
  children,
  childNoun,
  onPick,
  onClose,
}: {
  kind: "catalog" | "schema";
  title: string;
  eyebrow: string;
  comment: string | null;
  coverage: number;
  target: number;
  children: Child[];
  childNoun: string;
  onPick: (name: string) => void;
  onClose: () => void;
}) {
  const ref = useSlideIn<HTMLDivElement>("left", [title]);
  const sorted = [...children].sort((a, b) => a.coverage - b.coverage || a.name.localeCompare(b.name));
  const attention = sorted.filter((c) => c.coverage < target).length;
  return (
    <aside className="panel left" ref={ref}>
      <header className="panel-head">
        <div>
          <div className="eyebrow">{eyebrow || kind}</div>
          <h2 className="mono-title">{title}</h2>
          {comment && <p className="lede">{comment}</p>}
        </div>
        <CloseButton onClick={onClose} />
      </header>
      <Coverage value={coverage} target={target} />
      <div className="section-head">
        <span>
          {attention ? `${attention} of ${children.length} ${childNoun} need attention` : `All ${childNoun} documented`}
        </span>
        <Info>
          A {childNoun.replace(/s$/, "")} needs attention when fewer than {pct(target)} of its descriptions
          (table description plus columns) are filled in.
        </Info>
      </div>
      <div className="panel-body">
        {sorted.map((c) => (
          <button key={c.name} className="row-btn" onClick={() => onPick(c.name)}>
            <span className="dot" style={{ background: healthColor[health(c.coverage, target)] }} />
            <span className="row-name mono">{c.name}</span>
            {c.open_changes > 0 && <span className="mini-badge">{c.open_changes}</span>}
            <span className="row-sub muted">{c.sub}</span>
            <span className="row-pct mono">{pct(c.coverage)}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}
