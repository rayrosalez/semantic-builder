import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { animate } from "animejs";
import {
  api,
  type AppConfig,
  type CatalogNode,
  type ChangeRequest,
  type Me,
  type SchemaNode,
  type TableDetail,
  type TableNode,
} from "./api";
import { Graph, type GraphNode } from "./components/Graph";
import { HistoryPanel, type HistoryTarget } from "./components/HistoryPanel";
import { Overview, TableEditor } from "./components/Inspector";
import { ReviewQueue } from "./components/ReviewQueue";
import { About } from "./components/About";
import { Avatar, Info } from "./components/bits";
import { healthColor, palette } from "./theme";

interface Path {
  catalog?: string;
  schema?: string;
  table?: string;
}

const LEFT_W = 440;
const RIGHT_W = 400;
const QUEUE_W = 620;

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [catalogs, setCatalogs] = useState<CatalogNode[] | null>(null);
  const [schemas, setSchemas] = useState<Record<string, SchemaNode[]>>({});
  const [tables, setTables] = useState<Record<string, TableNode[]>>({});
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [path, setPath] = useState<Path>({});
  const [historyCol, setHistoryCol] = useState<string | null | undefined>(undefined);
  const [queueOpen, setQueueOpen] = useState(false);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [refreshKey, setRefreshKey] = useState(0);
  const [toast, setToast] = useState<string | null>(null);
  const [hoverChild, setHoverChild] = useState<string | null>(null);
  const [view, setView] = useState<"explore" | "about">(() => (location.hash === "#about" ? "about" : "explore"));

  useEffect(() => {
    history.replaceState(null, "", view === "about" ? "#about" : location.pathname);
  }, [view]);
  const [error, setError] = useState<string | null>(null);
  const toastRef = useRef<HTMLDivElement>(null);
  const autoPicked = useRef(false);

  const target = config?.coverage_target ?? 0.8;

  const notify = useCallback((text: string) => {
    setToast(text);
    requestAnimationFrame(() => {
      if (toastRef.current)
        animate(toastRef.current, { translateY: [-20, 0], opacity: [0, 1], duration: 450, ease: "outExpo" });
    });
    window.setTimeout(() => setToast((t) => (t === text ? null : t)), 3800);
  }, []);

  const loadCounts = useCallback(() => {
    api.summary().then(setCounts).catch(() => undefined);
  }, []);

  // Initial load.
  useEffect(() => {
    api.me().then(setMe).catch(() => undefined);
    api.config().then(setConfig).catch(() => undefined);
    api
      .catalogs()
      .then(setCatalogs)
      .catch((e) => setError(e.message));
    loadCounts();
    const t = window.setInterval(loadCounts, 30_000);
    return () => window.clearInterval(t);
  }, [loadCounts]);

  // With a single catalog in scope, open it straight away.
  useEffect(() => {
    if (!autoPicked.current && catalogs?.length === 1) {
      autoPicked.current = true;
      setPath({ catalog: catalogs[0].name });
    }
  }, [catalogs]);

  // Load what the current path needs.
  useEffect(() => {
    const { catalog, schema, table } = path;
    if (catalog)
      api
        .schemas(catalog)
        .then((s) => setSchemas((m) => ({ ...m, [catalog]: s })))
        .catch((e) => setError(e.message));
    if (catalog && schema)
      api
        .tables(catalog, schema)
        .then((t) => setTables((m) => ({ ...m, [`${catalog}.${schema}`]: t })))
        .catch((e) => setError(e.message));
    // Ignore a table response that arrives after the user has moved on.
    let stale = false;
    if (catalog && schema && table) {
      setDetail((d) => (d && d.catalog === catalog && d.schema === schema && d.name === table ? d : null));
      api
        .table(catalog, schema, table)
        .then((d) => !stale && setDetail(d))
        .catch((e) => !stale && setError(e.message));
    } else setDetail(null);
    return () => {
      stale = true;
    };
  }, [path, refreshKey]);

  useEffect(() => {
    if (refreshKey) api.catalogs().then(setCatalogs).catch(() => undefined);
  }, [refreshKey]);

  const reload = useCallback(() => {
    api.refresh().finally(() => {
      setRefreshKey((k) => k + 1);
      loadCounts();
    });
  }, [loadCounts]);

  // ---------- graph model ----------
  const nodes = useMemo<GraphNode[]>(() => {
    const out: GraphNode[] = [];
    const { catalog, schema, table } = path;
    const depth = table ? 3 : schema ? 2 : catalog ? 1 : 0;
    for (const c of catalogs ?? []) {
      out.push({
        id: `c:${c.name}`,
        kind: "catalog",
        label: c.name,
        parentId: null,
        childCount: c.schema_count,
        childNoun: "schemas",
        coverage: c.coverage,
        open: c.open_changes,
        onPath: c.name === catalog && depth > 1,
        focus: c.name === catalog && depth === 1,
      });
    }
    if (catalog)
      for (const s of schemas[catalog] ?? []) {
        out.push({
          id: `s:${catalog}.${s.name}`,
          kind: "schema",
          label: s.name,
          parentId: `c:${catalog}`,
          childCount: s.table_count,
          childNoun: "tables",
          coverage: s.coverage,
          open: s.open_changes,
          onPath: s.name === schema && depth > 2,
          focus: s.name === schema && depth === 2,
        });
      }
    if (catalog && schema)
      for (const t of tables[`${catalog}.${schema}`] ?? []) {
        out.push({
          id: `t:${catalog}.${schema}.${t.name}`,
          kind: "table",
          label: t.name,
          parentId: `s:${catalog}.${schema}`,
          childCount: t.column_count,
          childNoun: "columns",
          coverage: t.coverage,
          open: t.open_changes,
          onPath: false,
          focus: t.name === table,
        });
      }
    if (detail && detail.name === table)
      for (const col of detail.columns) {
        out.push({
          id: `col:${catalog}.${schema}.${table}.${col.column_name}`,
          kind: "column",
          label: col.column_name,
          parentId: `t:${catalog}.${schema}.${table}`,
          childCount: 0,
          childNoun: "",
          coverage: col.comment?.trim() ? 1 : 0,
          open: col.open_request ? 1 : 0,
          onPath: historyCol === col.column_name,
          focus: false,
        });
      }
    // Fade everything that isn't on the selected path or directly under it.
    const focusId = out.find((n) => n.focus)?.id;
    if (focusId)
      for (const n of out) {
        n.dim = !(n.focus || n.onPath || n.parentId === focusId);
        n.highlight = n.parentId === focusId && n.label === hoverChild;
      }
    return out;
  }, [catalogs, schemas, tables, detail, path, historyCol, hoverChild]);

  function select(n: GraphNode) {
    setQueueOpen(false);
    const parts = n.id.slice(n.id.indexOf(":") + 1).split(".");
    if (n.kind === "catalog") setPath(n.focus ? {} : { catalog: parts[0] });
    else if (n.kind === "schema")
      setPath(n.focus ? { catalog: parts[0] } : { catalog: parts[0], schema: parts[1] });
    else if (n.kind === "table") {
      setHistoryCol(undefined);
      setPath(n.focus ? { catalog: parts[0], schema: parts[1] } : { catalog: parts[0], schema: parts[1], table: parts[2] });
    } else setHistoryCol(parts[3]);
  }

  function goUp() {
    setHistoryCol(undefined);
    if (path.table) setPath({ catalog: path.catalog, schema: path.schema });
    else if (path.schema) setPath({ catalog: path.catalog });
    else if ((catalogs?.length ?? 0) > 1) setPath({});
  }

  function openRequest(r: ChangeRequest) {
    setQueueOpen(false);
    setPath({ catalog: r.catalog_name, schema: r.schema_name, table: r.table_name });
    setHistoryCol(r.column_name);
  }

  const historyTarget: HistoryTarget | null =
    detail && historyCol !== undefined
      ? {
          catalog: detail.catalog,
          schema: detail.schema,
          table: detail.name,
          column: historyCol,
          live: historyCol === null ? detail.comment : detail.columns.find((c) => c.column_name === historyCol)?.comment ?? null,
        }
      : null;

  // ---------- left panel ----------
  let left: React.ReactNode = null;
  if (path.table && detail) {
    left = (
      <TableEditor
        detail={detail}
        target={target}
        historyKey={historyCol === null ? "__table__" : historyCol ?? null}
        onHistory={(col) => {
          setQueueOpen(false);
          setHistoryCol((cur) => (cur === col ? undefined : col));
        }}
        onSubmitted={(n) => {
          notify(n ? `${n} change${n > 1 ? "s" : ""} submitted for review` : "No changes to submit");
          reload();
        }}
        onClose={goUp}
      />
    );
  } else if (path.schema && path.catalog) {
    const s = schemas[path.catalog]?.find((x) => x.name === path.schema);
    const ts = tables[`${path.catalog}.${path.schema}`] ?? [];
    if (s)
      left = (
        <Overview
          kind="schema"
          eyebrow={path.catalog}
          title={s.name}
          comment={s.comment}
          coverage={s.coverage}
          target={target}
          childNoun="tables"
          children={ts.map((t) => ({ name: t.name, coverage: t.coverage, open_changes: t.open_changes, sub: `${t.column_count} cols` }))}
          onPick={(t) => {
            setHoverChild(null);
            setPath({ ...path, table: t });
          }}
          onHover={setHoverChild}
          onClose={goUp}
        />
      );
  } else if (path.catalog) {
    const c = catalogs?.find((x) => x.name === path.catalog);
    const ss = schemas[path.catalog] ?? [];
    if (c)
      left = (
        <Overview
          kind="catalog"
          eyebrow="catalog"
          title={c.name}
          comment={null}
          coverage={c.coverage}
          target={target}
          childNoun="schemas"
          children={ss.map((s) => ({ name: s.name, coverage: s.coverage, open_changes: s.open_changes, sub: `${s.table_count} tables` }))}
          onPick={(s) => {
            setHoverChild(null);
            setPath({ catalog: path.catalog, schema: s });
          }}
          onHover={setHoverChild}
          onClose={goUp}
        />
      );
  }

  const rightOpen = queueOpen || !!historyTarget;
  const insets = { left: left ? LEFT_W : 0, right: queueOpen ? QUEUE_W : rightOpen ? RIGHT_W : 0 };
  const pending = counts.PENDING ?? 0;

  return (
    <div className="app">
      <TopBar
        view={view}
        onView={setView}
        title={config?.title ?? "Semantic Builder"}
        path={path}
        me={me}
        pending={pending}
        queueOpen={queueOpen}
        onCrumb={(p) => {
          setHistoryCol(undefined);
          setPath(p);
        }}
        onQueue={() => {
          setView("explore");
          setQueueOpen((o) => !o);
        }}
        onJump={(c, s, t) => {
          setView("explore");
          setQueueOpen(false);
          setHistoryCol(undefined);
          setPath({ catalog: c, schema: s, table: t });
        }}
        onRefresh={() => {
          reload();
          notify("Refreshed from Unity Catalog");
        }}
      />

      {view === "about" && (
        <main className="stage">
          <div className="about-scroll">
            <About config={config} />
          </div>
        </main>
      )}
      <main className="stage" style={view === "about" ? { display: "none" } : undefined}>
        <Graph
          nodes={nodes}
          target={target}
          insets={insets}
          onSelect={select}
          onBackground={() => {
            if (historyCol !== undefined) setHistoryCol(undefined);
          }}
        />
        {!catalogs && !error && <div className="center-msg">Reading Unity Catalog…</div>}
        {catalogs?.length === 0 && (
          <div className="center-msg">Nothing in scope. Set SB_SCOPE to the catalogs this app should manage.</div>
        )}
        {error && <div className="center-msg error">{error}</div>}
        <Legend target={target} left={insets.left} />
        {left}
        {queueOpen && me && config ? (
          <ReviewQueue
            me={me}
            config={config}
            counts={counts}
            onChanged={reload}
            onOpen={openRequest}
            onClose={() => setQueueOpen(false)}
          />
        ) : (
          historyTarget && (
            <HistoryPanel target={historyTarget} refreshKey={refreshKey} onClose={() => setHistoryCol(undefined)} />
          )
        )}
        {toast && (
          <div className="toast" ref={toastRef}>
            {toast}
          </div>
        )}
      </main>
    </div>
  );
}

