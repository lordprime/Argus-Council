// Modal side sheet for the run workspace's small-screen fallbacks (audit C-03): the inspector opens as a
// right drawer below 1280px and the council rail as a bottom sheet below 768px. Radix Dialog provides the
// focus trap, Escape, focus return and aria-modal.

import * as RDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";

import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";

export function Drawer({ open, onOpenChange, title, side = "right", children }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "right" | "bottom";
  children: ReactNode;
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-drawer bg-black/40" />
        <RDialog.Content
          aria-describedby={undefined}
          className={cn("fixed z-drawer flex flex-col bg-surface border-border shadow-3 focus-visible:outline-none animate-enter",
            side === "right"
              ? "inset-y-0 right-0 w-full sm:w-[480px] sm:border-l"
              : "inset-x-0 bottom-0 max-h-[85dvh] rounded-t-xl border-t")}>
          <div className="flex items-center gap-2 border-b border-border px-3 h-12 shrink-0">
            <RDialog.Title className="text-md font-semibold flex-1 truncate">{title}</RDialog.Title>
            <RDialog.Close asChild>
              <IconButton label={`Close ${title.toLowerCase()}`} icon={X} size="md" />
            </RDialog.Close>
          </div>
          <div className="flex-1 min-h-0 flex flex-col overflow-auto">{children}</div>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
