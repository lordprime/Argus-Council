import { Trash2 } from "lucide-react";
import type { ReactNode } from "react";

import { Button, Callout, Dialog } from "@/components/ui";
import { errorMessage } from "@/lib/api";

/**
 * Destructive confirmation that names the object (ui-spec "Security-related UI rules").
 * The confirm button repeats the name so screen-reader users hear exactly what will be deleted.
 */
export function ConfirmDelete({ open, onOpenChange, kind, name, consequence, onConfirm, pending, error }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: string;
  name: string;
  consequence?: ReactNode;
  onConfirm: () => void;
  pending?: boolean;
  error?: unknown;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={`Delete ${kind} “${name}”?`}
      description="This cannot be undone."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="danger" icon={Trash2} loading={pending} onClick={onConfirm}>
            Delete {name}
          </Button>
        </>
      }>
      {consequence && <div className="text-sm text-fg-muted">{consequence}</div>}
      {error != null && (
        <div className="mt-3">
          <Callout tone="danger" title={`Could not delete ${name}`}>{errorMessage(error)}</Callout>
        </div>
      )}
    </Dialog>
  );
}
