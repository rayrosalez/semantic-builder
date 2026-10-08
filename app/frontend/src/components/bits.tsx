import { useEffect, useRef, useState, type ReactNode } from "react";
import { animate } from "animejs";
import { health, healthColor, initials, pct, statusLabel } from "../theme";

/** Small (i) icon that reveals an explanation on hover or focus. */
export function Info({ children, side = "bottom" }: { children: ReactNode; side?: "bottom" | "left" | "top" }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="info"
      tabIndex={0}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
        <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <rect x="7.3" y="7" width="1.4" height="4.5" rx="0.7" fill="currentColor" />
        <circle cx="8" cy="4.8" r="0.9" fill="currentColor" />
      </svg>
      {open && <span className={`info-pop ${side}`}>{children}</span>}
    </span>
  );
}

export function Avatar({ email, size = 26 }: { email: string | null; size?: number }) {
  return (
    <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.4 }} title={email ?? ""}>
      {initials(email)}
    </span>
  );
}

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip st-${status.toLowerCase()}`}>{statusLabel[status] ?? status}</span>;
}

/** Coverage meter whose number counts up when it changes. */
export function Coverage({ value, target, label = "documented" }: { value: number; target: number; label?: string }) {
  const num = useRef<HTMLSpanElement>(null);
  const bar = useRef<HTMLSpanElement>(null);
  const last = useRef(0);
  useEffect(() => {
    const o = { v: last.current };
    animate(o, {
      v: value,
      duration: 900,
      ease: "outExpo",
      onUpdate: () => {
        if (num.current) num.current.textContent = pct(o.v);
        if (bar.current) bar.current.style.width = `${o.v * 100}%`;
      },
    });
    last.current = value;
  }, [value]);
  const color = healthColor[health(value, target)];
  return (
    <div className="coverage">
      <div className="cov-head">
        <span ref={num} className="cov-num" style={{ color }}>
          {pct(value)}
        </span>
        <span className="cov-label">{label}</span>
      </div>
      <div className="cov-track">
        <span ref={bar} className="cov-bar" style={{ background: color }} />
        <span className="cov-target" style={{ left: `${target * 100}%` }} title={`Target ${pct(target)}`} />
      </div>
    </div>
  );
}

export function HistoryIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden>
      <path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M2 2.5v2.8h2.8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8 5v3.2l2.2 1.4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function CloseButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="icon-btn" onClick={onClick} aria-label="Close">
      <svg width="14" height="14" viewBox="0 0 14 14">
        <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    </button>
  );
}

/** Slide a panel in from one side when it mounts. */
export function useSlideIn<T extends HTMLElement>(from: "left" | "right" | "up", deps: unknown[] = []) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!ref.current) return;
    const axis = from === "up" ? "translateY" : "translateX";
    const start = from === "left" ? -40 : 40;
    animate(ref.current, { [axis]: [start, 0], opacity: [0, 1], duration: 500, ease: "outExpo" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