function BrandMark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden>
      <line x1="13" y1="8" x2="6" y2="19" stroke={palette.fog} strokeWidth="1.4" />
      <line x1="13" y1="8" x2="20" y2="19" stroke={palette.fog} strokeWidth="1.4" />
      <circle cx="13" cy="8" r="5" fill={palette.blue} />
      <circle cx="6" cy="19" r="3.4" fill={palette.gold} />
      <circle cx="20" cy="19" r="3.4" fill={palette.red} />
    </svg>
  );
}

function TopBar(props: {
  view: "explore" | "about";
  onView: (v: "explore" | "about") => void;
  title: string;
  path: Path;
  me: Me | null;
  pending: number;
  queueOpen: boolean;
  onCrumb: (p: Path) => void;
  onQueue: () => void;
  onJump: (c: string, s: string, t: string) => void;
  onRefresh: () => void;
}) {
  const { view, onView, title, path, me, pending, queueOpen, onCrumb, onQueue, onJump, onRefresh } = props;
  const crumbs: { label: string; p: Path }[] = [];
  if (path.catalog) crumbs.push({ label: path.catalog, p: { catalog: path.catalog } });
  if (path.schema) crumbs.push({ label: path.schema, p: { catalog: path.catalog, schema: path.schema } });
  if (path.table) crumbs.push({ label: path.table, p: path });

  return (
    <header className="topbar">
      <div
        className="brand"
        onClick={() => {
          onView("explore");
          onCrumb({});
        }}
      >
        <BrandMark />
        <span>{title}</span>
      </div>
      <nav className="views">
        <button className={view === "explore" ? "on" : ""} onClick={() => onView("explore")}>
          Explore
        </button>
        <button className={view === "about" ? "on" : ""} onClick={() => onView("about")}>
          About
        </button>
      </nav>
      <nav className="crumbs mono" style={view === "about" ? { visibility: "hidden" } : undefined}>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sep">/</span>
            <button onClick={() => onCrumb(c.p)} className={i === crumbs.length - 1 ? "last" : ""}>
              {c.label}
            </button>
          </span>
        ))}
      </nav>
      <span className="spacer" />
      <Search onJump={onJump} />
      <button className="icon-btn" onClick={onRefresh} title="Refresh from Unity Catalog">
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v2.8h-2.8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <button className={`queue-btn ${queueOpen ? "on" : ""}`} onClick={onQueue}>
        {me?.is_approver ? "Review" : "Changes"}
        {pending > 0 && <span className="queue-n">{pending}</span>}
      </button>
      {me && (
        <div className="me" title={me.email}>
          <Avatar email={me.email} size={28} />
          <div className="me-text">
            <span>{me.email.split("@")[0]}</span>
            <span className="mono">{me.is_approver ? "approver" : "editor"}</span>
          </div>
        </div>
      )}
    </header>
  );
}

