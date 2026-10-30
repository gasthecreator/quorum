// Typed HTTP surface mirrored from contracts/openapi.json (contract v1.1.0).
// Keep these wire types aligned with the frozen OpenAPI document.
export type RunRequest = {
  pack: string;
  input: string;
  model_ids: string[];
  max_rounds?: number;
  judge_model_id?: string | null;
  category?: string | null;
  seed?: number | null;
  frame?: boolean | null;
  escalate?: "auto" | "always";
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

export type Health = {
  ok: boolean;
  version: string;
  contract_version: string;
  ollama: { reachable: boolean; version?: string | null; error?: string | null };
  models: number;
  fake_models_enabled?: boolean;
};

export type ApiError = { code: string; message: string };
