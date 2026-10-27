import { describe, expect, it } from "vitest";

import { parseSse } from "@/lib/stream";

function streamOf(chunks: string[]) {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      chunks.forEach((x) => c.enqueue(enc.encode(x)));
      c.close();
    },
  });
}

describe("parseSse", () => {
  it("yields data payloads across chunk boundaries and skips comments", async () => {
    const out: string[] = [];
    for await (const d of parseSse(streamOf(['id: 1\nevent: run.status\ndata: {"a"', ':1}\n\n: keep-alive\n\nid: 2\ndata: {"b":2}\n\n'])))
      out.push(d);
    expect(out).toEqual(['{"a":1}', '{"b":2}']);
  });
});
