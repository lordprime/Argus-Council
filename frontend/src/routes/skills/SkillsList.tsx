import { FileText, Plus, Search, Sparkles, Upload } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { CardSkeletons } from "@/components/connections/parts";
import { Marketplace } from "@/components/skills/Marketplace";
import { SkillCard } from "@/components/skills/SkillCard";
import { Button, Callout, Card, EmptyState, ErrorState, PageHeader, Segmented, Tabs } from "@/components/ui";
import { useImportSkill } from "@/features/skills/queries";
import { errorMessage } from "@/lib/api";
import { useSkills } from "@/lib/queries";
import { toast } from "@/stores/toast";

type Filter = "all" | "builtin" | "mine";

export default function SkillsList() {
  const skills = useSkills();
  const importer = useImportSkill();
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "marketplace" ? "marketplace" : "library";
  const setTab = (t: string) => setParams(t === "marketplace" ? { tab: t } : {}, { replace: true });

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (skills.data ?? []).filter((s) =>
      (filter === "all" || (filter === "builtin" ? s.builtin : !s.builtin)) &&
      (!q || s.name.includes(q) || s.description.toLowerCase().includes(q) || s.allowed_tools.some((t) => t.includes(q))));
  }, [skills.data, query, filter]);

  const counts = useMemo(() => {
    const all = skills.data ?? [];
    return { all: all.length, builtin: all.filter((s) => s.builtin).length, mine: all.filter((s) => !s.builtin).length };
  }, [skills.data]);

  const onFile = (file: File | undefined) => {
    if (!file) return;
    importer.mutate(file, {
      onSuccess: (s) => {
        toast({ tone: "success", title: `Imported ${s.name}`, body: s.shadows_builtin ? "It overrides the built-in skill of the same name." : undefined });
        navigate(`/skills/${encodeURIComponent(s.name)}`);
      },
    });
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      <PageHeader title="Skills"
        description="Repeatable procedures for agents. Agents see every skill's name and description, and load the instructions when needed."
        actions={
          <>
            <input ref={fileRef} type="file" accept=".zip,application/zip" className="sr-only" tabIndex={-1} aria-hidden
              onChange={(e) => onFile(e.target.files?.[0])} />
            <Button icon={Upload} loading={importer.isPending} onClick={() => fileRef.current?.click()}>Import .zip</Button>
            <Button variant="primary" icon={Plus} onClick={() => navigate("/skills/new")}>New skill</Button>
          </>
        } />

      {importer.error && (
        <div className="mb-4">
          <Callout tone="danger" title="Import failed"
            action={<Button size="sm" variant="ghost" onClick={() => importer.reset()}>Dismiss</Button>}>
            {errorMessage(importer.error)} The archive needs a SKILL.md at its root (or in one top-level folder) and text files only.
          </Callout>
        </div>
      )}

      <Tabs value={tab} onValueChange={setTab} tabs={[
        { value: "library", label: `Your library (${counts.all})`, content: <div className="pt-4">{library()}</div> },
        { value: "marketplace", label: "Marketplace", content: <Marketplace /> },
      ]} />
    </div>
  );

  function library() {
    return (
      <>
      <div className="flex flex-wrap items-end gap-3 mb-4">
        <div className="flex flex-col gap-1 flex-1 min-w-56 max-w-md">
          <label htmlFor="skill-search" className="text-sm font-medium">Search skills</label>
          <div className="relative">
            <Search className="size-4 text-fg-subtle absolute left-3 top-1/2 -translate-y-1/2" aria-hidden />
            <input id="skill-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Name, description or tool"
              className="w-full h-9 rounded-md border border-border-strong bg-surface pl-9 pr-3 text-base placeholder:text-fg-subtle" />
          </div>
        </div>
        <Segmented label="Show" value={filter} onChange={setFilter} options={[
          { value: "all", label: `All (${counts.all})` },
          { value: "builtin", label: `Built-in (${counts.builtin})` },
          { value: "mine", label: `Yours (${counts.mine})` },
        ]} />
      </div>

      <p className="sr-only" role="status" aria-live="polite">
        {skills.data ? `${visible.length} of ${counts.all} skills shown` : ""}
      </p>

      {skills.isLoading ? (
        <CardSkeletons count={6} className="lg:grid-cols-3" />
      ) : skills.error ? (
        <Card><ErrorState error={skills.error} onRetry={() => void skills.refetch()} /></Card>
      ) : !counts.all ? (
        <Card as="div">
          <EmptyState icon={Sparkles} title="No skills yet"
            body="Skills teach agents repeatable procedures. Write a SKILL.md or upload a .zip package."
            action={<div className="flex gap-2 justify-center"><Button icon={Upload} onClick={() => fileRef.current?.click()}>Import .zip</Button><Button variant="primary" icon={Plus} onClick={() => navigate("/skills/new")}>New skill</Button></div>} />
        </Card>
      ) : !visible.length ? (
        <Card as="div">
          <EmptyState icon={FileText} title="No skills match"
            body={query ? <>Nothing matches “{query}”{filter !== "all" ? " in this view" : ""}.</> : "No skills in this view yet."}
            action={filter === "mine" && !query
              ? <Link to="/skills/new" className="text-accent underline">Write your first skill</Link>
              : <Button size="sm" onClick={() => { setQuery(""); setFilter("all"); }}>Clear filters</Button>} />
        </Card>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {visible.map((s) => <li key={s.name} className="flex"><SkillCard skill={s} /></li>)}
        </ul>
      )}
      </>
    );
  }
}
