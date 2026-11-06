// Auto mode on a live run: two switches in a popover next to Pause/Stop. Approvals answers every
// request except limit raises; limit raises grants them within the run's extension policy. Neither
// widens what the guardrails block.

import * as Popover from "@radix-ui/react-popover";
import { Zap } from "lucide-react";

import { Badge, Button, Toggle } from "@/components/ui";
import { useSetAutoMode } from "@/features/runs/queries";
import { errorMessage } from "@/lib/api";
import type { AutoMode, Guardrails } from "@/lib/types";
import { useRun, useRuns } from "@/stores/run";
import { toast } from "@/stores/toast";

/** The run's auto mode: as last switched live, else what it was launched with. */
export function useAutoMode(runId: string, guardrails: Partial<Guardrails> | undefined): AutoMode {
  const live = useRun(runId, (s) => s.autoMode);
  return live ?? { tools: Boolean(guardrails?.auto_mode), extensions: Boolean(guardrails?.extensions?.auto_approve) };
}

export function AutoModeControl({ runId, guardrails }: { runId: string; guardrails: Partial<Guardrails> | undefined }) {
  const mode = useAutoMode(runId, guardrails);
  const setLocal = useRuns((s) => s.setAutoMode);
  const mutation = useSetAutoMode(runId);
  const on = mode.tools || mode.extensions;
  const change = async (flags: Partial<AutoMode>) => {
    try {
      setLocal(runId, await mutation.mutateAsync(flags));
    } catch (err) {
      toast({ tone: "danger", title: "Could not change auto mode", body: errorMessage(err) });
    }
  };
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <Button icon={Zap} aria-label={`Auto mode: ${on ? "on" : "off"}`}>
          Auto mode{on && <Badge tone="accent" className="ml-1">On</Badge>}
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="end" sideOffset={6} aria-label="Auto mode"
          className="pop-fx z-popover w-72 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface shadow-2 p-3 flex flex-col gap-3">
          <Toggle label="Approve requests" checked={mode.tools} disabled={mutation.isPending}
            hint="Agents run without asking for approval. Blocked actions stay blocked."
            onChange={(v) => void change({ tools: v })} />
          <Toggle label="Approve limit raises" checked={mode.extensions} disabled={mutation.isPending}
            hint="Raise the step, turn, runtime or token limit without asking, up to the run's extension cap."
            onChange={(v) => void change({ extensions: v })} />
          <p className="text-xs text-fg-subtle">Turning one on also answers the requests waiting for it now.</p>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
