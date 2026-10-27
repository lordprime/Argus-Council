// Resumable run event stream over SSE (ui-spec §6.1). Uses fetch streaming so the bearer token
// travels in a header; reconnects with exponential backoff from the last durable seq.

import { auth, refreshOnce } from "./api";
import type { RunEvent } from "./types";

export interface StreamHandlers {
  onEvents: (events: RunEvent[]) => void;
  onState: (state: "connecting" | "live" | "reconnecting" | "closed" | "lost", attempt: number) => void;
  getLastSeq: () => number;
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "halted"]);
const MAX_ATTEMPTS = 8;
/**
 * Events are batched and handed to the store at most every FLUSH_MS (about 10 Hz, implementation review
 * L5), not on every animation frame: each flush re-renders every subscriber, and streaming text does
 * not need 60 updates a second. A timer (not rAF) also keeps a hidden tab draining, at the browser's
 * background timer rate, instead of piling events up until it is shown again.
 */
export const FLUSH_MS = 100;

/** Parse an SSE byte stream into event payloads (only `data:` lines carry our JSON). */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const data = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data) yield data;
    }
  }
}

export function openRunStream(runId: string, h: StreamHandlers): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let attempt = 0;
  let pending: RunEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastFlush = 0;

  const flush = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    lastFlush = Date.now();
    if (pending.length) {
      const batch = pending;
      pending = [];
      h.onEvents(batch);
    }
  };
  const schedule = () => {
    if (timer) return;
    timer = setTimeout(flush, Math.max(0, lastFlush + FLUSH_MS - Date.now()));
  };

  const connect = async (): Promise<void> => {
    h.onState(attempt === 0 ? "connecting" : "reconnecting", attempt);
    controller = new AbortController();
    let terminal = false;
    try {
      if (!auth.token) await refreshOnce();
      const resp = await fetch(`/api/v1/runs/${runId}/events/stream?after_seq=${h.getLastSeq()}`, {
        headers: { Authorization: `Bearer ${auth.token ?? ""}`, Accept: "text/event-stream" },
        signal: controller.signal,
      });
      if (resp.status === 401 && (await refreshOnce())) return connect();
      if (!resp.ok || !resp.body) throw new Error(`stream HTTP ${resp.status}`);
      attempt = 0;
      h.onState("live", 0);
      for await (const data of parseSse(resp.body)) {
        const ev = JSON.parse(data) as RunEvent;
        pending.push(ev);
        schedule();
        if (ev.type === "run.status" && TERMINAL.has(String((ev.data as { status?: string }).status))) terminal = true;
      }
    } catch (err) {
      if (stopped || (err as Error).name === "AbortError") return;
    }
    flush();
    if (stopped) return;
    if (terminal) {
      h.onState("closed", 0);
      return;
    }
    attempt += 1;
    if (attempt > MAX_ATTEMPTS) {
      h.onState("lost", attempt);
      return;
    }
    const delay = Math.min(30_000, 500 * 2 ** (attempt - 1)) * (0.75 + Math.random() * 0.5);
    setTimeout(() => !stopped && void connect(), delay);
  };

  void connect();
  return () => {
    stopped = true;
    controller?.abort();
    if (timer) clearTimeout(timer);
    timer = null;
  };
}
