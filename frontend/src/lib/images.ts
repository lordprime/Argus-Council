// Run artifacts shown as images. An <img> cannot send the bearer token, and the app's CSP allows
// images only from 'self' and data: (no blob:), so the bytes are fetched with the token and handed to
// the <img> as a data: URL.

import { auth, V1 } from "./api";

const IMAGE_EXT = /\.(jpe?g|png|webp|gif)$/i;
const RASTER = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);

/** True when an artifact path is a raster image the inspector can show. */
export const isImagePath = (path: string) => IMAGE_EXT.test(path);

function typeOf(path: string, header: string | null): string {
  const t = (header ?? "").split(";")[0]!.trim().toLowerCase();
  if (RASTER.has(t)) return t;
  const ext = path.toLowerCase().match(IMAGE_EXT)?.[1];
  return ext === "png" ? "image/png" : ext === "webp" ? "image/webp" : ext === "gif" ? "image/gif" : "image/jpeg";
}

/** Bytes as base64, in chunks so a large screenshot does not overflow the argument list. */
export function toBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

/** Fetch a run artifact and return it as a data: URL (raster types only), or null when it can't be read. */
export async function artifactImage(runId: string, path: string, signal?: AbortSignal): Promise<string | null> {
  const resp = await fetch(`${V1}/runs/${runId}/artifacts/download?path=${encodeURIComponent(path)}`, {
    headers: { Authorization: `Bearer ${auth.token ?? ""}` },
    signal,
  });
  if (!resp.ok) return null;
  const bytes = new Uint8Array(await resp.arrayBuffer());
  return `data:${typeOf(path, resp.headers.get("content-type"))};base64,${toBase64(bytes)}`;
}
