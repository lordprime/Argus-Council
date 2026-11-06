import { RotateCcw } from "lucide-react";
import { useNavigate } from "react-router";

import { Button, type ButtonSize, type ButtonVariant } from "@/components/ui";
import { useResumeRun } from "@/features/runs/queries";
import { errorMessage } from "@/lib/api";
import { toast } from "@/stores/toast";

/**
 * Resume a stopped run where it left off: a new run that replays the old one's finished work (plan,
 * steps, report stages) without calling a model, then does only what never finished.
 */
export function ResumeRunButton({ runId, variant = "primary", size = "md" }: { runId: string; variant?: ButtonVariant; size?: ButtonSize }) {
  const navigate = useNavigate();
  const resume = useResumeRun(runId);
  const go = async () => {
    try {
      const run = await resume.mutateAsync({});
      navigate(`/runs/${run.id}`);
    } catch (err) {
      toast({ tone: "danger", title: "Could not resume the run", body: errorMessage(err) });
    }
  };
  return (
    <Button variant={variant} size={size} icon={RotateCcw} loading={resume.isPending} onClick={() => void go()}
      title="Continue from where this run stopped. Finished work is reused, not redone.">
      Resume where it stopped
    </Button>
  );
}
