// Knowledge bases, documents, search and memory (backend: /api/v1/knowledge-bases, /api/v1/memory).

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, auth, refreshOnce, V1 } from "@/lib/api";

import type { KnowledgeBase, KnowledgeDocument, LinkedKnowledge, MemoryItem, SearchResponse } from "./types";

export const kKeys = {
  bases: ["knowledge-bases"] as const,
  docs: (kb: string) => ["knowledge-docs", kb] as const,
  search: (kb: string, q: string) => ["knowledge-search", kb, q] as const,
  memory: (council: string) => ["memory", council] as const,
  runLinks: (runId: string) => ["run-knowledge", runId] as const,
};

export const useKnowledgeBases = () =>
  useQuery({
    queryKey: kKeys.bases,
    queryFn: async () => (await api.get<{ knowledge_bases: KnowledgeBase[] }>(`${V1}/knowledge-bases`)).knowledge_bases,
  });

export const useDocuments = (kbId: string | null) =>
  useQuery({
    queryKey: kKeys.docs(kbId ?? ""),
    queryFn: async () => (await api.get<{ documents: KnowledgeDocument[] }>(`${V1}/knowledge-bases/${kbId}/documents`)).documents,
    enabled: Boolean(kbId),
  });

export const useKnowledgeSearch = (kbId: string | null, q: string, k = 10) =>
  useQuery({
    queryKey: kKeys.search(kbId ?? "", q),
    queryFn: () =>
      api.get<SearchResponse>(`${V1}/knowledge-bases/${kbId}/search?${new URLSearchParams({ q, k: String(k) })}`),
    enabled: Boolean(kbId) && q.trim().length > 0,
    placeholderData: keepPreviousData,
  });

export function useCreateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description: string }) => api.post<KnowledgeBase>(`${V1}/knowledge-bases`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: kKeys.bases }),
  });
}

export function useDeleteKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`${V1}/knowledge-bases/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: kKeys.bases }),
  });
}

export function useDeleteDocument(kbId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (docId: string) => api.del(`${V1}/knowledge-bases/${kbId}/documents/${docId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: kKeys.docs(kbId) });
      void qc.invalidateQueries({ queryKey: kKeys.bases });
      void qc.invalidateQueries({ queryKey: ["knowledge-search", kbId] });
    },
  });
}

/** Multipart upload with progress (fetch cannot report upload progress, so this uses XHR). */
export function uploadDocument(kbId: string, file: File, onProgress?: (pct: number) => void): Promise<KnowledgeDocument> {
  const send = (retried: boolean): Promise<KnowledgeDocument> =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${V1}/knowledge-bases/${kbId}/documents`);
      if (auth.token) xhr.setRequestHeader("Authorization", `Bearer ${auth.token}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onerror = () => reject(new ApiError(0, "network_error", "Network error during upload"));
      xhr.onload = () => {
        let body: unknown = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON */
        }
        if (xhr.status === 401 && !retried) {
          void refreshOnce().then((ok) => (ok ? send(true).then(resolve, reject) : reject(new ApiError(401, "unauthorized", "Session expired"))), reject);
          return;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          onProgress?.(100);
          resolve(body as KnowledgeDocument);
          return;
        }
        const env = (body as { error?: { code?: string; message?: string } } | null)?.error;
        const fallback = xhr.status === 413 ? "File is too large" : xhr.status === 415 ? "Unsupported file type" : "Upload failed";
        reject(new ApiError(xhr.status, env?.code ?? "http_error", env?.message ?? fallback));
      };
      const form = new FormData();
      form.append("file", file, file.name);
      xhr.send(form);
    });
  return send(false);
}

export type NewKnowledge =
  | { kind: "text"; title: string; text: string; source_url?: string | null }
  | { kind: "url"; url: string; title?: string | null };

/** Add pasted text, or a page fetched from a URL, as a document. */
export function useAddKnowledge() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ kbId, item }: { kbId: string; item: NewKnowledge }) => {
      const { kind, ...body } = item;
      return api.post<KnowledgeDocument>(`${V1}/knowledge-bases/${kbId}/documents/${kind}`, body);
    },
    onSuccess: (_doc, { kbId }) => {
      void qc.invalidateQueries({ queryKey: kKeys.docs(kbId) });
      void qc.invalidateQueries({ queryKey: kKeys.bases });
      void qc.invalidateQueries({ queryKey: ["knowledge-search", kbId] });
    },
  });
}

export const useRunKnowledge = (runId: string) =>
  useQuery({
    queryKey: kKeys.runLinks(runId),
    queryFn: async () => (await api.get<{ linked: LinkedKnowledge[] }>(`${V1}/runs/${runId}/knowledge`)).linked,
  });

/** Link knowledge bases to a run (live agents pick them up on their next turn), or unlink one. */
export function useLinkKnowledge(runId: string) {
  const qc = useQueryClient();
  const done = () => void qc.invalidateQueries({ queryKey: kKeys.runLinks(runId) });
  const link = useMutation({
    mutationFn: (ids: string[]) =>
      api.post<{ linked: LinkedKnowledge[]; added: string[]; live: boolean }>(`${V1}/runs/${runId}/knowledge`, { knowledge_base_ids: ids }),
    onSuccess: done,
  });
  const unlink = useMutation({
    mutationFn: (kbId: string) => api.del<{ linked: LinkedKnowledge[] }>(`${V1}/runs/${runId}/knowledge/${kbId}`),
    onSuccess: done,
  });
  return { link, unlink };
}

export function useInvalidateKnowledge() {
  const qc = useQueryClient();
  return (kbId: string) => {
    void qc.invalidateQueries({ queryKey: kKeys.docs(kbId) });
    void qc.invalidateQueries({ queryKey: kKeys.bases });
    void qc.invalidateQueries({ queryKey: ["knowledge-search", kbId] });
  };
}

export const useMemory = (councilId: string) =>
  useQuery({
    queryKey: kKeys.memory(councilId),
    queryFn: async () => {
      const qs = councilId === "all" ? "" : councilId === "global" ? "?scope=global" : `?council_id=${councilId}`;
      return (await api.get<{ items: MemoryItem[] }>(`${V1}/memory${qs}`)).items;
    },
  });

export function useDeleteMemory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`${V1}/memory/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["memory"] }),
  });
}
