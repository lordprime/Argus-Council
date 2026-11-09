// The Browser view: watch each agent's browser live (watch-only), replay its saved screenshots, and
// the ledger's Browser filter.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserView } from "@/components/run/BrowserView";
import type { Member, RunDetail, RunEvent } from "@/lib/types";
import RunWorkspace from "@/routes/RunWorkspace";
import Ledger from "@/routes/connections/Ledger";
import { emptyRun, reduceAll, useRuns, type RunState } from "@/stores/run";

const frames = vi.hoisted(() => ({ openFrames: vi.fn() }));
vi.mock("@/lib/frames", () => frames);

// A JPEG body as bytes. Not `new Blob(...)`: under jsdom that is jsdom's Blob, which Node's
// Response handles differently by version (Node 24 stringifies it; Node 22's undici treats it as
// blob-like and calls the stream() jsdom lacks, so reading the body fails).
const jpegResponse = () =>
  new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { status: 200, headers: { "content-type": "image/jpeg" } });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const MEMBERS: Member[] = [
  { id: "m1", name: "Ana", role: "analyst", provider: "mock", model: "mock-1", system_prompt: "", temperature: 0.3, position: 0, enabled: true, config: {} },
];
const ANA = { kind: "agent" as const, member_id: "m1", role: "analyst", name: "Ana" };

let seq = 0;
const ev = (type: string, data: Record<string, unknown>): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r1", ts: "2026-01-01T00:00:00Z", type, actor: ANA, data,
});

function withBrowser(opts: { closed?: boolean } = {}): RunState {
  seq = 0;
  const events = [
    ev("browser.session.opened", { member_id: "m1", session_id: "s1" }),
    ev("browser.action", { member_id: "m1", session_id: "s1", action: "open", target: "https://example.com/", url: "https://example.com/", title: "Example", ok: true, error: null, latency_ms: 40 }),
    ev("browser.snapshot.saved", { member_id: "m1", path: "browser/m1/1.jpg", url: "https://example.com/", n: 1 }),
    ev("browser.action", { member_id: "m1", session_id: "s1", action: "click", target: "@e3", url: "https://example.com/next", title: "Next", ok: true, error: null, latency_ms: 30 }),
    ev("browser.snapshot.saved", { member_id: "m1", path: "browser/m1/2.jpg", url: "https://example.com/next", n: 2 }),
    ev("browser.action", { member_id: "m1", session_id: "s1", action: "get_text", target: null, url: "https://example.com/next", title: "Next", ok: false, error: "timed out", latency_ms: 30000 }),
  ];
  if (opts.closed) events.push(ev("browser.session.closed", { member_id: "m1", session_id: "s1" }));
  return reduceAll(emptyRun("r1", MEMBERS), events);
}

beforeEach(() => {
  frames.openFrames.mockReset();
  frames.openFrames.mockImplementation(() => new Promise<void>(() => {}));
  try {
    localStorage.removeItem("argus.runView");
  } catch {
    /* storage unavailable */
  }
  Object.assign(URL, { createObjectURL: vi.fn((b: Blob) => `blob:${b.size}-${Math.random()}`), revokeObjectURL: vi.fn() });
});
afterEach(() => vi.restoreAllMocks());

describe("browser events in the store", () => {
  it("reduces the session, actions and screenshots into a trail per member", () => {
    const s = withBrowser();
    const t = s.browser.m1!;
    expect(t).toMatchObject({ sessionId: "s1", url: "https://example.com/next", title: "Next", open: true });
    expect(t.actions.map((a) => a.action)).toEqual(["open", "click", "get_text"]);
    expect(t.actions[2]).toMatchObject({ ok: false, error: "timed out", target: null });
    expect(t.shots).toEqual([
      { path: "browser/m1/1.jpg", url: "https://example.com/", n: 1 },
      { path: "browser/m1/2.jpg", url: "https://example.com/next", n: 2 },
    ]);
    expect(withBrowser({ closed: true }).browser.m1!.open).toBe(false);
  });

  it("does not record a screenshot that was not kept", () => {
    const s = reduceAll(withBrowser(), [ev("browser.snapshot.saved", { member_id: "m1", url: "https://x.test/", kept: false, reason: "limit" })]);
    expect(s.browser.m1!.shots).toHaveLength(2);
  });
});

