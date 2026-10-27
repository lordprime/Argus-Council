import { CheckCircle2, FileUp, XCircle } from "lucide-react";
import { useId, useRef, useState, type DragEvent } from "react";

import { Button } from "@/components/ui";
import { uploadDocument, useInvalidateKnowledge } from "@/features/knowledge/queries";
import { ACCEPT, fileProblem } from "@/features/knowledge/types";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";

interface Item {
  key: string;
  name: string;
  progress: number;
  status: "uploading" | "done" | "error";
  message?: string;
}

/** Drag & drop or pick files; each upload shows progress and a per-file result. */
export function UploadZone({ kbId }: { kbId: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const [drag, setDrag] = useState(false);
  const [items, setItems] = useState<Item[]>([]);
  const invalidate = useInvalidateKnowledge();

  const patch = (key: string, p: Partial<Item>) => setItems((list) => list.map((i) => (i.key === key ? { ...i, ...p } : i)));

  const start = (files: FileList | File[] | null) => {
    if (!files) return;
    for (const file of Array.from(files)) {
      const key = `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const problem = fileProblem(file);
      const item: Item = { key, name: file.name, progress: 0, status: problem ? "error" : "uploading", message: problem ?? undefined };
      setItems((list) => [item, ...list].slice(0, 20));
      if (problem) continue;
      uploadDocument(kbId, file, (pct) => patch(key, { progress: pct }))
        .then((doc) => {
          patch(key, { status: "done", progress: 100, message: `${doc.chunk_count} chunk${doc.chunk_count === 1 ? "" : "s"}` });
          invalidate(kbId);
        })
        .catch((err: unknown) => patch(key, { status: "error", message: errorMessage(err) }));
    }
    if (inputRef.current) inputRef.current.value = "";
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDrag(false);
    start(e.dataTransfer.files);
  };

  return (
    <div className="space-y-2">
      <div
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
        data-testid="dropzone"
        className={cn("flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-5 text-center transition-colors",
          drag ? "border-accent bg-accent-subtle" : "border-border-strong bg-surface-2")}>
        <FileUp className="size-6 text-fg-subtle" aria-hidden />
        <p className="text-sm">Drop files here, or</p>
        <input ref={inputRef} type="file" multiple accept={ACCEPT} className="sr-only" tabIndex={-1} aria-hidden
          data-testid="file-input" onChange={(e) => start(e.target.files)} />
        <Button size="sm" icon={FileUp} aria-describedby={hintId} onClick={() => inputRef.current?.click()}>Choose files</Button>
        <p id={hintId} className="text-xs text-fg-subtle">Markdown, plain text or HTML · up to 5 MB each · PDF not supported yet</p>
      </div>
      {items.length > 0 && (
        <ul aria-label="Uploads" className="space-y-1.5">
          {items.map((i) => (
            <li key={i.key} className="rounded-md border border-border px-3 py-2 text-sm">
              <div className="flex items-center gap-2 min-w-0">
                {i.status === "done" ? <CheckCircle2 className="size-4 text-success shrink-0" aria-hidden />
                  : i.status === "error" ? <XCircle className="size-4 text-danger shrink-0" aria-hidden /> : null}
                <span className="truncate font-medium min-w-0 flex-1">{i.name}</span>
                <span className={cn("text-xs shrink-0", i.status === "error" ? "text-danger" : "text-fg-muted")}>
                  {i.status === "uploading" ? `${i.progress}%` : i.status === "done" ? `Uploaded · ${i.message}` : "Failed"}
                </span>
              </div>
              {i.status === "uploading" && (
                <progress className="mt-1.5 w-full h-1.5 accent-accent" max={100} value={i.progress}
                  aria-label={`Uploading ${i.name}`} />
              )}
              {i.status === "error" && i.message && <p role="alert" className="mt-1 text-xs text-danger break-words">{i.message}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
