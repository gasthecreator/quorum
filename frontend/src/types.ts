import type { RoleAssignment, RoleName } from "./contracts";

export type EventEnvelope<T = Record<string, unknown>> = {
  seq: number;
  event: string;
  data: T;
};

export type Finding = {
  id?: string;
  category: string;
  file?: string;
  line_start: number;
  line_end: number;
  severity: "low" | "medium" | "high" | "critical";
  claim: string;
  evidence: string;
  fix?: string;
  tool_verified?: boolean;
};

export type ModelMessage = {
  round: number;
  model_id: string;
  alias: string;
  position: { verdict: string; confidence: number };
  findings: Finding[];
  changed: boolean;
  changed_reason: string;
  text: string;
  critique?: { strongest: string; biggest_blind_spot: string; missed_by_all: string } | null;
  role?: RoleName | null;
  error?: string | null;
};

export type Supporter = {
  model_id: string;
  alias: string;
  confidence: number;
  weight: number;
  weight_source: "measured" | "pooled" | "default";
  finding_id?: string | null;
};

export type Cluster = {
  id: string;
  kind: "finding" | "verdict";
  verdict?: string | null;
  category: string;
  file?: string;
  line_start: number;
  line_end: number;
  severity: Finding["severity"];
  claim: string;
  evidence: string;
  supporters: Supporter[];
  weighted_score: number;
  total_weight: number;
  support_share: number;
  vote_accept: boolean;
  tool_verified: boolean;
};

export type Fixture = {
  file: string;
  description: string;
  terminal_event: string;
  stop_reason: string | null;
  request: {
    pack: string;
    input: string;
    max_rounds: number;
    models: string[];
    judge: string;
    escalate?: "auto" | "always";
  };
};

export type RunStarted = {
  run_id: string;
  pack: string;
  mode: "review" | "claim";
  model_ids: string[];
  aliases: Record<string, string>;
  judge_model_id: string;
  max_rounds: number;
  input_flags?: string[];
  frame: boolean;
  escalate: "auto" | "always";
  ground?: boolean;
  roles?: Record<string, RoleAssignment>;
};

export type Grounded = {
  tool: string;
  available: boolean;
  findings: Array<{ category: string; file: string; line_start: number; line_end: number; severity: string; claim: string; rule_id: string }>;
  error?: string | null;
  duration_ms: number;
};

export type Framed = { model_id: string; restatement: string; error?: string | null; input_flags?: string[] };
export type Escalated = { reason: "disagreement" | "low_confidence"; detail: string; first_pass_model_ids: string[]; added_model_ids: string[]; agreement: number };
export type FinishedEarly = { reason: string; model_ids: string[]; agreement: number; min_confidence: number; calls_saved: number };
export type JudgeRuling = {
  judge_model_id: string;
  rulings: Array<{ cluster_id: string; accept: boolean; vote_accept: boolean; overrides_vote: boolean; reason: string }>;
  overrides_vote: boolean;
  reason: string;
  injection_flags: string[];
  error?: string | null;
  skipped?: boolean;
  recommendation?: string;
};

export type FinalResult = {
  run_id: string;
  verdict: { overall: string; summary: string };
  confirmed_findings: Cluster[];
  disputed_findings: Cluster[];
  dissent: Array<{ model_id: string; alias: string; verdict: string; reason: string }>;
  agreements?: Array<{ cluster_id: string; claim: string; aliases: string[] }>;
  clashes?: Array<{ cluster_id: string; claim: string; for_side: { aliases: string[]; argument: string }; against_side: { aliases: string[]; argument: string } }>;
  blind_spots?: Array<{ alias: string; kind: string; text: string }>;
  recommendation?: { verdict: string; text: string; confidence: number };
  next_step?: { text: string; cluster_id?: string | null; patch?: string | null };
  usage?: { calls: number; prompt_tokens: number; completion_tokens: number; elapsed_ms: number };
};