describe("BrowserView", () => {
  it("shows the live frame for each browsing member", async () => {
    frames.openFrames.mockImplementation(async (_run: string, _member: string, onFrame: (b: string) => void) => {
      onFrame("QUJD");
      return new Promise<void>(() => {});
    });
    render(<BrowserView runId="r1" run={withBrowser()} live />);
    const img = await screen.findByRole("img", { name: "Live view of Ana's browser" });
    await waitFor(() => expect(img.getAttribute("src")).toBe("data:image/jpeg;base64,QUJD"));
    expect(frames.openFrames).toHaveBeenCalledWith("r1", "m1", expect.any(Function), expect.any(AbortSignal));
    expect(screen.getAllByText("https://example.com/next").length).toBeGreaterThan(0);
    // The last actions, failures in words.
    expect(screen.getByText(/timed out/)).toBeInTheDocument();
  });

  it("the live image has no interaction handlers", async () => {
    frames.openFrames.mockImplementation(async (_r: string, _m: string, onFrame: (b: string) => void) => {
      onFrame("QUJD");
      return new Promise<void>(() => {});
    });
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { container } = render(<BrowserView runId="r1" run={withBrowser()} live />);
    const img = await screen.findByRole("img", { name: "Live view of Ana's browser" });
    const before = JSON.stringify(useRuns.getState().runs);
    fireEvent.click(img);
    fireEvent.keyDown(img, { key: "Enter" });
    fireEvent.pointerDown(img);
    fireEvent.mouseMove(img);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(JSON.stringify(useRuns.getState().runs)).toBe(before);
    const area = img.closest("[data-frame-area]")!;
    expect(area.querySelector("input, button, textarea, select, a")).toBeNull();
    expect(img.getAttribute("tabindex")).toBeNull();
    expect(container.querySelectorAll("input, textarea, select")).toHaveLength(0);
  });

  it("stops streaming when the view is left", async () => {
    let signal!: AbortSignal;
    frames.openFrames.mockImplementation((_r: string, _m: string, _f: unknown, s: AbortSignal) => {
      signal = s;
      return new Promise<void>(() => {});
    });
    const { unmount } = render(<BrowserView runId="r1" run={withBrowser()} live />);
    await waitFor(() => expect(frames.openFrames).toHaveBeenCalled());
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("does not stream a closed session", () => {
    render(<BrowserView runId="r1" run={withBrowser({ closed: true })} live />);
    expect(frames.openFrames).not.toHaveBeenCalled();
  });

  it("finished runs show saved screenshots with their actions", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => jpegResponse());
    const { unmount } = render(<BrowserView runId="r1" run={withBrowser({ closed: true })} live={false} />);
    expect(frames.openFrames).not.toHaveBeenCalled();
    const shots = await screen.findAllByRole("img", { name: /^Screenshot \d of / });
    expect(shots).toHaveLength(2);
    expect(shots[0]).toHaveAttribute("alt", "Screenshot 1 of https://example.com/");
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls).toContain("/api/v1/runs/r1/artifacts/download?path=" + encodeURIComponent("browser/m1/1.jpg"));
    expect(urls).toContain("/api/v1/runs/r1/artifacts/download?path=" + encodeURIComponent("browser/m1/2.jpg"));
    const list = screen.getByRole("list", { name: "Actions by Ana" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByText(/click/)).toBeInTheDocument();
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });
});

describe("Browser tab", () => {
  const RUN = {
    id: "r1", council_id: null, parent_run_id: null, topic: "Topic", description: "", mode: "research", status: "completed", outputs: ["md"],
    guardrails: {}, usage: { usd: 0, steps: 0, runtime_s: 1 }, error: null, last_seq: 3, created_at: "2026-10-27T10:00:00Z",
    started_at: "2026-10-27T10:00:00Z", finished_at: "2026-10-27T10:01:00Z", result: "", council_snapshot: { council_id: null, name: "c", members: MEMBERS },
  } as unknown as RunDetail;
  const STREAM = `data: ${JSON.stringify({ v: 1, seq: 3, id: "e3", run_id: "r1", ts: "2026-10-27T10:01:00Z", type: "run.status", actor: { kind: "system" }, data: { status: "completed" } })}\n\n`;

  function mount(state: RunState) {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input).split("?")[0];
      if (path === "/api/v1/runs/r1") return json(RUN);
      if (path === "/api/v1/runs/r1/artifacts") return json({ artifacts: [] });
      if (path === "/api/v1/runs/r1/events/stream") return new Response(STREAM, { status: 200, headers: { "content-type": "text/event-stream" } });
      return jpegResponse();
    });
    useRuns.setState({ runs: { r1: { ...state, status: "completed", lastSeq: 100 } } });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/runs/r1"]}>
          <Routes><Route path="/runs/:runId/*" element={<RunWorkspace />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("appears only after a browser session", async () => {
    const first = mount(emptyRun("r1", MEMBERS));
    await screen.findByRole("heading", { level: 1, name: "Topic" });
    expect(screen.queryByRole("tab", { name: "Browser" })).not.toBeInTheDocument();
    first.unmount();

    mount(withBrowser({ closed: true }));
    const tab = await screen.findByRole("tab", { name: "Browser" });
    await act(async () => fireEvent.click(tab));
    expect(await screen.findByRole("list", { name: "Actions by Ana" })).toBeInTheDocument();
  });
});

describe("Ledger", () => {
  it("offers Browser", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v1/ledger")) return json({ entries: [], next_before_seq: null });
      return json({ error: { code: "not_found", message: url } }, 404);
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Ledger /></MemoryRouter></QueryClientProvider>);
    const group = await screen.findByRole("radiogroup", { name: "Protocol" });
    fireEvent.click(within(group).getByRole("radio", { name: "Browser" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("protocol=browser"), expect.anything()));
  });
});
