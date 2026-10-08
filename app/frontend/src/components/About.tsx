import { useEffect, useRef } from "react";
import { animate, stagger } from "animejs";
import type { AppConfig } from "../api";
import { palette } from "../theme";
import { Diff } from "./HistoryPanel";

export function About({ config }: { config: AppConfig | null }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const els = ref.current?.querySelectorAll(".reveal");
    if (els?.length)
      animate(els, { opacity: [0, 1], translateY: [14, 0], delay: stagger(70), duration: 650, ease: "outExpo" });
  }, []);

  const queue = config?.staging_table ?? "<catalog>.semantic_builder.metadata_change_requests";

  return (
    <div className="about" ref={ref}>
      <section className="about-hero reveal">
        <div className="eyebrow">About</div>
        <h1>
          Your business knows what the data means.
          <br />
          <span>Semantic Builder puts that knowledge in Unity Catalog.</span>
        </h1>
        <p>
          A simple place for the people closest to the data to describe it. Every change is reviewed, tracked and
          applied without disturbing the pipelines that keep the data fresh.
        </p>
      </section>

      <section className="about-section">
        <h2 className="reveal">Why it helps</h2>
        <div className="cards">
          <Card n="01" title="Context lives in people's heads">
            Definitions, caveats and rules of thumb sit with business owners. The workspace console is built for
            engineers, so that knowledge rarely makes it into the catalog.
          </Card>
          <Card n="02" title="Descriptions power what comes next">
            Unity Catalog descriptions feed Genie, AI/BI dashboards, catalog search and AI agents. Better
            descriptions mean better answers for everyone downstream.
          </Card>
          <Card n="03" title="No spreadsheets or YAML hand-offs">
            Replaces Excel trackers and hand-built YAML batches. People edit what they see, submit piece by piece, and
            the change lands in a governed queue.
          </Card>
        </div>
      </section>

      <section className="about-section split">
        <div className="reveal">
          <h2>Every change has a history</h2>
          <p className="muted">
            Click the clock icon next to any description to see its timeline, similar to a pull request: who proposed
            it, what changed word by word, who approved it, and when it reached Unity Catalog. Edits made outside the
            app are flagged as <b>Changed outside</b>.
          </p>
          <ul className="ticks">
            <li>Who requested and who reviewed</li>
            <li>When each step happened</li>
            <li>The value before and after</li>
            <li>Status, from pending to applied</li>
          </ul>
        </div>
        <div className="example reveal">
          <div className="ex-head">
            <span className="avatar" style={{ width: 22, height: 22, fontSize: 9 }}>
              JL
            </span>
            <b>jordan.lee</b>
            <span className="muted">proposed</span>
            <span className="spacer" />
            <span className="chip st-applied">Applied</span>
          </div>
          <div className="mono small muted">title_production.policies.underwriter_code</div>
          <Diff
            from="Underwriter code."
            to="Code of the underwriting company that issued the policy. Joins to underwriters.code."
          />
          <div className="ex-events">
            <span>
              <i style={{ background: palette.blue }} /> Approved by sam.ortiz
            </span>
            <span>
              <i style={{ background: palette.blue }} /> Applied to Unity Catalog
            </span>
          </div>
        </div>
      </section>

      <section className="about-section">
        <h2 className="reveal">How a change moves</h2>
        <ol className="steps reveal">
          <Step status="PENDING" title="Propose">
            Anyone with access edits a table or column description and submits it, with an optional note.
          </Step>
          <Step status="APPROVED" title="Review">
            A data steward approves or rejects it, one at a time or in bulk. You can't approve your own changes.
          </Step>
          <Step status="QUEUED" title="Wait for a quiet moment">
            Approved changes wait until no job or pipeline is writing to that table.
          </Step>
          <Step status="APPLIED" title="Apply">
            The change is written to Unity Catalog and the timeline records it.
          </Step>
        </ol>
      </section>

      <section className="about-section">
        <h2 className="reveal">Technical overview</h2>
        <div className="arch-card reveal">
          <ArchDiagram />
        </div>
        <div className="tech-grid">
          <Tech title="App">
            Databricks App (React and FastAPI) running as its own service principal. It reads current descriptions
            from <code>information_schema</code> through a SQL warehouse and only ever writes to the queue, never to
            business tables.
          </Tech>
          <Tech title="Change queue">
            <code>{queue}</code>: a Delta table with one row per request (requester, reviewer, before and after
            values, status, apply notes). It is the audit trail, and it can be queried like any other table.
          </Tech>
          <Tech title="Apply job">
            A serverless job, every 15 minutes by default. It finds each table's writers from{" "}
            <code>system.access.table_lineage</code> and the table's Delta history, waits while any are running, then
            applies all of the table's edits in one commit and retries if a write collides.
          </Tech>
          <Tech title="Why wait?">
            Each description change is a metadata commit to the table. A job writing at the same moment can fail with{" "}
            <code>MetadataChangedException</code>. Waiting for a gap, and batching edits, avoids that.
          </Tech>
          <Tech title="Pipeline-managed tables">
            Materialized views and streaming tables keep descriptions through refreshes. A redeploy that redefines them
            resets descriptions to the pipeline source, so the app warns the owner and flags any reversion.
          </Tech>
          <Tech title="Permissions and portability">
            The app's service principal has <code>BROWSE</code> on the catalogs in scope and write access to the
            queue. The apply job runs as an identity with <code>MODIFY</code> on those tables. Everything ships as
            one Databricks Asset Bundle.
          </Tech>
        </div>
      </section>
    </div>
  );
}

