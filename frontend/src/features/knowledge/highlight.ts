export interface Segment {
  text: string;
  hit: boolean;
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Split `text` into segments, marking words that start with any of the (lowercase) query terms. */
export function highlight(text: string, terms: string[]): Segment[] {
  const usable = terms.filter(Boolean);
  if (!usable.length) return [{ text, hit: false }];
  const re = new RegExp(`\\b(${usable.map(escape).join("|")})[a-z0-9]*`, "gi");
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ text: text.slice(last, at), hit: false });
    out.push({ text: m[0], hit: true });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out;
}
