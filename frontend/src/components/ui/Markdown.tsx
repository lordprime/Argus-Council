// Agent output is untrusted: no raw HTML, sanitized, links open safely and show their domain.

import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/cn";

function safeHref(href?: string): string | undefined {
  if (!href) return undefined;
  return /^(https?:|mailto:|#)/i.test(href) ? href : undefined;
}

type Level = 1 | 2 | 3 | 4 | 5 | 6;
const TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;
const PROSE_SIZE: Record<Level, string> = { 1: "text-2xl", 2: "text-xl", 3: "text-lg", 4: "text-base font-semibold", 5: "text-sm font-semibold", 6: "text-sm font-semibold" };

/**
 * Heading renderers that shift every Markdown heading down by `offset` levels (capped at h6), so
 * embedded agent or skill Markdown never adds a second h1 to the page. The visual size follows the
 * source level, so a deliverable still looks like its author wrote it.
 */
function headingComponents(offset: number): Partial<Components> {
  const out: Partial<Components> = {};
  TAGS.forEach((tag, i) => {
    const source = (i + 1) as Level;
    const Tag = TAGS[Math.min(5, i + offset)]!;
    out[tag] = ({ children: c }: { children?: ReactNode }) => <Tag className={PROSE_SIZE[source]}>{c}</Tag>;
  });
  return out;
}
const HEADINGS = [0, 1, 2, 3, 4, 5].map(headingComponents);

/**
 * `headingOffset` (default 1) demotes headings: `# Title` renders as an h2 under the page's h1.
 * Pass 0 only when the Markdown has already been demoted (for example by `demoteHeadings`).
 */
export function Markdown({ children, className, headingOffset = 1 }: { children: string; className?: string; headingOffset?: number }) {
  const headings = HEADINGS[Math.max(0, Math.min(5, Math.trunc(headingOffset)))]!;
  return (
    <div className={cn("prose-argus break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        skipHtml
        components={{
          ...headings,
          a: ({ href, children: c }) => {
            const safe = safeHref(href);
            if (!safe) return <span>{c}</span>;
            let host = "";
            try {
              host = new URL(safe, window.location.href).host;
            } catch {
              host = "";
            }
            return (
              <a href={safe} target="_blank" rel="noopener noreferrer nofollow" title={host || safe}>
                {c}
              </a>
            );
          },
          img: ({ alt }) => <span className="text-fg-subtle">[image: {alt || "omitted"}]</span>,
          pre: ({ children: c }) => (
            <pre tabIndex={0} role="region" aria-label="Code block">
              {c}
            </pre>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
