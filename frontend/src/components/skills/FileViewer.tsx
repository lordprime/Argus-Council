import { FileCode2, FileText } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/cn";

const CODE = /\.(py|js|ts|json|ya?ml|toml|sh|sql|css|html|xml|svg|tex|bib|cfg|ini|jinja|j2|csv)$/i;

/** Read-only viewer for a skill's bundled text files. Content is shown as plain text, never rendered as HTML. */
export function FileViewer({ files }: { files: Record<string, string> }) {
  const paths = Object.keys(files).sort();
  const [current, setCurrent] = useState(paths[0] ?? "");
  const content = files[current] ?? "";
  const lines = content ? content.split("\n").length : 0;
  return (
    <div className="grid gap-3 md:grid-cols-[14rem_minmax(0,1fr)]">
      <ul aria-label="Files" className="flex md:flex-col gap-1 overflow-x-auto md:overflow-visible">
        {paths.map((p) => {
          const Icon = CODE.test(p) ? FileCode2 : FileText;
          return (
            <li key={p} className="shrink-0">
              <button type="button" aria-current={p === current ? "true" : undefined} onClick={() => setCurrent(p)} title={p}
                className={cn("w-full flex items-center gap-2 rounded-md px-2 h-8 text-left text-sm font-mono",
                  p === current ? "bg-accent-subtle text-accent font-medium" : "text-fg-muted hover:bg-surface-2 hover:text-fg")}>
                <Icon className="size-4 shrink-0" aria-hidden />
                <span className="truncate">{p}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className="min-w-0 rounded-md border border-border bg-surface-2">
        <div className="flex items-center justify-between gap-2 px-3 h-8 border-b border-border text-xs text-fg-subtle">
          <span className="font-mono truncate">{current}</span>
          <span className="tabular shrink-0">{lines} lines</span>
        </div>
        <pre tabIndex={0} role="region" aria-label={`Contents of ${current}`}
          className="p-3 overflow-auto max-h-[28rem] font-mono text-xs leading-5 whitespace-pre">{content}</pre>
      </div>
    </div>
  );
}
