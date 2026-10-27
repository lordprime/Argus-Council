import { History, Plus, Users } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { CouncilHeader, StartRunLink } from "@/components/councils/CouncilHeader";
import { GuardrailsEditor } from "@/components/councils/GuardrailsEditor";
import { ConfirmDeleteDialog } from "@/components/councils/parts";
import { ResizePanel, SeatCard, SeatEditDialog } from "@/components/councils/seats";
import { RunRowList } from "@/components/run/RunRow";
import { Button, ButtonLink, Card, EmptyState, ErrorState, FormActions, Page, PageHeader, SectionTitle, Skeleton, Tabs } from "@/components/ui";
import { deepEqual, fieldErrorsFrom, nextSeatName, previewResize, type Dict } from "@/features/councils/logic";
import {
  useAddMember, useDeleteCouncil, useRemoveMember, useResizeCouncil, useUpdateCouncil, useUpdateMember,
  type MemberInput, type MemberPatch,
} from "@/features/councils/queries";
import { ApiError, errorMessage } from "@/lib/api";
import { useCouncil, useRoles, useRunsList } from "@/lib/queries";
import type { Council, Member } from "@/lib/types";
import { toast } from "@/stores/toast";

// ------------------------------------------------------------------ Seats tab
function SeatsTab({ council }: { council: Council }) {
  const roles = useRoles();
  const updateMember = useUpdateMember(council.id);
  const addMember = useAddMember(council.id);
  const removeMember = useRemoveMember(council.id);
  const resize = useResizeCouncil(council.id);

  const [target, setTarget] = useState<number | null>(null);
  const [resizeError, setResizeError] = useState<string>();
  const [editing, setEditing] = useState<Member | "new" | null>(null);
  const [editError, setEditError] = useState<string>();
  const [removing, setRemoving] = useState<Member | null>(null);
  const focusAfter = useRef<{ id: string; dir: "up" | "down" } | null>(null);
  const listRef = useRef<HTMLOListElement>(null);

  const members = useMemo(() => [...council.members].sort((a, b) => a.position - b.position), [council.members]);
  const roleList = roles.data?.roles ?? [];
  const overseers = useMemo(() => new Set((roles.data?.roles ?? []).filter((r) => r.overseer).map((r) => r.key)), [roles.data]);
  const current = members.length;
  const tgt = target ?? current;
  const preview = previewResize(members, council.mode, tgt, roles.data?.rosters, overseers, roleList);
  const removedIds = new Set(preview.removed.map((m) => m.id));
  const ref = members[0];
  const atMin = current <= council.min_size;
  const atMax = current >= council.max_size;

  // Keep keyboard focus on the moved seat's button after the list re-orders (2.5.7 alternative to drag).
  useEffect(() => {
    const f = focusAfter.current;
    if (!f) return;
    focusAfter.current = null;
    const btn = listRef.current?.querySelector<HTMLButtonElement>(`[data-move="${f.id}-${f.dir}"]`);
    const other = listRef.current?.querySelector<HTMLButtonElement>(`[data-move="${f.id}-${f.dir === "up" ? "down" : "up"}"]`);
    (btn && !btn.disabled ? btn : other)?.focus();
  }, [members]);

  const fail = (title: string, e: unknown, retry?: () => void) =>
    toast({ tone: "danger", title, body: errorMessage(e), action: retry ? { label: "Retry", run: retry } : undefined });

  const toggle = (m: Member, enabled: boolean) =>
    updateMember.mutate({ memberId: m.id, patch: { enabled } }, { onError: (e) => fail(`Could not update ${m.name}`, e, () => toggle(m, enabled)) });

  const move = (m: Member, index: number, dir: "up" | "down") => {
    focusAfter.current = { id: m.id, dir };
    updateMember.mutate({ memberId: m.id, patch: { position: dir === "up" ? index - 1 : index + 1 } },
      { onError: (e) => fail(`Could not move ${m.name}`, e) });
  };

  const applyResize = async () => {
    setResizeError(undefined);
    try {
      await resize.mutateAsync({ size: tgt });
      setTarget(null);
      toast({ tone: "success", title: `Council resized to ${tgt} seats` });
    } catch (e) {
      const fe = fieldErrorsFrom(e);
      setResizeError(fe.size ?? Object.values(fe)[0]);
    }
  };

  const saveSeat = async (body: MemberPatch | MemberInput) => {
    setEditError(undefined);
    try {
      if (editing === "new") await addMember.mutateAsync(body as MemberInput);
      else if (editing && Object.keys(body).length) await updateMember.mutateAsync({ memberId: editing.id, patch: body });
      setEditing(null);
    } catch (e) {
      setEditError(errorMessage(e));
    }
  };

  const confirmRemove = async () => {
    if (!removing) return;
    const m = removing;
    try {
      await removeMember.mutateAsync(m.id);
      setRemoving(null);
      toast({ tone: "success", title: `Removed ${m.name}` });
    } catch (e) {
      setRemoving(null);
      fail(`Could not remove ${m.name}`, e);
    }
  };

  return (
    <div className="flex flex-col gap-4 py-4">
      <ResizePanel current={current} target={tgt} min={council.min_size} max={council.max_size} preview={preview}
        roles={roleList} overseers={overseers} pending={resize.isPending} error={resizeError}
        newSeatRoute={ref ? `${ref.provider}/${ref.model}` : "mock/mock-1"}
        onTarget={(n) => { setTarget(n === current ? null : n); setResizeError(undefined); }} onApply={() => void applyResize()} />

      <SectionTitle count={current} flush
        actions={
          <div className="flex items-center gap-3">
            {(atMin || atMax) && (
              <span className="text-xs text-fg-subtle">
                {atMax ? `At the maximum of ${council.max_size}; raise it in Edit details to add seats.` : `At the minimum of ${council.min_size}; seats can't be removed.`}
              </span>
            )}
            <Button size="sm" icon={Plus} disabled={atMax || !roles.data} onClick={() => { setEditError(undefined); setEditing("new"); }}>
              Add seat
            </Button>
          </div>
        }>
        Seats
      </SectionTitle>

      {members.length === 0 ? (
        <Card as="div">
          <EmptyState titleAs="h3" icon={Users} title="No seats" body="Use the size stepper above to seat the default roster for this mode." />
        </Card>
      ) : (
        <ol ref={listRef} className="flex flex-col gap-2" aria-label="Seats in speaking order">
          {members.map((m, i) => (
            <SeatCard key={m.id} member={m} index={i} count={members.length} roles={roleList}
              canRemove={!atMin} removeReason={`council is at its minimum of ${council.min_size}`}
              pendingRemoval={removedIds.has(m.id)}
              onToggle={(on) => toggle(m, on)} onMove={(d) => move(m, i, d)}
              onEdit={() => { setEditError(undefined); setEditing(m); }} onRemove={() => setRemoving(m)} />
          ))}
        </ol>
      )}

      <SeatEditDialog open={editing != null} onOpenChange={(o) => !o && setEditing(null)}
        member={editing === "new" ? undefined : editing ?? undefined} roles={roleList}
        defaults={{ provider: ref?.provider ?? "mock", model: ref?.model ?? "mock-1", name: (role) => nextSeatName(members, role, roleList) }}
        onSave={(b) => void saveSeat(b)} pending={addMember.isPending || updateMember.isPending} error={editError} />

      <ConfirmDeleteDialog open={removing != null} onOpenChange={(o) => !o && setRemoving(null)} requireTyping={false}
        verb="Remove" kind="seat" name={removing?.name ?? ""} pending={removeMember.isPending} onConfirm={() => void confirmRemove()}
        body={<>The council keeps {current - 1} seat{current - 1 === 1 ? "" : "s"}. Past runs are not affected.</>} />
    </div>
  );
}

