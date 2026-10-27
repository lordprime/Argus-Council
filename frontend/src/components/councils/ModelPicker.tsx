// Provider select + model field (suggested models via datalist, free text allowed for self-hosted).
// Providers are grouped by how they authenticate: API keys, or the user's own vendor account (CLI agents).

import { useId } from "react";

import { Badge, SelectField, TextField } from "@/components/ui";
import { useProviderOptions, type ProviderOption } from "@/features/councils/queries";
import { cn } from "@/lib/cn";

/** "Account" (CLI agent signed in with the user's vendor account) or "API key" badge for a provider option. */
export function AuthBadge({ auth }: { auth?: ProviderOption["auth"] }) {
  if (auth === "account") return <Badge tone="accent">Account</Badge>;
  if (auth === "api_key") return <Badge>API key</Badge>;
  return null;
}

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
  const apiOptions = all.filter((o) => o.auth !== "account");
  const accountOptions = all.filter((o) => o.auth === "account");
  const render = (o: ProviderOption) => <option key={o.value} value={o.value}>{o.label}</option>;

  return (
    <div className={cn("grid gap-4 sm:grid-cols-2", className)}>
      <SelectField label={providerLabel} value={provider} disabled={isLoading} error={errors?.provider}
        hint={onlyMock ? "Only the offline demo is available. Connect a provider for real runs."
          : current?.auth === "account" ? "Runs the vendor's CLI with your own sign-in or CLI key (Connections > CLI agents)." : undefined}
        onChange={(e) => {
          const next = all.find((o) => o.value === e.target.value);
          onChange({ provider: e.target.value, model: next?.models[0] ?? "" });
        }}>
        {accountOptions.length ? (
          <>
            <optgroup label="API key">{apiOptions.map(render)}</optgroup>
            <optgroup label="Account (CLI agents)">{accountOptions.map(render)}</optgroup>
          </>
        ) : apiOptions.map(render)}
      </SelectField>
      <TextField label={modelLabel} value={model} list={listId} autoComplete="off" spellCheck={false} error={errors?.model}
        hint={current?.models.length ? `Suggested: ${current.models.slice(0, 3).join(", ")}` : "Type the model id."}
        onChange={(e) => onChange({ provider, model: e.target.value })} />
      <datalist id={listId}>
        {current?.models.map((m) => <option key={m} value={m} />)}
      </datalist>
      {current?.auth && (
        <p className="sm:col-span-2 -mt-2 flex items-center gap-2 text-xs text-fg-subtle" data-testid="auth-badge">
          <AuthBadge auth={current.auth} />
          {current.auth === "account" ? "Uses your vendor sign-in (counts against your plan, no API dollars) or your CLI key." : "Billed to the provider's API key."}
        </p>
      )}
    </div>
  );
}
