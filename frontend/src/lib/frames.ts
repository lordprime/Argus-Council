// Live browser frames for one member (watch-only): an SSE stream of base64 JPEGs, read with fetch
// streaming so the bearer token travels in a header, like the run event stream. Nothing is ever sent
// back over it.

import { auth, refreshOnce, V1 } from "./api";
import { parseSse } from "./stream";

const MAX_RETRIES = 5;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });

/**
 * Stream frames until the server ends the stream, answers 404 (the run is not live or the member has no
 * session) or `signal` aborts. A dropped connection (for example the browser service restarting) is
 * retried a few times, so the view recovers without a page reload.
 */
export async function openFrames(runId: string, memberId: string, onFrame: (jpegB64: string) => void, signal: AbortSignal): Promise<void> {
  const url = `${V1}/runs/${encodeURIComponent(runId)}/browser/${encodeURIComponent(memberId)}/frames`;
  const get = () => fetch(url, { headers: { Authorization: `Bearer ${auth.token ?? ""}`, Accept: "text/event-stream" }, signal });
  for (let attempt = 0; attempt <= MAX_RETRIES && !signal.aborted; attempt++) {
    try {
      if (!auth.token) await refreshOnce();
      let resp = await get();
      if (resp.status === 401 && (await refreshOnce())) resp = await get();
      if (resp.status === 404) return;
      if (!resp.ok || !resp.body) throw new Error(`frames HTTP ${resp.status}`);
      for await (const data of parseSse(resp.body)) {
        if (BASE64.test(data)) onFrame(data);
      }
      return; // the server ended the stream (`event: end`)
    } catch (err) {
      if (signal.aborted || (err as Error).name === "AbortError") return;
    }
    await sleep(Math.min(8000, 1000 * 2 ** attempt), signal);
  }
}
