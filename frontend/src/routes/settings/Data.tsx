import { Download, Eye, Play, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { SettingsLayout, SettingsSection } from "@/components/settings/SettingsNav";
import { Badge, Button, Callout, Dialog, ErrorState, Skeleton, TextField } from "@/components/ui";
import { useIsAdmin } from "@/features/admin/queries";
import {
  describeDays,
  formatBytes,
  useDeleteAccount,
  useExportData,
  useRetention,
  useRunRetention,
  type RetentionStats,
} from "@/features/data/queries";
import { ApiError, errorMessage } from "@/lib/api";
import { useSession } from "@/stores/session";
import { toast } from "@/stores/toast";

export const CONFIRM_PHRASE = "delete my account";

// ------------------------------------------------------------------ Export
function ExportSection() {
  const exp = useExportData();
  const download = async () => {
    try {
      const { name, size } = await exp.mutateAsync();
      toast({ tone: "success", title: "Export ready", body: `${name} (${formatBytes(size)})` });
    } catch {
      /* shown inline */
    }
  };
  return (
    <SettingsSection id="export" title="Export your data"
      description="Download a zip of your profile, councils, skills, connections, runs with their events, results and artifacts, and your audit trail. Secrets such as API keys and provider credentials are never included."
      footer={<Button variant="primary" icon={Download} loading={exp.isPending} onClick={download}>Download export</Button>}>
      {exp.error ? (
        <Callout tone="danger" title="Export failed">{errorMessage(exp.error)}</Callout>
      ) : (
        <p className="text-sm text-fg-muted">The archive is built on demand and can take a moment for large accounts.</p>
      )}
    </SettingsSection>
  );
}

// ------------------------------------------------------------------ Retention (admin)
const STAT_ROWS: { key: keyof RetentionStats; label: string; bytes?: boolean }[] = [
  { key: "runs", label: "Runs" },
  { key: "run_events", label: "Run events" },
  { key: "audit_events", label: "Audit events" },
  { key: "ledger_entries", label: "Ledger entries" },
  { key: "blob_dirs", label: "Artifact folders" },
  { key: "blob_bytes", label: "Artifact storage", bytes: true },
];

function StatsTable({ stats, caption }: { stats: RetentionStats; caption: string }) {
  return (
    <table className="w-full text-sm">
      <caption className="text-left text-sm font-medium text-fg mb-1">{caption}</caption>
      <tbody>
        {STAT_ROWS.map((r) => {
          const v = Number(stats[r.key] ?? 0);
          return (
            <tr key={r.key} className="border-t border-border">
              <th scope="row" className="py-1.5 pr-4 text-left font-normal text-fg-muted">{r.label}</th>
              <td className="py-1.5 text-right tabular-nums">{r.bytes ? formatBytes(v) : v.toLocaleString()}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function RetentionSection() {
  const q = useRetention();
  const run = useRunRetention();
  const [preview, setPreview] = useState<RetentionStats | null>(null);
  const [confirm, setConfirm] = useState(false);

  const doPreview = async () => {
    try {
      setPreview(await run.mutateAsync(true));
    } catch {
      /* shown inline */
    }
  };
  const doRun = async () => {
    try {
      const stats = await run.mutateAsync(false);
      setConfirm(false);
      setPreview(null);
      toast({ tone: "success", title: "Retention applied", body: `${stats.runs} runs, ${stats.run_events} events, ${stats.audit_events} audit rows removed` });
    } catch {
      setConfirm(false);
    }
  };

  if (q.isPending) return <Skeleton className="h-48" />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const { policy, last_run: last, enabled } = q.data;
  const rows: [string, string][] = [
    ["Finished runs", describeDays(policy.run_days, "Deleted with their artifacts")],
    ["Run event logs", describeDays(policy.event_days, "Dropped (results and artifacts kept)")],
    ["Audit trail", describeDays(policy.audit_days, "Deleted")],
    ["Protocol ledger", describeDays(policy.ledger_days, "Deleted")],
  ];

  return (
    <SettingsSection id="retention" title="Data retention"
      badge={<Badge tone={enabled ? "success" : "neutral"}>{enabled ? `Runs every ${Math.round(policy.interval_s / 60)} min` : "Scheduled runs off"}</Badge>}
      description="Set with ARGUS_RETENTION_* environment variables. Old data is removed in small batches."
      footer={
        <>
          <Button icon={Eye} loading={run.isPending && run.variables === true} disabled={run.isPending} onClick={doPreview}>Preview</Button>
          <Button variant="danger" icon={Play} disabled={run.isPending} onClick={() => setConfirm(true)}>Run now</Button>
        </>
      }>
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-[max-content_1fr] text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-fg-muted">{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {policy.notes.map((n) => <p key={n} className="text-xs text-fg-muted">{n}</p>)}
      {run.error && <Callout tone="danger" title="Retention failed">{errorMessage(run.error)}</Callout>}
      {preview && (
        <div aria-live="polite">
          <StatsTable stats={preview} caption="Would be removed now (preview)" />
        </div>
      )}
      {last ? (
        <StatsTable stats={last} caption={`Last run ${new Date(last.started_at).toLocaleString()}${last.error ? ` (failed: ${last.error})` : ""}`} />
      ) : (
        <p className="text-sm text-fg-muted">Retention has not run since the server started.</p>
      )}
      <Dialog open={confirm} onOpenChange={setConfirm} title="Apply retention now?"
        description="Everything matching the policy is permanently deleted. Use Preview first to see the counts."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button variant="danger" loading={run.isPending} onClick={doRun}>Delete old data</Button>
          </>
        }>
        <p className="text-sm text-fg-muted">This cannot be undone.</p>
      </Dialog>
    </SettingsSection>
  );
}

// ------------------------------------------------------------------ Delete account
function DeleteAccountSection() {
  const del = useDeleteAccount();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [password, setPassword] = useState("");
  const ready = phrase.trim().toLowerCase() === CONFIRM_PHRASE && password.length > 0;

  const close = (o: boolean) => {
    setOpen(o);
    if (!o) {
      setPhrase("");
      setPassword("");
      del.reset();
    }
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    try {
      await del.mutateAsync(password);
      useSession.getState().logout();
      toast({ tone: "success", title: "Your account has been deleted" });
      void navigate("/login", { replace: true });
    } catch {
      /* shown inline */
    }
  };
  const err = del.error;
  const pwError = err instanceof ApiError && err.code === "invalid_password" ? err.message : undefined;

  return (
    <SettingsSection id="delete" title="Delete account"
      description="Permanently delete your account and everything it owns: councils, skills, connections, runs, artifacts and stored files."
      footer={<Button variant="danger" icon={Trash2} onClick={() => setOpen(true)}>Delete account…</Button>}>
      <p className="text-sm text-fg-muted">Download an export first if you want to keep a copy. Audit entries are kept without your name.</p>
      <Dialog open={open} onOpenChange={close} title="Delete your account?"
        description="This permanently deletes your account and all of its data. It cannot be undone.">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <TextField label={`Type "${CONFIRM_PHRASE}" to confirm`} value={phrase} autoComplete="off"
            onChange={(e) => setPhrase(e.target.value)} />
          <TextField label="Current password" type="password" value={password} autoComplete="current-password"
            error={pwError} onChange={(e) => setPassword(e.target.value)} />
          {err && !pwError && <Callout tone="danger" title="Could not delete the account">{errorMessage(err)}</Callout>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => close(false)}>Cancel</Button>
            <Button type="submit" variant="danger" loading={del.isPending} disabled={!ready}>Permanently delete account</Button>
          </div>
        </form>
      </Dialog>
    </SettingsSection>
  );
}

export default function DataSettings() {
  const admin = useIsAdmin();
  return (
    <SettingsLayout title="Data & privacy" description="Export or delete your data, and manage how long the server keeps it.">
      <ExportSection />
      {admin && <RetentionSection />}
      <DeleteAccountSection />
    </SettingsLayout>
  );
}
