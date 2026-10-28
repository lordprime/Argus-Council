import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

// jsdom lacks these browser APIs used by the UI.
window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null, addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
})) as typeof window.matchMedia;
Element.prototype.scrollIntoView ??= function () {};
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
// axe-core probes icon-font ligatures through a 2D canvas, which jsdom does not implement; without
// this it logs a "Not implemented: HTMLCanvasElement.prototype.getContext" error for every check.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