// ------------------------------------------------------------------ Guardrails tab
function GuardrailsTab({ council, draft, setDraft }: { council: Council; draft: Dict; setDraft: (d: Dict | null) => void }) {
  const update = useUpdateCouncil(council.id);
  const [valid, setValid] = useState(true);
  const [error, setError] = useState<string>();
  const dirty = !deepEqual(draft, council.guardrails ?? {});

  const save = async () => {
    setError(undefined);
    try {
      await update.mutateAsync({ guardrails: draft });
      setDraft(null);
      toast({ tone: "success", title: "Guardrails saved" });
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <div className="flex flex-col gap-4 py-4">
      <h2 className="sr-only">Guardrails</h2>
      <GuardrailsEditor value={draft} onChange={setDraft} onValidChange={setValid} />
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <FormActions sticky meta={<span aria-live="polite">{dirty ? "Unsaved changes" : "All changes saved"}</span>}>
        <Button variant="primary" disabled={!dirty || !valid} loading={update.isPending} onClick={() => void save()}>Save guardrails</Button>
        <Button disabled={!dirty} onClick={() => setDraft(null)}>Discard changes</Button>
      </FormActions>
    </div>
  );
}

// ------------------------------------------------------------------ Runs tab
function RunsTab({ council }: { council: Council }) {
  const runs = useRunsList({ limit: 200 });
  const mine = useMemo(() => (runs.data ?? []).filter((r) => r.council_id === council.id), [runs.data, council.id]);
  return (
    <div className="flex flex-col gap-3 py-4">
      <SectionTitle flush actions={<StartRunLink variant="secondary" />}>Runs</SectionTitle>
      {runs.isLoading ? (
        <div className="space-y-2" aria-hidden><Skeleton className="h-14" /><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
      ) : runs.error ? (
        <Card as="div"><ErrorState error={runs.error} onRetry={() => void runs.refetch()} /></Card>
      ) : mine.length ? (
        <Card as="div"><RunRowList runs={mine} label={`Runs of ${council.name}`} /></Card>
      ) : (
        <Card as="div">
          <EmptyState titleAs="h3" icon={History} title="No runs yet"
            body={<>Start a run from Home and pick <span className="font-medium text-fg">{council.name}</span> as the council.</>} />
        </Card>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ Page
const TABS = ["seats", "guardrails", "runs"] as const;

export default function CouncilDetail() {
  const { councilId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const council = useCouncil(councilId);
  const del = useDeleteCouncil();
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Lifted so unsaved guardrail edits survive switching tabs (inactive tab panels unmount).
  const [guardrailsDraft, setGuardrailsDraft] = useState<Dict | null>(null);
  const tab = TABS.includes(params.get("tab") as (typeof TABS)[number]) ? params.get("tab")! : "seats";

  if (council.isLoading) {
    return (
      <Page aria-busy="true">
        <span role="status" className="sr-only">Loading council…</span>
        <div aria-hidden>
          <Skeleton className="h-7 w-64" />
          <Skeleton className="h-4 w-96 max-w-full mt-3" />
          <Skeleton className="h-9 w-72 mt-8" />
          <div className="mt-4 space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-16" />)}</div>
        </div>
      </Page>
    );
  }
  if (council.error || !council.data) {
    const notFound = council.error instanceof ApiError && council.error.status === 404;
    return (
      <Page>
        {notFound ? (
          <Card as="div">
            <EmptyState titleAs="h1" icon={Users} title="Council not found" body="It may have been deleted."
              action={<ButtonLink to="/councils">All councils</ButtonLink>} />
          </Card>
        ) : (
          <>
            <PageHeader title="Could not load this council" />
            <Card as="div"><ErrorState error={council.error} onRetry={() => void council.refetch()} /></Card>
          </>
        )}
      </Page>
    );
  }

  const c = council.data;
  const draft = guardrailsDraft ?? c.guardrails ?? {};
  const guardrailsDirty = guardrailsDraft != null && !deepEqual(guardrailsDraft, c.guardrails ?? {});
  const doDelete = async () => {
    try {
      await del.mutateAsync(c.id);
      toast({ tone: "success", title: `Deleted “${c.name}”` });
      navigate("/councils");
    } catch (e) {
      toast({ tone: "danger", title: `Could not delete “${c.name}”`, body: errorMessage(e) });
    }
  };

  return (
    <Page>
      <CouncilHeader council={c} onDelete={() => setConfirmDelete(true)} />
      <Tabs value={tab} className="[&>div[role=tablist]]:px-0"
        onValueChange={(v) => setParams(v === "seats" ? {} : { tab: v }, { replace: true })}
        tabs={[
          { value: "seats", label: `Seats (${c.members.length})`, content: <SeatsTab council={c} /> },
          {
            value: "guardrails",
            label: guardrailsDirty ? <>Guardrails <span className="text-warning" aria-label="unsaved changes">•</span></> : "Guardrails",
            content: <GuardrailsTab council={c} draft={draft} setDraft={setGuardrailsDraft} />,
          },
          { value: "runs", label: "Runs", content: <RunsTab council={c} /> },
        ]} />
      <ConfirmDeleteDialog open={confirmDelete} onOpenChange={setConfirmDelete} kind="council" name={c.name}
        pending={del.isPending} onConfirm={() => void doDelete()}
        body={<>Its {c.members.length} seats and guardrails are removed. Past runs keep their own snapshot.</>} />
    </Page>
  );
}
