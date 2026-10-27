// Minimal line diff (LCS) for comparing skill versions. Inputs are SKILL.md-sized, so O(n·m) is fine; very large
// inputs fall back to a whole-block replace rather than allocating a huge table.

export type DiffOp = { kind: "same" | "add" | "del"; text: string };

const MAX_CELLS = 4_000_000;

export function lineDiff(before: string, after: string): DiffOp[] {
  const a = before.split("\n");
  const b = after.split("\n");
  // Trim the common prefix and suffix first: most edits touch a small region.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const head: DiffOp[] = a.slice(0, start).map((text) => ({ kind: "same", text }));
  const tail: DiffOp[] = a.slice(endA).map((text) => ({ kind: "same", text }));
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  const n = x.length;
  const m = y.length;
  if (n * m > MAX_CELLS) {
    return [...head, ...x.map((text) => ({ kind: "del" as const, text })), ...y.map((text) => ({ kind: "add" as const, text })), ...tail];
  }
  // lcs[i][j] = LCS length of x[i:], y[j:]
  const lcs = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] = x[i] === y[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const mid: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { mid.push({ kind: "same", text: x[i]! }); i++; j++; }
    else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) mid.push({ kind: "del", text: x[i++]! });
    else mid.push({ kind: "add", text: y[j++]! });
  }
  while (i < n) mid.push({ kind: "del", text: x[i++]! });
  while (j < m) mid.push({ kind: "add", text: y[j++]! });
  return [...head, ...mid, ...tail];
}

export function diffStats(ops: DiffOp[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const o of ops) {
    if (o.kind === "add") added++;
    else if (o.kind === "del") removed++;
  }
  return { added, removed };
}

/** Serialise a skill snapshot (SKILL.md plus bundled files) into one text so a single diff covers both. */
export function snapshotText(skillMd: string, files: Record<string, string>): string {
  const parts = [skillMd.trimEnd()];
  for (const [path, content] of Object.entries(files).sort(([p], [q]) => p.localeCompare(q))) {
    parts.push("", `=== ${path} ===`, content.trimEnd());
  }
  return parts.join("\n") + "\n";
}
