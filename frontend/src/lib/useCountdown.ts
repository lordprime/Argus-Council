// Whole-second countdown (rate-limit retry hints). Returns the seconds left; 0 when done or when
// `seconds` is empty. Changing `key` restarts it with the same length.

import { useEffect, useState } from "react";

export function useCountdown(seconds: number | null | undefined, key?: unknown): number {
  const total = Math.max(0, Math.ceil(seconds ?? 0));
  const [left, setLeft] = useState(total);
  useEffect(() => {
    setLeft(total);
    if (!total) return;
    const end = Date.now() + total * 1000;
    const id = window.setInterval(() => {
      const s = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      setLeft(s);
      if (!s) window.clearInterval(id);
    }, 250);
    return () => window.clearInterval(id);
  }, [total, key]);
  return left;
}
