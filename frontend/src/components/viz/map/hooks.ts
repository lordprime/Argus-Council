import { useEffect, useRef, useState, type RefObject } from "react";

import type { CommEvent } from "@/features/viz/types";

import { pointAt, type Polyline } from "./layout";
import { PULSE_CLASS } from "./style";

/** True when the OS asks for reduced motion or the app sets :root[data-motion="reduced"]. */
export function useReducedMotion(): boolean {
  const read = () => {
    if (typeof window === "undefined") return false;
    const attr = document.documentElement.getAttribute("data-motion") === "reduced";
    const mq = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    return attr || !!mq;
  };
  const [reduced, setReduced] = useState(read);
  useEffect(() => {
    const update = () => setReduced(read());
    const mql = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    mql?.addEventListener?.("change", update);
    const mo = typeof MutationObserver !== "undefined" ? new MutationObserver(update) : null;
    mo?.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    update();
    return () => {
      mql?.removeEventListener?.("change", update);
      mo?.disconnect();
    };
  }, []);
  return reduced;
}

export const MAX_PULSES = 60;
const DURATION = 1600;

interface Pulse {
  start: number;
  pl: Polyline;
  reverse: boolean;
  cls: string;
}

const raf = (cb: FrameRequestCallback) =>
  typeof requestAnimationFrame === "function" ? requestAnimationFrame(cb) : (setTimeout(() => cb(performance.now()), 16) as unknown as number);
const caf = (id: number) => (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame(id) : clearTimeout(id));

/**
 * Drives up to MAX_PULSES light pulses along conduits. Positions are written straight to a fixed
 * pool of <circle> elements from a requestAnimationFrame loop, so React never re-renders per frame.
 */
export function usePulses(opts: {
  enabled: boolean;
  comms: CommEvent[];
  routeFor: (from: string, to: string) => { pl: Polyline; reverse: boolean } | undefined;
  visible: (ev: CommEvent) => boolean;
  pool: RefObject<(SVGCircleElement | null)[]>;
}) {
  const { enabled, comms, routeFor, visible, pool } = opts;
  const pulses = useRef<Pulse[]>([]);
  const lastSeq = useRef<number | null>(null);
  const frame = useRef<number | null>(null);

  function tick(now: number) {
    const els = pool.current ?? [];
    pulses.current = pulses.current.filter((p) => now - p.start < DURATION);
    for (let i = 0; i < els.length; i++) {
      const el = els[i];
      if (!el) continue;
      const p = pulses.current[i];
      if (!p || now < p.start) {
        el.setAttribute("visibility", "hidden");
        continue;
      }
      const t = (now - p.start) / DURATION;
      const [x, y] = pointAt(p.pl, p.reverse ? 1 - t : t);
      el.setAttribute("cx", x.toFixed(1));
      el.setAttribute("cy", y.toFixed(1));
      el.setAttribute("class", p.cls);
      el.setAttribute("opacity", String(t < 0.85 ? 1 : (1 - t) / 0.15));
      el.setAttribute("visibility", "visible");
    }
    frame.current = pulses.current.length ? raf(tick) : null;
  }

  // Enqueue pulses for events we have not seen yet. The first pass only records the high-water
  // mark so a page load / replay does not fire the whole backlog at once.
  useEffect(() => {
    const maxSeq = comms.reduce((m, c) => Math.max(m, c.seq), 0);
    if (!enabled || lastSeq.current === null) {
      lastSeq.current = maxSeq;
      return;
    }
    const now = performance.now();
    const fresh = comms.filter((c) => c.seq > lastSeq.current!).slice(-MAX_PULSES);
    lastSeq.current = Math.max(lastSeq.current, maxSeq);
    fresh.forEach((ev, i) => {
      if (!visible(ev)) return;
      const r = routeFor(ev.from.id, ev.to.id);
      if (!r || r.pl.len < 1) return;
      // Stagger bursts slightly so simultaneous events read as separate pulses.
      pulses.current.push({ start: now + i * 60, pl: r.pl, reverse: r.reverse, cls: PULSE_CLASS[ev.kind] ?? PULSE_CLASS.status! });
    });
    if (pulses.current.length > MAX_PULSES) pulses.current.splice(0, pulses.current.length - MAX_PULSES);
    if (pulses.current.length && frame.current === null) frame.current = raf(tick);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [comms, enabled]);


  // Pausing or switching to reduced motion drops in-flight pulses immediately.
  useEffect(() => {
    if (enabled) return;
    pulses.current = [];
    if (frame.current !== null) caf(frame.current);
    frame.current = null;
  }, [enabled]);

  useEffect(() => () => {
    if (frame.current !== null) caf(frame.current);
  }, []);
}

/** Throttled screen-reader summary: at most one announcement per `ms`, always ending on the latest. */
export function useThrottledAnnouncement(message: string, ms = 4000): string {
  const [out, setOut] = useState("");
  const last = useRef(0);
  useEffect(() => {
    if (!message) return;
    const wait = last.current + ms - Date.now();
    if (wait <= 0) {
      last.current = Date.now();
      setOut(message);
      return;
    }
    const t = setTimeout(() => {
      last.current = Date.now();
      setOut(message);
    }, wait);
    return () => clearTimeout(t);
  }, [message, ms]);
  return out;
}
