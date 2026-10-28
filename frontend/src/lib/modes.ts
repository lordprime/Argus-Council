// The one mode -> label/icon map (audit C-13). Runs, councils, filters and the composer all read it,
// so a mode never has two icons or two spellings. Kept free of React components.

import { Code2, FileText, MessageCircle, Wand2, type LucideIcon } from "lucide-react";

import type { Mode } from "@/lib/types";

export interface ModeMeta {
  label: string;
  icon: LucideIcon;
  /** One-line description shown under the mode picker (ui-spec §3.2). */
  hint: string;
}

export const MODE_META: Record<Mode, ModeMeta> = {
  research: { label: "Research", icon: FileText, hint: "Gather, analyze, cite, write." },
  coding: { label: "Coding", icon: Code2, hint: "Design, implement, test in a sandbox." },
  chat: { label: "Chat", icon: MessageCircle, hint: "Conversational with oversight." },
  general: { label: "General", icon: Wand2, hint: "Anything else." },
};

/** Metadata for any mode string, including unknown ones from older servers (title-cased, generic icon). */
export function modeMeta(mode: string): ModeMeta {
  return MODE_META[mode as Mode] ?? { label: mode ? mode[0]!.toUpperCase() + mode.slice(1) : "General", icon: Wand2, hint: "" };
}
