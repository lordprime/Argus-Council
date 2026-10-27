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
  let frame = 0;

  const flush = () => {
    frame = 0;
    if (pending.length) {
      const batch = pending;
      pending = [];
      h.onEvents(batch);
    }
  };
  const schedule = () => {
    if (frame) return;
    frame = typeof requestAnimationFrame === "function" ? requestAnimationFrame(flush) : (setTimeout(flush, 16) as unknown as number);
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
    if (frame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
  };
}
