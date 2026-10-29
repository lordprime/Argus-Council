export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  /** "library": the research library, filled automatically with every source agents read. */
  kind?: "user" | "library";
  document_count: number;
  chunk_count: number;
  max_chunks: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocument {
  id: string;
  kb_id: string;
  filename: string;
  content_type: string;
  size: number;
  sha256: string;
  chunk_count: number;
  title?: string;
  /** upload: a file · text: pasted · url: fetched from a link · agent: captured from a run */
  origin?: "upload" | "text" | "url" | "agent";
  source_url?: string | null;
  run_id?: string | null;
  created_at: string;
  updated_at?: string;
}

/** Knowledge base linked to a run (GET /runs/{id}/knowledge). */
export interface LinkedKnowledge {
  id: string;
  name: string;
  description: string;
  document_count: number;
}

export interface SearchHit {
  score: number;
  kb_id: string;
  kb_name: string;
  document_id: string;
  filename: string;
  title?: string;
  source_url?: string | null;
  chunk_index: number;
  text: string;
  snippet: string;
  citation: string;
}

export interface SearchResponse {
  query: string;
  terms: string[];
  results: SearchHit[];
}

export interface MemoryItem {
  id: string;
  council_id: string | null;
  key: string;
  text: string;
  tags: string[];
  run_id: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

/** Accepted upload types. PDFs must have a text layer (scans without OCR have no text to index). */
export const ACCEPT = ".md,.markdown,.txt,.text,.html,.htm,.pdf,text/markdown,text/plain,text/html,application/pdf";
export const MAX_FILE_BYTES = 5 * 1024 * 1024;
const OK_EXT = /\.(md|markdown|txt|text|html?|pdf)$/i;

export function fileProblem(file: File): string | null {
  if (!OK_EXT.test(file.name)) return "Unsupported file type — use .md, .txt, .html or .pdf.";
  if (file.size > MAX_FILE_BYTES) return "File is larger than the 5 MB limit.";
  if (file.size === 0) return "File is empty.";
  return null;
}
