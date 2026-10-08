import { useEffect, useRef, useState } from "react";
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior } from "d3-zoom";
import { animate, stagger } from "animejs";
import { health, healthColor, healthLabel, palette, pct } from "../theme";

export type Kind = "catalog" | "schema" | "table" | "column";

export interface GraphNode {
  id: string;
  kind: Kind;
  label: string;
  parentId: string | null;
  childCount: number;
  childNoun: string;
  coverage: number;
  open: number;
  onPath: boolean;
  focus: boolean;
  dim?: boolean;
}

interface SimNode extends SimulationNodeDatum {
  id: string;
  r: number;
  kind: Kind;
}
type SimLink = SimulationLinkDatum<SimNode> & { kind: Kind };

const LINK_DISTANCE: Record<Kind, number> = { catalog: 0, schema: 190, table: 120, column: 105 };

export function radius(n: Pick<GraphNode, "kind" | "childCount">): number {
  const c = Math.sqrt(Math.max(n.childCount, 0));
  switch (n.kind) {
    case "catalog":
      return 26 + c * 7;
    case "schema":
      return 15 + c * 4.5;
    case "table":
      return 8 + c * 2.4;
    default:
      return 6;
  }
}

interface Props {
  nodes: GraphNode[];
  target: number;
  insets: { left: number; right: number };
  onSelect: (n: GraphNode) => void;
  onBackground: () => void;
}

