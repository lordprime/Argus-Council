import { AlertTriangle, ExternalLink } from "lucide-react";
import { useId, useState } from "react";

import { CopyBlock } from "@/components/connections/CopyBlock";
import { SettingsLayout, SettingsSection } from "@/components/settings/SettingsNav";
import { Badge, Callout, Card, ErrorState, Skeleton, SwitchTrack, switchHit } from "@/components/ui";
import { useAdminFeatures, useIsAdmin, useSetFeature } from "@/features/admin/queries";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import type { FeatureKey, FeatureOut, FeatureStatus } from "@/lib/types";
import { toast } from "@/stores/toast";

const THREAT_MODEL_URL =
  "https://github.com/lordprime/Argus-Council/blob/main/docs/security/threat-model.md#tm-13-a-web-page-attacks-the-agents-browser-or-uses-it-to-reach-or-leak-data";

const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

const STATUS_BADGE: Record<FeatureStatus, { tone: "success" | "warning" | "neutral"; label: string }> = {
  reachable: { tone: "success", label: "Installed and reachable" },
  unreachable: { tone: "warning", label: "Installed but unreachable" },
  not_installed: { tone: "neutral", label: "Not installed" },
};

/** Per-feature copy. Unknown keys from a newer server fall back to the label alone. */
const COPY: Record<FeatureKey, { description: string; offEffect: string; container: string }> = {
  browser: {
    description: "Lets council members open and read web pages in a real Chrome on the server, which you can watch live in the run.",
    offEffect: "Turning it off takes effect from the next browse call, including in runs already going.",
    container: "browser",
  },
  cli_agents: {
    description: "Seats Claude Code, the Codex CLI or the Gemini CLI on a council, signed in with each user's own account or CLI API key.",
    offEffect: "Turning it off takes effect from the next CLI turn, including in runs already going.",
    container: "runner",
  },
};

function FeatureSwitch({ f }: { f: FeatureOut }) {
  const id = useId();
  const set = useSetFeature();
  const [error, setError] = useState<string | null>(null);
  const copy = COPY[f.key] as (typeof COPY)[FeatureKey] | undefined;
  const label = `Enable ${f.label}`;

  const toggle = (enabled: boolean) => {
    setError(null);
    set.mutate({ key: f.key, enabled }, {
      onSuccess: (r) => toast({ tone: "success", title: `${r.label} turned ${r.enabled ? "on" : "off"}` }),
      onError: (e) => setError(errorMessage(e)),
    });
  };

  const describedBy = [copy && `${id}-hint`, error && `${id}-err`].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start gap-3">
        <button id={id} type="button" role="switch" aria-checked={f.enabled} disabled={set.isPending}
          aria-invalid={error ? true : undefined} aria-describedby={describedBy} onClick={() => toggle(!f.enabled)}
          className={cn(switchHit, "-my-0.5 [@media(pointer:coarse)]:-my-3")}>
          <SwitchTrack checked={f.enabled} />
        </button>
        <div className="text-sm min-w-0">
          <label htmlFor={id} className="font-medium cursor-pointer">{label}</label>
          {copy && <p id={`${id}-hint`} className="text-xs text-fg-subtle">{copy.offEffect}</p>}
          {f.updated_at && (
            <p className="text-xs text-fg-muted mt-1">
              Last changed{f.updated_by ? <> by <span className="break-all">{f.updated_by}</span></> : null}{" "}
              <time dateTime={f.updated_at} title={fullDate(f.updated_at)}>{relTime(f.updated_at)}</time>
            </p>
          )}
          {error && <p id={`${id}-err`} role="alert" className="mt-1 text-xs text-danger">{error}</p>}
        </div>
      </div>
      {f.status === "unreachable" && f.enabled && (
        <div className="flex gap-2 rounded-md border border-border bg-warning-subtle px-3 py-2 text-sm text-fg">
          <AlertTriangle className="size-4 shrink-0 mt-0.5 text-warning" aria-hidden />
          <p>
            The switch is on, but the service isn't answering. Check the{" "}
            {copy ? <code className="font-mono text-xs">{copy.container}</code> : "feature's"} container on the server.
          </p>
        </div>
      )}
    </div>
  );
}

function NotInstalled({ f }: { f: FeatureOut }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-fg-muted">To install it, run this on the server, from the Argus Council folder:</p>
      <CopyBlock label={`${f.label} install command`} value={f.install_command} />
      <p className="text-xs text-fg-subtle">
        It runs on the server, not in the app: the app never installs containers. Once it's installed, the switch appears here.
      </p>
    </div>
  );
}

function FeatureCard({ f }: { f: FeatureOut }) {
  const badge = STATUS_BADGE[f.status] ?? STATUS_BADGE.not_installed;
  const copy = COPY[f.key] as (typeof COPY)[FeatureKey] | undefined;
  return (
    <SettingsSection id={`feature-${f.key}`} title={f.label} description={copy?.description}
      badge={<Badge tone={badge.tone}>{badge.label}</Badge>}>
      {f.installed ? <FeatureSwitch f={f} /> : <NotInstalled f={f} />}
      {f.key === "browser" && (
        <p className="text-xs text-fg-subtle">
          Residual risk: Chrome runs with <code className="font-mono">--no-sandbox</code> inside its container. Isolation
          comes from the container, its seccomp and capability limits, and the egress proxy.{" "}
          <a href={THREAT_MODEL_URL} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-accent underline">
            Threat model (TM-13)<ExternalLink className="size-3" aria-hidden />
          </a>
        </p>
      )}
      {f.key === "cli_agents" && (
        <p className="text-xs text-fg-subtle">
          While it's off, CLI-agent seats are refused (a seat's fallback route takes over). Users' CLI sign-ins and keys are kept.
        </p>
      )}
    </SettingsSection>
  );
}

function FeatureList() {
  const q = useAdminFeatures();
  if (q.isLoading) {
    return (
      <div role="status" aria-label="Loading features" className="flex flex-col gap-6">
        <Card as="div" className="p-4 space-y-3"><Skeleton className="h-6 w-40" /><Skeleton className="h-8" /></Card>
        <Card as="div" className="p-4 space-y-3"><Skeleton className="h-6 w-40" /><Skeleton className="h-8" /></Card>
      </div>
    );
  }
  if (q.error) return <Card as="div"><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>;
  return <>{(q.data ?? []).map((f) => <FeatureCard key={f.key} f={f} />)}</>;
}

export default function Features() {
  const admin = useIsAdmin();
  return (
    <SettingsLayout title="Features"
      description="Features are installed once on the server and switched on and off here at any time. The server's settings are the ceiling: a switch can only turn on what the server has installed.">
      {admin ? (
        <FeatureList />
      ) : (
        <Callout tone="warning" title="Admins only">Ask an admin if you need a feature turned on or off.</Callout>
      )}
    </SettingsLayout>
  );
}
