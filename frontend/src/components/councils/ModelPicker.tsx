// Provider select + model field (suggested models via datalist, free text allowed for self-hosted).

import { useId } from "react";

import { SelectField, TextField } from "@/components/ui";
import { useProviderOptions } from "@/features/councils/queries";
import { cn } from "@/lib/cn";

export function ModelPicker({ provider, model, onChange, providerLabel = "Provider", modelLabel = "Model", className, errors }: {
  provider: string;
  model: string;
  onChange: (next: { provider: string; model: string }) => void;
  providerLabel?: string;
  modelLabel?: string;
  className?: string;
  errors?: { provider?: string; model?: string };
}) {
  const listId = useId();
  const { options, isLoading } = useProviderOptions();
  const current = options.find((o) => o.value === provider);
  // Keep an unknown (e.g. since-deleted) provider selectable so editing never silently changes it.
  const all = current || !provider ? options : [...options, { value: provider, label: `${provider} (not configured)`, models: [] }];
  const onlyMock = !isLoading && options.every((o) => o.value === "mock");

  return (
    <div className={cn("grid gap-4 sm:grid-cols-2", className)}>
      <SelectField label={providerLabel} value={provider} disabled={isLoading} error={errors?.provider}
        hint={onlyMock ? "Only the offline demo is available. Connect a provider for real runs." : undefined}
        onChange={(e) => {
          const next = all.find((o) => o.value === e.target.value);
          onChange({ provider: e.target.value, model: next?.models[0] ?? "" });
        }}>
        {all.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </SelectField>
      <TextField label={modelLabel} value={model} list={listId} autoComplete="off" spellCheck={false} error={errors?.model}
        hint={current?.models.length ? `Suggested: ${current.models.slice(0, 3).join(", ")}` : "Type the model id."}
        onChange={(e) => onChange({ provider, model: e.target.value })} />
      <datalist id={listId}>
        {current?.models.map((m) => <option key={m} value={m} />)}
      </datalist>
    </div>
  );
}
