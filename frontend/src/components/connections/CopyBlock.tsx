import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { IconButton } from "@/components/ui";
import { toast } from "@/stores/toast";

/** A labelled, keyboard-scrollable snippet with a copy button. */
export function CopyBlock({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast({ tone: "warning", title: "Copy failed", body: "Select the text and copy it manually." });
    }
  };
  return (
    <div className="flex items-start gap-1 rounded-md border border-border bg-surface-2">
      <pre tabIndex={0} role="region" aria-label={label}
        className="flex-1 min-w-0 overflow-x-auto px-3 py-2 font-mono text-xs leading-5 text-fg whitespace-pre">
        {value}
      </pre>
      <IconButton label={copied ? "Copied" : `Copy ${label}`} icon={copied ? Check : Copy} className="m-1 shrink-0"
        onClick={() => void copy()} />
    </div>
  );
}
