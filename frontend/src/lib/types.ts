// Mirrors backend/argus/api/schemas.py and the run event envelope (docs/design/ui-spec.md §6).

export type Mode = "general" | "research" | "coding" | "chat";
export type OutputFormat = "txt" | "md" | "latex" | "pdf" | "html" | "docx" | "json";
export type RunStatus = "queued" | "running" | "paused" | "completed" | "failed" | "cancelled" | "halted";

export interface User {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  created_at: string;
  /** Two-factor authentication (TOTP) is on for this account. */
  mfa_enabled?: boolean;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

/** POST /auth/login answer when the account has two-factor authentication on. */
export interface MfaChallenge {
  mfa_required: true;
  mfa_token: string;
  expires_in: number;
}

export interface Member {
  id: string;
  name: string;
  role: string;
  provider: string;
  model: string;
  system_prompt: string;
  temperature: number | null;
  position: number;
  enabled: boolean;
  config: { tools?: string[]; overseer?: boolean; [k: string]: unknown };
}

export interface Council {
  id: string;
  name: string;
  description: string;
  mode: Mode;
  min_size: number;
  max_size: number;
  guardrails: Record<string, unknown>;
  parent_id: string | null;
  created_at: string;
  updated_at: string;
  members: Member[];
}

export interface RoleInfo {
  key: string;
  title: string;
  description: string;
  overseer: boolean;
  default_tools: string[];
}

export interface Run {
  id: string;
  council_id: string | null;
  parent_run_id: string | null;
  topic: string;
  description: string;
  mode: Mode;
  status: RunStatus;
  outputs: OutputFormat[];
  guardrails: Guardrails;
  usage: { usd?: number; tokens_in?: number; tokens_out?: number; steps?: number; llm_calls?: number; runtime_s?: number };
  error: string | null;
  last_seq: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDetail extends Run {
  result: string;
  council_snapshot: { council_id: string | null; name: string; members: Member[] };
}

export interface Guardrails {
  budget: { usd: number | null; tokens: number | null };
  max_steps: number;
  max_runtime_minutes: number;
  max_parallel_agents: number;
  max_agent_iterations: number;
  tools: { allow: string[]; deny: string[] };
  skills: { allow: string[]; deny: string[] };
  network: { allow_domains: string[]; block_private_ips: boolean };
  approval_required: string[];
  content_policy: { preset: "off" | "standard" | "strict"; blocked_topics: string[] };
  sandbox: { root: string; read_only_paths: string[]; max_file_mb: number };
  oversight: {
    intercept_mode: "annotate" | "hold" | "block";
    monitor_sensitivity: "off" | "low" | "medium" | "high";
    loop_threshold: number;
    max_corrections_per_thought: number;
  };
}

export interface Actor {
  kind: "agent" | "user" | "system" | "remote";
  member_id?: string;
  role?: string;
  name?: string;
  overseer?: boolean;
  protocol?: string;
}

export interface RunEvent<T = Record<string, unknown>> {
  v: number;
  seq: number;
  id: string;
  run_id: string;
  council_id?: string;
  ts: string;
  type: string;
  actor: Actor;
  step_id?: string;
  causation_id?: string;
  ephemeral?: boolean;
  data: T;
}

export interface ArtifactInfo {
  path: string;
  version: number;
  size: number;
  kind: string;
  mime: string;
  created_by: string;
  updated_at: string;
}

export interface ProviderKind {
  key: string;
  label: string;
  protocol: string;
  default_base_url: string | null;
  requires_api_key: boolean;
  self_hosted: boolean;
  suggested_models: string[];
}

export interface ProviderConfig {
  id: string;
  name: string;
  kind: string;
  base_url: string | null;
  default_model: string | null;
  enabled: boolean;
  options: Record<string, unknown>;
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
}

export interface SkillSummary {
  name: string;
  description: string;
  version: string;
  allowed_tools: string[];
  files: string[];
  source: string;
  enabled: boolean;
  builtin: boolean;
  shadows_builtin: boolean;
}

export interface SkillDetail extends SkillSummary {
  skill_md: string;
  file_contents: Record<string, string>;
}

export interface ApiKeyInfo {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked: boolean;
}

export interface McpServerInfo {
  id: string;
  name: string;
  transport: "http" | "stdio";
  url: string | null;
  command: string[];
  header_names: string[];
  tool_allow: string[];
  risk: string[];
  enabled: boolean;
  tools: { name: string; description: string }[];
  created_at: string;
  updated_at: string;
}

export interface RemoteAgentInfo {
  id: string;
  name: string;
  protocol: "a2a" | "acp";
  url: string;
  agent_name: string | null;
  description: string;
  enabled: boolean;
  has_token: boolean;
  card: Record<string, unknown>;
  created_at: string;
}

export interface LedgerEntry {
  seq: number;
  ts: string;
  protocol: "mcp" | "a2a" | "acp";
  direction: "inbound" | "outbound";
  peer: string;
  method: string;
  status: string;
  latency_ms: number;
  run_id: string | null;
  request_sha256: string;
  response_sha256: string;
  hash: string;
  prev_hash: string;
  detail: Record<string, unknown>;
}
