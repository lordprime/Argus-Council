// Copy-on-write for the run reducer (implementation review L5). `reduceAll` used to copy `order`,
// `items` and `comms` (up to thousands of entries each) on every flush, even for a batch that only
// appended text to one thought. Now each collection is copied the first time a batch writes to it and
// shared with the previous state otherwise, so earlier states are never mutated and untouched
// collections keep their identity (their subscribers do not re-render).

type Collection = "order" | "items" | "comms";
type Draftable = { [K in Collection]: unknown };

const DRAFTS = new WeakMap<object, Set<Collection>>();

/** Mark `s` (a fresh shallow copy made by the reducer) as a draft whose collections are still shared. */
export function beginDraft(s: Draftable): void {
  DRAFTS.set(s, new Set());
}

export function endDraft(s: Draftable): void {
  DRAFTS.delete(s);
}

/**
 * The collection `k` of `s`, safe to mutate. In a draft, the first call copies it; outside a draft (a
 * caller applying events to a state it owns, as `applyEvent`'s contract allows) it is returned as is.
 */
export function writable<S extends Draftable, K extends Collection>(s: S, k: K): S[K] {
  const owned = DRAFTS.get(s);
  if (owned && !owned.has(k)) {
    owned.add(k);
    const v = s[k];
    s[k] = (Array.isArray(v) ? [...v] : { ...(v as object) }) as S[K];
  }
  return s[k];
}