function Card({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <div className="card reveal">
      <div className="card-n mono">{n}</div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

function Step({ status, title, children }: { status: string; title: string; children: React.ReactNode }) {
  return (
    <li className="step">
      <span className={`chip st-${status === "QUEUED" ? "approved" : status.toLowerCase()}`}>
        {status === "QUEUED" ? "Approved · queued" : status.toLowerCase()}
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </li>
  );
}

function Tech({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="tech reveal">
      <h4>{title}</h4>
      <p>{children}</p>
    </div>
  );
}

/** The flow, left to right: people → app → queue → apply job → Unity Catalog. */
function ArchDiagram() {
  const ref = useRef<SVGSVGElement>(null);
  useEffect(() => {
    const boxes = ref.current?.querySelectorAll(".a-box");
    if (boxes?.length)
      animate(boxes, { opacity: [0, 1], translateY: [10, 0], delay: stagger(110, { start: 250 }), duration: 600, ease: "outExpo" });
  }, []);

  const B = palette.blue;
  const F = palette.fog;
  const M = "#6b6a68";
  return (
    <svg ref={ref} viewBox="0 0 1180 420" className="arch" role="img" aria-label="Semantic Builder architecture">
      <defs>
        <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0 0L10 5L0 10z" fill={B} />
        </marker>
        <marker id="arr-f" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0 0L10 5L0 10z" fill={M} />
        </marker>
      </defs>

      {/* main flow */}
      <g className="a-flow" fill="none" stroke={B} strokeWidth="1.6" markerEnd="url(#arr)">
        <path d="M146 206 H196" />
        <path d="M386 206 H446" />
        <path d="M646 206 H706" />
        <path d="M936 206 H996" />
        <path d="M546 108 V148" />
        <path d="M1085 150 V110" />
      </g>
      <g className="a-labels">
        <text x="171" y="196">edit</text>
        <text x="416" y="196">submit</text>
        <text x="676" y="196">approved</text>
        <text x="966" y="196">apply</text>
        <text x="556" y="133" style={{ textAnchor: "start" }}>approve / reject</text>
        <text x="1095" y="135" style={{ textAnchor: "start" }}>used by</text>
      </g>

      {/* feedback paths (dashed) */}
      <g fill="none" stroke={M} strokeWidth="1.2" strokeDasharray="4 4" markerEnd="url(#arr-f)">
        <path d="M1030 262 V318 H821 V266" />
        <path d="M1060 262 V372 H291 V266" />
      </g>
      <g className="a-labels muted">
        <text x="900" y="336">who is writing? (lineage + Delta history)</text>
        <text x="676" y="390">current descriptions (information_schema)</text>
      </g>

      {/* people */}
      <g className="a-box">
        <circle cx="74" cy="184" r="13" fill={B} />
        <circle cx="50" cy="197" r="10" fill={palette.gold} />
        <circle cx="98" cy="197" r="10" fill={palette.red} />
        <path d="M32 234 q42 -32 84 0" fill="none" stroke={F} strokeWidth="2" />
        <text x="74" y="258" className="a-title" textAnchor="middle">Business users</text>
        <text x="74" y="276" className="a-sub" textAnchor="middle">closest to the data</text>
      </g>

      {/* app */}
      <g className="a-box">
        <rect x="196" y="150" width="190" height="112" rx="12" className="a-rect" />
        <text x="214" y="180" className="a-eyebrow">DATABRICKS APP</text>
        <text x="214" y="205" className="a-title">Semantic Builder</text>
        <text x="214" y="228" className="a-sub">Browse the graph</text>
        <text x="214" y="246" className="a-sub">Edit · review · history</text>
      </g>

      {/* approvers */}
      <g className="a-box">
        <rect x="456" y="44" width="180" height="62" rx="12" className="a-rect soft" />
        <text x="546" y="70" className="a-title" textAnchor="middle">Approvers</text>
        <text x="546" y="90" className="a-sub" textAnchor="middle">data stewards, in the app</text>
      </g>

      {/* queue */}
      <g className="a-box">
        <rect x="446" y="150" width="200" height="112" rx="12" className="a-rect" />
        <text x="464" y="180" className="a-eyebrow">DELTA TABLE</text>
        <text x="464" y="205" className="a-title">Change queue</text>
        <text x="464" y="228" className="a-sub">who · when · before → after</text>
        <g transform="translate(464 240)">
          <rect width="62" height="16" rx="8" fill="none" stroke={palette.brown} />
          <text x="31" y="11.5" className="a-pill" fill={palette.brown}>PENDING</text>
          <text x="70" y="12" className="a-sub">→</text>
          <rect x="84" width="70" height="16" rx="8" fill="none" stroke={palette.gold} />
          <text x="119" y="11.5" className="a-pill" fill="#8a6a17">APPROVED</text>
        </g>
      </g>

      {/* apply job */}
      <g className="a-box">
        <rect x="706" y="150" width="230" height="112" rx="12" className="a-rect" />
        <text x="724" y="180" className="a-eyebrow">SCHEDULED JOB · EVERY 15 MIN</text>
        <text x="724" y="205" className="a-title">Apply job</text>
        <rect x="724" y="218" width="94" height="30" rx="7" fill="rgba(215,169,66,.16)" stroke={palette.gold} />
        <text x="771" y="231" className="a-pill" fill="#8a6a17">WRITER RUNNING</text>
        <text x="771" y="242" className="a-tiny">wait</text>
        <rect x="826" y="218" width="94" height="30" rx="7" fill="rgba(10,49,97,.07)" stroke={B} />
        <text x="873" y="231" className="a-pill" fill={B}>TABLE QUIET</text>
        <text x="873" y="242" className="a-tiny">apply in 1 commit</text>
      </g>

      {/* unity catalog */}
      <g className="a-box">
        <rect x="996" y="150" width="176" height="112" rx="12" className="a-rect strong" />
        <text x="1014" y="180" className="a-eyebrow light">GOVERNED</text>
        <text x="1014" y="205" className="a-title light">Unity Catalog</text>
        <text x="1014" y="228" className="a-sub light">Table and column</text>
        <text x="1014" y="246" className="a-sub light">descriptions</text>
      </g>

      {/* consumers */}
      <g className="a-box">
        <rect x="996" y="44" width="176" height="62" rx="12" className="a-rect soft" />
        <text x="1084" y="70" className="a-title" textAnchor="middle">Everyone downstream</text>
        <text x="1084" y="90" className="a-sub" textAnchor="middle">Genie · AI/BI · search · agents</text>
      </g>
    </svg>
  );
}
