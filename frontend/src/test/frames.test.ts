// lib/frames: the watch-only live browser stream, its retries and what it accepts as a frame.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { auth } from "@/lib/api";
import { followFrames, MAX_RETRIES, openFrames } from "@/lib/frames";

const FRAMES = "/api/v1/runs/r1/browser/m1/frames";
const sse = (body: string, status = 200) => new Response(body, { status, headers: { "content-type": "text/event-stream" } });
const END = "event: end\ndata: {}\n\n";

/** A stream that delivers one frame, then fails like a dropped connection. */
const dropping = () => {
  const enc = new TextEncoder();
  let sent = false;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(c) {
        if (sent) return c.error(new TypeError("network"));
        sent = true;
        c.enqueue(enc.encode("event: frame\ndata: QUJD\n\n"));
      },
    }),
    { status: 200 },
  );
};

const frameCalls = (m: { mock: { calls: unknown[][] } }) => m.mock.calls.filter(([u]) => String(u) === FRAMES).length;

beforeEach(() => {
  vi.useFakeTimers();
  auth.setTokens({ access_token: "a1", refresh_token: "r1" } as never);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  auth.clear();
});

describe("openFrames", () => {
  it("returns quietly on 404", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 404 }));
    const onFrame = vi.fn();
    await expect(openFrames("r1", "m1", onFrame, new AbortController().signal)).resolves.toBe("gone");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onFrame).not.toHaveBeenCalled();
  });

  it("sends the bearer token, and refreshes it once on 401 before retrying", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input) === "/api/v1/auth/refresh") return new Response(JSON.stringify({ access_token: "a2", refresh_token: "r2" }), { status: 200 });
      const authz = new Headers(init?.headers).get("Authorization");
      return authz === "Bearer a2" ? sse(`data: QUJD\n\n${END}`) : new Response("", { status: 401 });
    });
    const onFrame = vi.fn();
    await expect(openFrames("r1", "m1", onFrame, new AbortController().signal)).resolves.toBe("ended");
    expect(onFrame).toHaveBeenCalledWith("QUJD");
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toEqual([FRAMES, "/api/v1/auth/refresh", FRAMES]);
  });

  it("drops data that is not base64", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(sse(`data: QUJD\n\ndata: <script>alert(1)</script>\n\ndata: javascript:x\n\ndata: TkVX\n\n${END}`));
    const onFrame = vi.fn();
    await openFrames("r1", "m1", onFrame, new AbortController().signal);
    expect(onFrame.mock.calls.map(([d]) => d)).toEqual(["QUJD", "TkVX"]);
  });

  it("retries a dropped connection with backoff, starting over after a successful connect", async () => {
    let n = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      n += 1;
      if (n === 1) throw new TypeError("offline");
      if (n === 2) throw new TypeError("offline");
      if (n === 3) return dropping();
      return sse(END);
    });
    const onFrame = vi.fn();
    const done = openFrames("r1", "m1", onFrame, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(frameCalls(fetchMock)).toBe(1);
    await vi.advanceTimersByTimeAsync(1000); // 1 s after the first failure
    expect(frameCalls(fetchMock)).toBe(2);
    await vi.advanceTimersByTimeAsync(1999);
    expect(frameCalls(fetchMock)).toBe(2);
    await vi.advanceTimersByTimeAsync(1); // 2 s after the second
    expect(frameCalls(fetchMock)).toBe(3);
    expect(onFrame).toHaveBeenCalledWith("QUJD");
    await vi.advanceTimersByTimeAsync(1000); // connected, then dropped: back to 1 s, not 4 s
    expect(frameCalls(fetchMock)).toBe(4);
    await expect(done).resolves.toBe("ended");
  });

  it("gives up after MAX_RETRIES failures in a row", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 502 }));
    const done = openFrames("r1", "m1", vi.fn(), new AbortController().signal);
    await vi.advanceTimersByTimeAsync(60_000);
    await expect(done).resolves.toBe("gave-up");
    expect(frameCalls(fetchMock)).toBe(MAX_RETRIES + 1);
  });

  it("stops when aborted, also while waiting to retry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const ctl = new AbortController();
    const done = openFrames("r1", "m1", vi.fn(), ctl.signal);
    await vi.advanceTimersByTimeAsync(10);
    ctl.abort();
    await expect(done).resolves.toBe("aborted");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(frameCalls(fetchMock)).toBe(1);
  });
});

describe("followFrames", () => {
  it("reopens a stream that ended or 404ed while the session is still open, until aborted", async () => {
    const replies = [() => sse(`data: QUJD\n\n${END}`), () => new Response("", { status: 404 }), () => sse(`data: TkVX\n\n${END}`)];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => (replies.shift() ?? (() => new Response("", { status: 404 })))());
    const onFrame = vi.fn();
    const ctl = new AbortController();
    const done = followFrames("r1", "m1", onFrame, ctl.signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(onFrame).toHaveBeenCalledWith("QUJD");
    await vi.advanceTimersByTimeAsync(1000); // after a clean end: reconnect (gets 404)
    expect(frameCalls(fetchMock)).toBe(2);
    await vi.advanceTimersByTimeAsync(1999); // 404 with no frames: the wait doubles
    expect(frameCalls(fetchMock)).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(frameCalls(fetchMock)).toBe(3);
    expect(onFrame).toHaveBeenLastCalledWith("TkVX");
    ctl.abort();
    await expect(done).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(frameCalls(fetchMock)).toBe(3);
  });
});
