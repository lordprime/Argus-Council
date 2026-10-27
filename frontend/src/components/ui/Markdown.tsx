// Agent output is untrusted: no raw HTML, sanitized, links open safely and show their domain.

import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/cn";

function safeHref(href?: string): string | undefined {
  if (!href) return undefined;
  return /^(https?:|mailto:|#)/i.test(href) ? href : undefined;
}

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("prose-argus break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        skipHtml
        components={{
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
