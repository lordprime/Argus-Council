// In-app "Reduce motion" override (ui-spec §8.13). The OS preference is always honored by CSS;
// this forces reduced motion on top of it via :root[data-motion="reduced"].

export const MOTION_KEY = "argus.motion";

export function readReducedMotion(): boolean {
  try {
    return localStorage.getItem(MOTION_KEY) === "reduced";
  } catch {
    return false;
  }
}

export function applyReducedMotion(on: boolean) {
  if (on) document.documentElement.dataset.motion = "reduced";
  else delete document.documentElement.dataset.motion;
}

export function setReducedMotion(on: boolean) {
  try {
    if (on) localStorage.setItem(MOTION_KEY, "reduced");
    else localStorage.removeItem(MOTION_KEY);
  } catch {
    /* storage disabled: applies for this session only */
  }
  applyReducedMotion(on);
}
