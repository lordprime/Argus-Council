// A live run's budget: raise it now, or set the reserve it draws on instead of halting at its budget
// (POST /runs/{id}/budget). The run's budget meter and the timeline update from the run's events.

import * as Popover from "@radix-ui/react-popover";
import { Wallet } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Badge, Button, TextField } from "@/components/ui";
import { api, errorMessage, V1 } from "@/lib/api";
import { usd } from "@/lib/format";
import { useRun } from "@/stores/run";
import { toast } from "@/stores/toast";

export function BudgetControl({ runId }: { runId: string }) {
  const budget = useRun(runId, (s) => s.budget);
  const reserve = budget?.buffer?.usd ?? 0;
  const [open, setOpen] = useState(false);
  const [add, setAdd] = useState("");
  const [buffer, setBuffer] = useState("");
  const [busy, setBusy] = useState(false);
  const addN = Number(add);
  const bufferN = Number(buffer);
  const valid = (add === "" || addN > 0) && (buffer === "" || bufferN >= 0) && (add !== "" || buffer !== "");
  const apply = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    try {
      await api.post(`${V1}/runs/${runId}/budget`, {
        ...(add !== "" ? { add_usd: addN } : {}),
        ...(buffer !== "" ? { buffer_usd: bufferN } : {}),
      });
      setAdd("");
      setBuffer("");
      setOpen(false);
    } catch (err) {
      toast({ tone: "danger", title: "Could not change the budget", body: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button icon={Wallet} aria-label={`Budget${reserve ? `, ${usd(reserve)} reserve` : ""}`}>
          Budget{reserve > 0 && <Badge tone="accent" className="ml-1">+{usd(reserve)}</Badge>}
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="end" sideOffset={6} aria-label="Budget"
          className="pop-fx z-popover w-72 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface shadow-2 p-3">
          <form onSubmit={apply} className="flex flex-col gap-3">
            <p className="text-sm">
              {usd(budget?.used.usd ?? 0)} spent of {budget?.limits.usd == null ? "no cap" : usd(budget.limits.usd)}
              {reserve > 0 && <span className="text-fg-muted">, {usd(reserve)} in reserve</span>}.
            </p>
            <TextField label="Add to the budget now" type="number" min="0" step="0.5" inputMode="decimal" placeholder="0.00"
              value={add} onChange={(e) => setAdd(e.target.value)} />
            <TextField label="Reserve" type="number" min="0" step="0.5" inputMode="decimal" placeholder={String(reserve)}
              hint="Drawn without asking when the run reaches its budget, instead of halting."
              value={buffer} onChange={(e) => setBuffer(e.target.value)} />
            <Button type="submit" variant="primary" size="sm" loading={busy} disabled={!valid} className="self-end">Apply</Button>
          </form>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