function Search({ onJump }: { onJump: (c: string, s: string, t: string) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ catalog: string; schema: string; table: string; comment: string | null }[]>([]);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  useEffect(() => {
    if (q.trim().length < 2) {
      setHits([]);
      return;
    }
    const t = window.setTimeout(() => api.search(q).then(setHits).catch(() => setHits([])), 200);
    return () => window.clearTimeout(t);
  }, [q]);

  return (
    <div className="search">
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
        <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      <input
        ref={inputRef}
        value={q}
        placeholder="Find a table"
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
      />
      <kbd>⌘K</kbd>
      {open && hits.length > 0 && (
        <div className="search-pop">
          {hits.map((h) => (
            <button
              key={`${h.catalog}.${h.schema}.${h.table}`}
              onMouseDown={() => {
                onJump(h.catalog, h.schema, h.table);
                setQ("");
              }}
            >
              <span className="mono">{h.table}</span>
              <span className="muted mono small">
                {h.catalog}.{h.schema}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Legend({ target, left }: { target: number; left: number }) {
  return (
    <div className="legend" style={{ left: left + 20 }}>
      <span>
        <i style={{ background: healthColor.good }} /> ≥ {Math.round(target * 100)}% described
      </span>
      <span>
        <i style={{ background: healthColor.partial }} /> partial
      </span>
      <span>
        <i style={{ background: healthColor.poor }} /> needs attention
      </span>
      <span>
        <i style={{ background: palette.brown }} /> open change
      </span>
      <Info side="top">
        Size reflects how much hangs off a node: catalogs by schemas, schemas by tables, tables by columns. The ring
        shows the share of descriptions filled in. Click a node to drill in; click a column to see its history.
      </Info>
    </div>
  );
}
