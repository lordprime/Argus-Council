import { ArrowLeft, Copy, Download, FileText, Pencil, SearchX, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { Chips, Meta, Switch } from "@/components/connections/parts";
import { SkillBadges } from "@/components/skills/SkillBadges";
import { FileViewer } from "@/components/skills/FileViewer";
import { VersionHistory } from "@/components/skills/VersionHistory";
import { Button, Callout, Card, EmptyState, ErrorState, PageHeader, Skeleton } from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { exportSkill, useDeleteSkill, useToggleSkill } from "@/features/skills/queries";
import { demoteHeadings, SKILL_PROSE, stripFrontmatter } from "@/features/skills/skillmd";
import { ApiError, errorMessage } from "@/lib/api";
import { useSkill } from "@/lib/queries";
import { toast } from "@/stores/toast";

function DetailSkeleton() {
  return (
    <div role="status" aria-busy="true">
      <span className="sr-only">Loading skill…</span>
      <div aria-hidden className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-full max-w-xl" />
        <div className="grid gap-6 lg:grid-cols-[1fr_18rem]">
          <Skeleton className="h-96" />
          <Skeleton className="h-64" />
        </div>
      </div>
    </div>
  );
}

export default function SkillDetail() {
  const { name = "" } = useParams();
  const q = useSkill(name);
  const navigate = useNavigate();
  const del = useDeleteSkill();
  const toggle = useToggleSkill();
  const [confirm, setConfirm] = useState(false);
  const [exporting, setExporting] = useState(false);

  const back = (
    <Link to="/skills" className="inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg mb-3 min-h-6">
      <ArrowLeft className="size-4" aria-hidden />All skills
    </Link>
  );

  if (q.isLoading) return <div className="p-4 md:p-6 max-w-6xl mx-auto">{back}<DetailSkeleton /></div>;
  if (q.error || !q.data) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <div className="p-4 md:p-6 max-w-6xl mx-auto">
        {back}
        <h1 className="text-xl mb-4">{notFound ? "Skill not found" : "Could not load skill"}</h1>
        <Card as="div">
          {notFound
            ? <EmptyState icon={SearchX} title={`There is no skill named “${name}”`} body="It may have been deleted or renamed." action={<Link to="/skills" className="text-accent underline">Back to skills</Link>} />
            : <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
        </Card>
      </div>
    );
  }

  const s = q.data;
  const body = stripFrontmatter(s.skill_md);
  const doExport = async () => {
    setExporting(true);
    try {
      await exportSkill(s.name);
    } catch (e) {
      toast({ tone: "danger", title: `Could not export ${s.name}`, body: errorMessage(e) });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      {back}
      <PageHeader title={s.name} description={s.description}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button icon={Download} loading={exporting} onClick={() => void doExport()}>Export .zip</Button>
            {s.builtin ? (
              <Button variant="primary" icon={Copy} onClick={() => navigate(`/skills/new?from=${encodeURIComponent(s.name)}`)}>Customize</Button>
            ) : (
              <>
                <Button icon={Pencil} onClick={() => navigate(`/skills/new?edit=${encodeURIComponent(s.name)}`)}>Edit</Button>
                <Button variant="danger-ghost" icon={Trash2} onClick={() => { del.reset(); setConfirm(true); }}>Delete</Button>
              </>
            )}
          </div>
        } />

      {s.builtin && (
        <div className="mb-6">
          <Callout tone="info" title="Built-in skills are read-only">
            Customize creates your own copy with the same name. Your copy overrides the built-in until you delete it.
          </Callout>
        </div>
      )}
      {s.shadows_builtin && (
        <div className="mb-6">
          <Callout tone="warning" title={`This overrides the built-in ${s.name}`}>
            Agents use your version. Delete it to go back to the built-in skill.
          </Callout>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem] items-start">
        <div className="flex flex-col gap-6 min-w-0">
          <Card className="p-5" aria-labelledby="instructions-h">
            <h2 id="instructions-h" className="text-lg mb-1">Instructions</h2>
            <p className="text-xs text-fg-subtle mb-3">Loaded when an agent calls use_skill.</p>
            <Markdown className={SKILL_PROSE}>{demoteHeadings(body)}</Markdown>
          </Card>

          <Card className="p-5" aria-labelledby="files-h">
            <h2 id="files-h" className="text-lg mb-3">Bundled files <span className="text-fg-subtle text-sm font-normal">({s.files.length})</span></h2>
            {s.files.length ? (
              <FileViewer files={s.file_contents} />
            ) : (
              <p className="text-sm text-fg-subtle flex items-center gap-2"><FileText className="size-4" aria-hidden />This skill has no extra files.</p>
            )}
          </Card>

          {!s.builtin && <VersionHistory name={s.name} current={{ skill_md: s.skill_md, files: s.file_contents }} />}

          <details className="rounded-lg border border-border bg-surface">
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium">SKILL.md source</summary>
            <pre tabIndex={0} role="region" aria-label="SKILL.md source"
              className="px-4 pb-4 overflow-auto max-h-[32rem] font-mono text-xs leading-5 whitespace-pre-wrap break-words">{s.skill_md}</pre>
          </details>
        </div>

        <aside aria-labelledby="about-h" className="lg:sticky lg:top-4">
        <Card as="div" className="p-4 flex flex-col gap-4">
          <h2 id="about-h" className="text-base font-semibold">About</h2>
          <SkillBadges skill={s} align="start" />
          <dl className="grid grid-cols-2 gap-3">
            <Meta label="Version" mono>{s.version}</Meta>
            <Meta label="Source">{s.source === "builtin" ? "Ships with Argus" : s.source === "upload" ? "Uploaded .zip" : s.source === "marketplace" ? "Marketplace" : "Written here"}</Meta>
          </dl>
          <div>
            <p className="text-xs text-fg-subtle mb-1">Allowed tools</p>
            <Chips items={s.allowed_tools} label={`Tools ${s.name} may use`} empty="No tool restrictions declared" />
          </div>
          {!s.builtin && (
            <div className="pt-3 border-t border-border">
              <Switch object={s.name} checked={s.enabled} disabled={toggle.isPending}
                onChange={(enabled) => toggle.mutate({ name: s.name, enabled }, {
                  onError: (e) => toast({ tone: "danger", title: `Could not update ${s.name}`, body: errorMessage(e) }),
                })} />
              <p className="text-xs text-fg-subtle mt-1">Disabled skills are hidden from every council.</p>
            </div>
          )}
        </Card>
        </aside>
      </div>

      <ConfirmDelete open={confirm} onOpenChange={setConfirm} kind="skill" name={s.name}
        consequence={s.shadows_builtin
          ? <>Agents go back to the built-in <strong>{s.name}</strong>. Your instructions and {s.files.length} bundled files are removed.</>
          : <>Agents stop seeing <strong>{s.name}</strong>. Its instructions and {s.files.length} bundled files are removed. Export it first if you may need it again.</>}
        pending={del.isPending} error={del.error}
        onConfirm={() => del.mutate(s.name, {
          onSuccess: () => { toast({ tone: "success", title: `Deleted ${s.name}` }); setConfirm(false); navigate("/skills"); },
        })} />
    </div>
  );
}
