// Typed HTTP surface mirrored from contracts/openapi.json (contract v1.3.0).
// Keep these wire types aligned with the frozen OpenAPI document.
export type RunRequest = {
  pack: string;
  input: string;
  model_ids: string[];
  max_rounds?: number;
  judge_model_id?: string | null;
  category?: string | null;
  seed?: number | null;
  roles?: "off" | "profile";
  outsider?: boolean;
  ground?: boolean;
  frame?: boolean | null;
  escalate?: "auto" | "always";
};

export type RoleName = "attacker" | "maintainer" | "operator" | "outsider";
export type RoleAssignment = {
  role: RoleName;
  source: "measured" | "default" | "chosen";
  detail: string;
};

export type RunCreated = { run_id: string };

export type ModelInfo = {
  id: string;
  provider: "ollama" | "openai_compat" | "digitalocean" | "fake";
  tag: string;
  family: string;
  license: string;
  open_weight: boolean;
  locality: "local" | "hosted";
  status: "registered" | "calibrating" | "ready" | "unavailable" | "error";
  base_url?: string | null;
  installed?: boolean | null;
  status_detail?: string;
};

export type PackInfo = {
  id: string;
  name: string;
  description?: string;
  categories: string[];
  category_descriptions?: Record<string, string>;
  category_roles?: Record<string, "attacker" | "maintainer" | "operator">;
  modes: Array<"review" | "claim">;
  corpus: Partial<Record<"calibration" | "heldout", string>>;
  corpus_counts?: Record<string, number>;
};

export type ProfileCell = { n: number; correct: number; rate: number; lower: number; weight: number };
export type ProfilesResponse = {
  default_weight: number;
  profiles: Record<string, Record<string, Record<string, ProfileCell>>>;
};

export type ModelCreate = {
  provider: "ollama" | "openai_compat" | "digitalocean";
  tag: string;
  base_url?: string | null;
  api_key_env?: string | null;
  family?: string | null;
  license?: string | null;
  open_weight?: boolean | null;
  locality?: "local" | "hosted" | null;
  calibrate?: boolean;
  packs?: string[] | null;
};
export type ModelCreated = { model: ModelInfo; job_id?: string | null };
export type JobProgress = { job_id: string; done: number; total: number; message: string };
export type JobDone = { job_id: string; result: Record<string, unknown> };
export type JobError = { job_id: string; code: string; message: string };
export type JobCreated = { job_id: string };
export type EvalRequest = { pack: string; model_ids: string[]; judge_model_id?: string | null; max_rounds?: number; limit?: number | null; roles?: boolean };
export type EvalInterval = { value: number; lo: number; hi: number };
export type EvalCondition = {
  id: number; name: string; available: boolean; reason_unavailable: string; n_items: number;
  tp: number; fp: number; tn: number; fn: number;
  precision?: EvalInterval | null; recall?: EvalInterval | null; f1?: EvalInterval | null;
  fpr?: EvalInterval | null; accuracy?: EvalInterval | null;
};
export type EvalResult = {
  id: string; created_at: string; pack: string; model_ids: string[]; judge_model_id: string;
  max_rounds: number; n_items: number; n_positive: number; n_negative: number;
  conditions: EvalCondition[];
  debate_movement: { toward_correct: number; toward_wrong: number; neutral: number; note: string };
  notes: string[];
};

export type Health = {
  ok: boolean;
  version: string;
  contract_version: string;
  ollama: { reachable: boolean; version?: string | null; error?: string | null };
  models: number;
  grounding?: Record<string, boolean>;
  fake_models_enabled?: boolean;
};

export type ApiError = { code: string; message: string };

// ---------------------------------------------------------------------------- group chat (contract v1.4)

export type ChatCreate = {
  model_ids: string[];
  judge_model_id?: string | null;
  rounds?: number;
  title?: string | null;
  message?: string | null;
};

export type ChatCreated = { chat_id: string; turn?: number | null; message_id?: string | null };
export type ChatMessageAccepted = { chat_id: string; turn: number; message_id: string };

export type ChatListItem = {
  chat_id: string;
  title: string;
  model_ids: string[];
  created_at: string;
  turns: number;
  last_message: string;
};

export type ChatSummary = {
  chat_id: string;
  title: string;
  model_ids: string[];
  aliases: Record<string, string>;
  judge_model_id: string;
  rounds: number;
  created_at: string;
  status: "idle" | "replying";
  events: { seq: number; event: string; data: Record<string, unknown> }[];
};
