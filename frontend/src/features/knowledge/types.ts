export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
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
  created_at: string;
}

export interface SearchHit {
  score: number;
  kb_id: string;
  kb_name: string;
  document_id: string;
  filename: string;
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

/** Accepted upload types (PDF is refused by the server with 415). */
export const ACCEPT = ".md,.markdown,.txt,.text,.html,.htm,text/markdown,text/plain,text/html";
export const MAX_FILE_BYTES = 5 * 1024 * 1024;
const OK_EXT = /\.(md|markdown|txt|text|html?)$/i;

export function fileProblem(file: File): string | null {
  if (/\.pdf$/i.test(file.name)) return "PDF is not supported yet — upload Markdown, plain text or HTML.";
  if (!OK_EXT.test(file.name)) return "Unsupported file type — use .md, .txt or .html.";
  if (file.size > MAX_FILE_BYTES) return "File is larger than the 5 MB limit.";
  if (file.size === 0) return "File is empty.";
  return null;
}
