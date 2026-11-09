// Live browser frames for one member (watch-only): an SSE stream of base64 JPEGs, read with fetch
// streaming so the bearer token travels in a header, like the run event stream. Nothing is ever sent
// back over it.

import { auth, refreshOnce, V1 } from "./api";
import { parseSse } from "./stream";

export const MAX_RETRIES = 5;
/** Longest wait between reconnects, in ms. */
export const MAX_BACKOFF_MS = 8000;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;

/** How one connection attempt ended. */
export type FramesEnd = "ended" | "gone" | "gave-up" | "aborted";

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });

const backoff = (attempt: number) => Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt);

/**
 * Stream frames until the server ends the stream (`"ended"`), answers 404 (`"gone"`: the run is not live
 * or the member has no session right now) or `signal` aborts (`"aborted"`). A dropped connection (for
 * example the browser service restarting) is retried with backoff; the count of failed attempts resets
 * after every successful connect, and after MAX_RETRIES failures in a row it gives up (`"gave-up"`).
 */
export async function openFrames(runId: string, memberId: string, onFrame: (jpegB64: string) => void, signal: AbortSignal): Promise<FramesEnd> {
  const url = `${V1}/runs/${encodeURIComponent(runId)}/browser/${encodeURIComponent(memberId)}/frames`;
  const get = () => fetch(url, { headers: { Authorization: `Bearer ${auth.token ?? ""}`, Accept: "text/event-stream" }, signal });
  let attempt = 0;
  while (!signal.aborted) {
    try {
      if (!auth.token) await refreshOnce();
      let resp = await get();
      if (resp.status === 401 && (await refreshOnce())) resp = await get();
      if (resp.status === 404) return "gone";
      if (!resp.ok || !resp.body) throw new Error(`frames HTTP ${resp.status}`);
      attempt = 0; // connected: a later drop starts the backoff over
      for await (const data of parseSse(resp.body)) {
        if (BASE64.test(data)) onFrame(data);
      }
      return signal.aborted ? "aborted" : "ended"; // the server ended the stream (`event: end`)
    } catch (err) {
      if (signal.aborted || (err as Error).name === "AbortError") return "aborted";
    }
    if (attempt >= MAX_RETRIES) return "gave-up";
    await sleep(backoff(attempt++), signal);
  }
  return "aborted";
}

/**
 * Keep watching a member's browser for as long as `signal` is live. The caller aborts when the session
 * closes or is replaced; until then a stream that ends, 404s (the session is not registered yet, or a
 * replacement is starting) or gives up is reopened after a backoff that resets once frames flow again.
 */
export async function followFrames(runId: string, memberId: string, onFrame: (jpegB64: string) => void, signal: AbortSignal): Promise<void> {
  let round = 0;
  while (!signal.aborted) {
    let got = false;
    const end = await openFrames(runId, memberId, (b64) => { got = true; onFrame(b64); }, signal);
    if (end === "aborted" || signal.aborted) return;
    if (got) round = 0;
    await sleep(backoff(round++), signal);
  }
}
