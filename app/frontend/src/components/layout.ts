/**
 * Deterministic radial tree layout.
 *
 * Catalogs sit at the center. Each expanded node fans its children out in an
 * arc pointing away from its own parent, so a schema's tables always grow
 * outward (never back across the sibling schemas), and a table's columns grow
 * outward from the table. Small groups sit on a clean arc; large groups (more
 * than ARC_LIMIT) are packed into a compact sunflower cluster in that outward
 * direction, least-documented first so trouble spots sit in the middle.
 */
import type { GraphNode, Kind } from "./Graph";

export interface Point {
  x: number;
  y: number;
}

// Arc length reserved per child (node diameter is added on top) and distance
// from parent to the first row, by child kind.
const GAP: Record<Kind, number> = { catalog: 120, schema: 70, table: 54, column: 72 };
const BASE: Record<Kind, number> = { catalog: 0, schema: 190, table: 150, column: 130 };
const MAX_SPAN = (200 * Math.PI) / 180;
export const ARC_LIMIT = 14;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));

export function layout(nodes: GraphNode[], radius: (n: GraphNode) => number): Map<string, Point> {
  const out = new Map<string, Point>();
  const kids = new Map<string | null, GraphNode[]>();
  for (const n of nodes) {
    const k = n.parentId && nodes.some((m) => m.id === n.parentId) ? n.parentId : null;
    kids.set(k, [...(kids.get(k) ?? []), n]);
  }

  const roots = kids.get(null) ?? [];
  if (roots.length === 1) out.set(roots[0].id, { x: 0, y: 0 });
  else {
    const circ = roots.reduce((a, n) => a + 2 * radius(n) + GAP.catalog, 0);
    const R = Math.max(280, circ / (2 * Math.PI));
    roots.forEach((n, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / roots.length;
      out.set(n.id, { x: R * Math.cos(a), y: R * Math.sin(a) });
    });
  }

  const place = (parent: GraphNode, dir: number, full: boolean) => {
    const children = kids.get(parent.id);
    if (!children?.length) return;
    const p = out.get(parent.id)!;
    const kind = children[0].kind;
    const slot = Math.max(...children.map((c) => 2 * radius(c))) + GAP[kind];
    const r0 = BASE[kind] + radius(parent);
    const span = full ? 2 * Math.PI : MAX_SPAN;

    if (full) {
      // Top level: one even ring around the catalog, however many schemas.
      const r = Math.max(r0, (children.length * slot) / span);
      children.forEach((c, i) => {
        const a = dir + (i * 2 * Math.PI) / children.length;
        out.set(c.id, { x: p.x + r * Math.cos(a), y: p.y + r * Math.sin(a) });
      });
    } else if (children.length <= ARC_LIMIT) {
      const r = Math.max(r0, (children.length * slot) / span);
      const step = children.length > 1 ? Math.min(slot / r, span / (children.length - 1)) : 0;
      const start = dir - (step * (children.length - 1)) / 2;
      children.forEach((c, i) => {
        const a = start + i * step;
        out.set(c.id, { x: p.x + r * Math.cos(a), y: p.y + r * Math.sin(a) });
      });
    } else {
      const s = slot * 0.54;
      const R = s * Math.sqrt(children.length);
      const cx = p.x + (r0 + R) * Math.cos(dir);
      const cy = p.y + (r0 + R) * Math.sin(dir);
      [...children]
        .sort((a, b) => a.coverage - b.coverage || a.label.localeCompare(b.label))
        .forEach((c, i) => {
          const rr = s * Math.sqrt(i + 0.5);
          const a = dir + Math.PI + i * GOLDEN;
          out.set(c.id, { x: cx + rr * Math.cos(a), y: cy + rr * Math.sin(a) });
        });
    }

    for (const c of children) {
      const q = out.get(c.id)!;
      place(c, Math.atan2(q.y - p.y, q.x - p.x), false);
    }
  };

  for (const r of roots) {
    const q = out.get(r.id)!;
    const single = roots.length === 1;
    place(r, single ? -Math.PI / 2 : Math.atan2(q.y, q.x), single);
  }
  return out;
}