export function Graph({ nodes, target, insets, onSelect, onBackground }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const gRef = useRef<SVGGElement>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const pos = useRef(new Map<string, { x: number; y: number }>());
  const nodeEls = useRef(new Map<string, SVGGElement>());
  const linkEls = useRef(new Map<string, SVGLineElement>());
  const seen = useRef(new Set<string>());
  const transform = useRef({ x: 0, y: 0, k: 1 });
  const [hover, setHover] = useState<{ n: GraphNode; x: number; y: number } | null>(null);

  // Pan/zoom; the background grid pattern moves with the canvas.
  useEffect(() => {
    const svg = select(svgRef.current!);
    const z = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 4])
      .on("zoom", (e) => {
        const t = e.transform;
        transform.current = { x: t.x, y: t.y, k: t.k };
        gRef.current?.setAttribute("transform", t.toString());
        svgRef.current
          ?.querySelectorAll("pattern")
          .forEach((p) => p.setAttribute("patternTransform", t.toString()));
      });
    svg.call(z).on("dblclick.zoom", null);
    zoomRef.current = z;
    const { width, height } = svgRef.current!.getBoundingClientRect();
    svg.call(z.transform, zoomIdentity.translate(width / 2, height / 2));
  }, []);

  // Rebuild the simulation whenever the visible node set changes, keeping
  // existing positions so the graph grows instead of re-shuffling.
  useEffect(() => {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const simNodes: SimNode[] = nodes.map((n) => {
      const prev = pos.current.get(n.id);
      const parent = n.parentId ? pos.current.get(n.parentId) : undefined;
      const start = prev ?? {
        x: (parent?.x ?? 0) + (Math.random() - 0.5) * 40,
        y: (parent?.y ?? 0) + (Math.random() - 0.5) * 40,
      };
      return { id: n.id, kind: n.kind, r: radius(n), x: start.x, y: start.y };
    });
    const links: SimLink[] = nodes
      .filter((n) => n.parentId && byId.has(n.parentId))
      .map((n) => ({ source: n.parentId!, target: n.id, kind: n.kind }));

    simRef.current?.stop();
    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance((l) => LINK_DISTANCE[l.kind] + (l.target as SimNode).r)
          .strength(0.7),
      )
      .force("charge", forceManyBody<SimNode>().strength((d) => -30 - d.r * 9))
      .force("collide", forceCollide<SimNode>((d) => d.r + (d.kind === "column" ? 26 : 30)).strength(0.9))
      .force("x", forceX<SimNode>(0).strength(0.03))
      .force("y", forceY<SimNode>(0).strength(0.03))
      .alpha(0.9)
      .alphaDecay(0.035);

    sim.on("tick", () => {
      for (const n of simNodes) {
        pos.current.set(n.id, { x: n.x!, y: n.y! });
        nodeEls.current.get(n.id)?.setAttribute("transform", `translate(${n.x},${n.y})`);
      }
      for (const l of links) {
        const s = l.source as SimNode;
        const t = l.target as SimNode;
        const el = linkEls.current.get(t.id);
        if (el) {
          el.setAttribute("x1", String(s.x));
          el.setAttribute("y1", String(s.y));
          el.setAttribute("x2", String(t.x));
          el.setAttribute("y2", String(t.y));
        }
      }
    });
    sim.on("end", () => fitToFocus());
    simRef.current = sim;

    // Entry animation for nodes we haven't drawn before.
    const fresh = nodes.filter((n) => !seen.current.has(n.id));
    fresh.forEach((n) => seen.current.add(n.id));
    const els = fresh
      .map((n) => nodeEls.current.get(n.id)?.querySelector(".glyph"))
      .filter(Boolean) as Element[];
    if (els.length) {
      animate(els, {
        scale: [0, 1],
        opacity: [0, 1],
        duration: 700,
        delay: stagger(Math.min(25, 600 / els.length)),
        ease: "outElastic(1, .7)",
      });
    }

    const fit = window.setTimeout(() => fitToFocus(), 650);
    return () => window.clearTimeout(fit);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.map((n) => n.id).join("|")]);

  // Glide the camera to frame the focused node and its children.
  function fitToFocus() {
    const svg = svgRef.current;
    const z = zoomRef.current;
    if (!svg || !z) return;
    const focus = nodes.find((n) => n.focus);
    const frame = focus
      ? nodes.filter((n) => n.id === focus.id || n.parentId === focus.id)
      : nodes.filter((n) => n.kind === "catalog");
    const pts = frame
      .map((n) => ({ p: pos.current.get(n.id), r: radius(n) + 30 }))
      .filter((x) => x.p) as { p: { x: number; y: number }; r: number }[];
    if (!pts.length) return;
    const x0 = Math.min(...pts.map((d) => d.p.x - d.r));
    const x1 = Math.max(...pts.map((d) => d.p.x + d.r));
    const y0 = Math.min(...pts.map((d) => d.p.y - d.r));
    const y1 = Math.max(...pts.map((d) => d.p.y + d.r + 16));
    const { width, height } = svg.getBoundingClientRect();
    const availW = width - insets.left - insets.right - 80;
    const availH = height - 120;
    const k = Math.max(0.2, Math.min(1.25, availW / (x1 - x0), availH / (y1 - y0)));
    const cx = insets.left + 40 + availW / 2;
    const cy = 60 + availH / 2;
    const to = { x: cx - k * ((x0 + x1) / 2), y: cy - k * ((y0 + y1) / 2), k };
    const cam = { ...transform.current };
    animate(cam, {
      ...to,
      duration: 900,
      ease: "inOutQuart",
      onUpdate: () =>
        select(svg).call(z.transform, zoomIdentity.translate(cam.x, cam.y).scale(cam.k)),
    });
  }

  // Re-frame when the side panels open or close.
  useEffect(() => {
    const t = window.setTimeout(() => fitToFocus(), 50);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insets.left, insets.right]);

  const linked = nodes.filter((n) => n.parentId && nodes.some((m) => m.id === n.parentId));

  return (
    <div className="graph-wrap">
      <svg
        ref={svgRef}
        className="graph"
        onClick={(e) => {
          if (e.target === e.currentTarget || (e.target as Element).classList.contains("grid-bg"))
            onBackground();
        }}
      >
        <defs>
          <pattern id="grid" width="160" height="160" patternUnits="userSpaceOnUse">
            <path
              d="M 32 0 V 160 M 64 0 V 160 M 96 0 V 160 M 128 0 V 160 M 0 32 H 160 M 0 64 H 160 M 0 96 H 160 M 0 128 H 160"
              fill="none"
              stroke={palette.fog}
              strokeWidth="0.6"
            />
            <path d="M 160 0 L 0 0 0 160" fill="none" stroke={palette.fog} strokeWidth="1.2" />
          </pattern>
        </defs>
        <rect className="grid-bg" width="100%" height="100%" fill="url(#grid)" />
        <g ref={gRef}>
          <g className="links">
            {linked.map((n) => (
              <line
                key={n.id}
                ref={(el) => {
                  if (el) linkEls.current.set(n.id, el);
                  else linkEls.current.delete(n.id);
                }}
                className={`link ${n.onPath || n.focus ? "on-path" : ""} ${n.dim ? "dim" : ""}`}
              />
            ))}
          </g>
          <g className="nodes">
            {nodes.map((n) => (
              <NodeGlyph
                key={n.id}
                n={n}
                target={target}
                register={(el) => {
                  if (el) {
                    nodeEls.current.set(n.id, el);
                    const p = pos.current.get(n.id);
                    if (p) el.setAttribute("transform", `translate(${p.x},${p.y})`);
                  } else nodeEls.current.delete(n.id);
                }}
                onClick={() => onSelect(n)}
                onHover={(e) => setHover(e ? { n, x: e.clientX, y: e.clientY } : null)}
              />
            ))}
          </g>
        </g>
      </svg>
      {hover && (
        <div className="hovercard" style={{ left: hover.x + 16, top: hover.y + 12 }}>
          <div className="hc-kind">{hover.n.kind}</div>
          <div className="hc-name">{hover.n.label}</div>
          <div className="hc-row">
            <span className="dot" style={{ background: healthColor[health(hover.n.coverage, target)] }} />
            {hover.n.kind === "column"
              ? hover.n.coverage >= 1
                ? "Described"
                : "No description"
              : `${pct(hover.n.coverage)} documented · ${healthLabel[health(hover.n.coverage, target)]}`}
          </div>
          {hover.n.kind !== "column" && (
            <div className="hc-row muted">
              {hover.n.childCount} {hover.n.childNoun}
            </div>
          )}
          {hover.n.open > 0 && (
            <div className="hc-row brown">
              {hover.n.open} open change{hover.n.open > 1 ? "s" : ""}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NodeGlyph({
  n,
  target,
  register,
  onClick,
  onHover,
}: {
  n: GraphNode;
  target: number;
  register: (el: SVGGElement | null) => void;
  onClick: () => void;
  onHover: (e: React.MouseEvent | null) => void;
}) {
  const r = radius(n);
  const h = health(n.coverage, n.kind === "column" ? 1 : target);
  const color = n.kind === "column" && n.open > 0 ? palette.brown : healthColor[h];
  const ring = r + (n.kind === "column" ? 3 : 5);
  const circ = 2 * Math.PI * ring;
  const showLabel = true;
  const cls = ["node", `kind-${n.kind}`, n.focus ? "focus" : "", n.onPath ? "on-path" : "", n.dim ? "dim" : "", h];

  return (
    <g
      ref={register}
      className={cls.join(" ")}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      onMouseMove={onHover}
      onMouseLeave={() => onHover(null)}
    >
      <g className="glyph">
        {n.kind !== "column" && (
          <>
            <circle r={ring} className="track" />
            <circle
              r={ring}
              className="arc"
              stroke={color}
              strokeDasharray={`${circ * n.coverage} ${circ}`}
              transform="rotate(-90)"
            />
          </>
        )}
        {h === "poor" && n.kind !== "column" && <circle r={ring + 4} className="pulse" stroke={color} />}
        <circle r={r} className="core" fill={color} />
        {n.kind !== "column" && r > 13 && (
          <text className="count" dy="0.35em">
            {n.childCount}
          </text>
        )}
        {n.open > 0 && n.kind !== "column" && (
          <g transform={`translate(${ring * 0.72},${-ring * 0.72})`} className="badge">
            <circle r={9} fill={palette.brown} />
            <text dy="0.35em">{n.open}</text>
          </g>
        )}
      </g>
      {showLabel && (
        <text className="label" y={ring + 16}>
          {n.label}
        </text>
      )}
    </g>
  );
}
