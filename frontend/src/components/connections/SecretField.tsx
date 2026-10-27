import { KeyRound, RotateCcw } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui";

export type SecretAction = { kind: "keep" } | { kind: "replace"; value: string } | { kind: "clear" };

/**
 * Write-only secret input (ui-spec "Secrets fields are write-only"). A stored secret is never rendered: the
 * server only reports that one exists. The user can Replace it (type a new one) or Remove it.
 */
export function SecretField({ label, stored, action, onChange, hint, required, clearable = true }: {
  label: string;
  stored: boolean;
  action: SecretAction;
  onChange: (a: SecretAction) => void;
  hint?: string;
  required?: boolean;
  clearable?: boolean;
}) {
  const id = useId();
  const editing = !stored || action.kind === "replace";
  const describe = `${id}-hint`;
  return (
    <div className="flex flex-col gap-1">
      {editing ? (
        <label htmlFor={id} id={`${id}-label`} className="text-sm font-medium">{label}</label>
      ) : (
        <span id={`${id}-label`} className="text-sm font-medium">{label}</span>
      )}
      {editing ? (
        <div className="flex gap-2">
          <input
            id={id}
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            required={required && !stored}
            aria-describedby={describe}
            value={action.kind === "replace" ? action.value : ""}
            onChange={(e) => onChange({ kind: "replace", value: e.target.value })}
            placeholder={stored ? "Enter a new value" : "Paste the secret"}
            className="w-full h-9 rounded-md border border-border-strong bg-surface px-3 text-base text-fg placeholder:text-fg-subtle font-mono"
          />
          {stored && (
            <Button type="button" variant="ghost" icon={RotateCcw} onClick={() => onChange({ kind: "keep" })}>
              Keep current
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-labelledby={`${id}-label`}>
          <span className="inline-flex items-center gap-2 h-9 rounded-md border border-border bg-surface-2 px-3 text-sm text-fg-muted">
            <KeyRound className="size-4" aria-hidden />
            {action.kind === "clear" ? "Will be removed on save" : "Stored · write-only"}
          </span>
          {action.kind === "clear" ? (
            <Button type="button" size="sm" variant="ghost" onClick={() => onChange({ kind: "keep" })}>Undo</Button>
          ) : (
            <>
              <Button type="button" size="sm" onClick={() => onChange({ kind: "replace", value: "" })}>Replace</Button>
              {clearable && (
                <Button type="button" size="sm" variant="danger-ghost" onClick={() => onChange({ kind: "clear" })}>Remove</Button>
              )}
            </>
          )}
        </div>
      )}
      <p id={describe} className="text-xs text-fg-subtle">
        {hint ?? "Write-only: stored encrypted and never shown again."}
      </p>
    </div>
  );
}

/** Maps a SecretAction to a PATCH value: undefined = keep, null = clear, string = replace (blank = keep). */
export function secretPatch(a: SecretAction): string | null | undefined {
  if (a.kind === "clear") return null;
  if (a.kind === "replace" && a.value.trim()) return a.value.trim();
  return undefined;
}
