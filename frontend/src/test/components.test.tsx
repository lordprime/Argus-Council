import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApprovalCard } from "@/components/run/parts";
import { Markdown } from "@/components/ui/Markdown";
import { rosterFor } from "@/routes/Home";

afterEach(() => vi.restoreAllMocks());

describe("rosterFor mirrors the backend roster logic", () => {
  const rosters = { chat: ["assistant", "monitor", "critic", "researcher"], general: ["chair", "analyst"] };
  const overseers = new Set(["monitor", "critic"]);
  it("slices and pads with workers", () => {
    expect(rosterFor(rosters, overseers, "chat", 2)).toEqual(["assistant", "monitor"]);
    expect(rosterFor(rosters, overseers, "chat", 6)).toEqual(["assistant", "monitor", "critic", "researcher", "assistant", "researcher"]);
  });
});

describe("Markdown", () => {
  it("never renders raw HTML or javascript: links", () => {
    const { container } = render(<Markdown>{"<img src=x onerror=alert(1)> [x](javascript:alert(1)) [ok](https://ex.com)"}</Markdown>);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector('a[href^="javascript"]')).toBeNull();
    const ok = container.querySelector('a[href="https://ex.com"]');
    expect(ok?.getAttribute("rel")).toBe("noopener noreferrer nofollow");
  });
});

describe("ApprovalCard", () => {
  it("posts the decision and is accessible", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ decision: "approved" }), {
      status: 202, headers: { "content-type": "application/json" },
    }));
    const { container } = render(<ApprovalCard runId="r1" a={{ kind: "approval", id: "apr_1", seq: 1, ts: "2026-01-01T00:00:00Z",
      actor: { kind: "agent", name: "Coder" }, summary: "Coder wants to run shell(ls)", risk: ["shell"], status: "pending" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Approval needed");
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/runs/r1/approvals/apr_1", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ decision: "approve" });
  });
});
