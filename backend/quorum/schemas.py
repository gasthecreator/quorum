"""Wire contract for Quorum.

Everything the frontend sees (HTTP bodies, SSE payloads, pack files) is defined here.
`scripts/export_contracts.py` turns these models into the frozen files under `contracts/`.
Bump CONTRACT_VERSION on any change that alters a field.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.3.0"

Verdict = Literal["issue", "no_issue", "true", "false", "unsure"]
Severity = Literal["low", "medium", "high", "critical"]
StopReason = Literal["consensus", "stalemate", "max_rounds", "budget"]
Mode = Literal["review", "claim"]
Provider = Literal["ollama", "openai_compat", "digitalocean", "fake"]
Locality = Literal["local", "hosted"]
Escalate = Literal["auto", "always"]
ModelStatus = Literal["registered", "calibrating", "ready", "unavailable", "error"]
JobStatus = Literal["queued", "running", "done", "error"]
WeightSource = Literal["measured", "pooled", "default"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- core objects


class Position(Strict):
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)


class FindingDraft(Strict):
    """A finding as a debater writes it. The engine assigns the id."""

    category: str = Field(max_length=64)
    file: str = Field(default="", max_length=256)
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    severity: Severity = "medium"
    claim: str = Field(max_length=600)
    evidence: str = Field(default="", max_length=1200)
    fix: str = Field(default="", max_length=1200, description="Suggested replacement code, if any")


class Finding(FindingDraft):
    id: str
    tool_verified: bool = False


class ProfileCell(Strict):
    n: int = Field(ge=0)
    correct: int = Field(ge=0)
    rate: float = Field(ge=0, le=1)
    lower: float = Field(ge=0, le=1, description="Wilson 95% lower bound of the rate")
    weight: float = Field(ge=0, le=1, description="max(lower, 0.05)")


class RoleAssignment(Strict):
    role: Literal["attacker", "maintainer", "operator", "outsider"]
    source: Literal["measured", "default", "chosen"] = Field(
        description="measured: picked from the model's profile; default: no calibration yet; "
        "chosen: the outsider")
    detail: str = ""


class Pack(Strict):
    id: str
    name: str
    description: str = ""
    categories: list[str]
    category_descriptions: dict[str, str] = Field(default_factory=dict)
    category_roles: dict[str, Literal["attacker", "maintainer", "operator"]] = Field(
        default_factory=dict,
        description="Optional: pin a category to a role. Otherwise the backend guesses from the name")
    modes: list[Mode]
    corpus: dict[Literal["calibration", "heldout"], str] = Field(
        description="Paths relative to the pack directory"
    )
    corpus_counts: dict[str, int] = Field(default_factory=dict, description="Filled by the API")


class ReviewLabel(Strict):
    has_issue: bool
    issue_category: str | None = None
    lines: tuple[int, int] | None = Field(
        default=None, description="1-based inclusive line numbers of the corpus item's `input`"
    )


class ClaimLabel(Strict):
    truth: bool
    source_url: str


class CorpusItem(Strict):
    id: str
    category: str
    input: str
    label: ReviewLabel | ClaimLabel


# --------------------------------------------------------------------------- debater output


class Critique(Strict):
    """Rounds 1 and 2: a structured critique of the other panelists' latest answers."""

    strongest: str = Field(default="", max_length=500, description="Which message is strongest, and why")
    biggest_blind_spot: str = Field(default="", max_length=500)
    missed_by_all: str = Field(default="", max_length=500, description="What did everyone miss")


class DebaterOutput(Strict):
    """The JSON a debating model must return, every round."""

    position: Position
    findings: list[FindingDraft] = Field(default_factory=list, max_length=12)
    changed: bool = False
    changed_reason: str = Field(default="", max_length=800)
    text: str = Field(default="", max_length=2000)
    critique: Critique | None = Field(default=None, description="Asked for in rounds 1 and 2 only")


class FramerOutput(Strict):
    restatement: str = Field(max_length=1200)


class JudgeRulingDraft(Strict):
    cluster_id: str
    accept: bool
    reason: str = Field(default="", max_length=800)


class JudgeOutput(Strict):
    rulings: list[JudgeRulingDraft]
    reason: str = Field(default="", max_length=1200)
    recommendation: str = Field(default="", max_length=600, description="One direct sentence")


# --------------------------------------------------------------------------- tally / verdict


class ToolFinding(Strict):
    """What a static-analysis tool reported. Line numbers are lines of the pasted input."""

    tool: str
    rule_id: str
    category: str
    file: str = ""
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    claim: str


class Supporter(Strict):
    model_id: str
    alias: str
    confidence: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)
    weight_source: WeightSource
    finding_id: str | None = None


class Cluster(Strict):
    id: str
    kind: Literal["finding", "verdict"]
    verdict: Verdict | None = Field(default=None, description="Set when kind is 'verdict'")
    category: str
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    severity: Severity = "medium"
    claim: str
    evidence: str = ""
    supporters: list[Supporter]
    fix: str = ""
    weighted_score: float = Field(description="sum(weight * confidence) over supporters")
    total_weight: float = Field(description="Sum of the weights of all answering debaters")
    support_share: float = Field(
        ge=0, le=1, description="supporters' weight / total_weight"
    )
    vote_accept: bool = Field(description="Weighted vote outcome, before the judge")
    tool_verified: bool = Field(
        default=False,
        description="A static-analysis tool confirmed it. Such a cluster is always accepted, and the "
        "judge cannot reject it. May have no supporters (the tool found what no model did).")
    tool_rule: str | None = None


class ClusterRuling(Strict):
    cluster_id: str
    accept: bool
    vote_accept: bool
    overrides_vote: bool
    reason: str = ""


class Dissent(Strict):
    model_id: str
    alias: str
    verdict: Verdict
    confidence: float
    finding_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class FinalVerdict(Strict):
    overall: Verdict
    summary: str


class Agreement(Strict):
    """A cluster every answering panelist backed."""

    cluster_id: str | None = Field(default=None, description="None when the panel agreed there is nothing to flag")
    claim: str
    aliases: list[str]


class ClashSide(Strict):
    aliases: list[str]
    argument: str = ""


class Clash(Strict):
    """A cluster the panel split on. Both sides are shown."""

    cluster_id: str
    claim: str
    for_side: ClashSide
    against_side: ClashSide


class BlindSpot(Strict):
    alias: str
    kind: Literal["biggest_blind_spot", "missed_by_all"]
    text: str


class Recommendation(Strict):
    verdict: Verdict
    text: str
    confidence: float = Field(
        ge=0, le=1,
        description="weight x confidence of panelists who agree with the verdict / total weight")
    tool_verified: bool = Field(
        default=False, description="A static-analysis tool confirmed at least one finding behind "
        "this verdict. Confidence still reflects the panel only, so it can be low or 0")


class NextStep(Strict):
    cluster_id: str | None = None
    text: str
    patch: str | None = Field(default=None, description="Suggested replacement code, when a panelist gave one")


class Usage(Strict):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0


# --------------------------------------------------------------------------- SSE events


class RunStarted(Strict):
    run_id: str
    pack: str
    mode: Mode
    model_ids: list[str]
    aliases: dict[str, str]
    judge_model_id: str
    max_rounds: int
    escalate: Escalate
    frame: bool
    ground: bool = False
    roles: dict[str, RoleAssignment] = Field(
        default_factory=dict, description="model_id -> role, empty unless roles=profile or outsider")
    input_flags: list[str] = Field(
        default_factory=list, description="Suspected prompt-injection patterns found in the input"
    )


class Framed(Strict):
    """A neutral restatement of the input, produced before round 0 so a leading prompt cannot steer
    every model the same way. Debaters see it next to the original, never instead of it."""

    model_id: str
    restatement: str
    error: str | None = None
    input_flags: list[str] = Field(default_factory=list)


class Escalated(Strict):
    reason: Literal["disagreement", "low_confidence"]
    detail: str
    first_pass_model_ids: list[str]
    added_model_ids: list[str]
    agreement: float


class FinishedEarly(Strict):
    reason: str
    model_ids: list[str]
    agreement: float
    min_confidence: float
    calls_saved: int = Field(description="Lower bound: one blind call per model not consulted, plus the judge")


class Grounded(Strict):
    """Static-analysis results, sent after the debate stops and before `tally`. Only when ground=true."""

    tool: str
    available: bool
    findings: list[ToolFinding]
    error: str | None = None
    duration_ms: int = 0


class RoundStarted(Strict):
    round: int = Field(ge=0, description="0 is the blind round")


class ModelMessage(Strict):
    round: int
    model_id: str
    alias: str
    position: Position
    findings: list[Finding]
    changed: bool
    changed_reason: str
    text: str
    critique: Critique | None = None
    role: Literal["attacker", "maintainer", "operator", "outsider"] | None = None
    error: str | None = Field(default=None, description="Set when the model failed to answer")
    prompt_tokens: int = 0
    completion_tokens: int = 0


class RoundFinished(Strict):
    round: int
    agreement: float = Field(ge=0, le=1)


class Stopped(Strict):
    reason: StopReason
    round: int


class Tally(Strict):
    clusters: list[Cluster]
    threshold: float = Field(description="support_share above which a cluster is accepted")


class JudgeRuling(Strict):
    judge_model_id: str
    rulings: list[ClusterRuling]
    overrides_vote: bool
    reason: str
    injection_flags: list[str] = Field(default_factory=list)
    error: str | None = None
    skipped: bool = Field(default=False, description="True when the early-finish path skipped the judge")
    recommendation: str = Field(default="", description="The judge's one-sentence recommendation, if given")


class Final(Strict):
    run_id: str
    verdict: FinalVerdict
    confirmed_findings: list[Cluster]
    disputed_findings: list[Cluster]
    dissent: list[Dissent]
    agreements: list[Agreement]
    clashes: list[Clash]
    blind_spots: list[BlindSpot]
    recommendation: Recommendation
    next_step: NextStep | None = None
    usage: Usage


class ErrorEvent(Strict):
    code: str
    message: str


RUN_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "run_started": RunStarted,
    "framed": Framed,
    "grounded": Grounded,
    "escalated": Escalated,
    "finished_early": FinishedEarly,
    "round_started": RoundStarted,
    "model_message": ModelMessage,
    "round_finished": RoundFinished,
    "stopped": Stopped,
    "tally": Tally,
    "judge_ruling": JudgeRuling,
    "final": Final,
    "error": ErrorEvent,
}


class JobProgress(Strict):
    job_id: str
    done: int
    total: int
    message: str = ""


class JobDone(Strict):
    job_id: str
    result: dict


class JobError(Strict):
    job_id: str
    code: str
    message: str


JOB_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "job_progress": JobProgress,
    "job_done": JobDone,
    "job_error": JobError,
}


class EventEnvelope(Strict):
    """One SSE frame: `id: <seq>`, `event: <event>`, `data: <json of data>`."""

    seq: int
    event: str
    data: dict


# --------------------------------------------------------------------------- HTTP bodies


class OllamaHealth(Strict):
    reachable: bool
    base_url: str
    version: str | None = None


class Health(Strict):
    ok: bool
    version: str
    contract_version: str
    ollama: OllamaHealth
    models: int
    fake_models_enabled: bool = False
    grounding: dict[str, bool] = Field(
        default_factory=dict, description="Static-analysis tools that are installed, e.g. {semgrep: true}")


class ModelInfo(Strict):
    id: str
    provider: Provider
    tag: str
    base_url: str | None = None
    family: str
    license: str
    open_weight: bool
    locality: Locality
    status: ModelStatus
    status_detail: str = ""
    installed: bool | None = Field(default=None, description="Ollama only: tag is pulled locally")


class ModelCreate(Strict):
    provider: Provider
    tag: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(
        default=None,
        description="Required for openai_compat, e.g. http://127.0.0.1:1234/v1. "
        "digitalocean defaults to https://inference.do-ai.run/v1")
    api_key_env: str | None = Field(
        default=None, description="Name of an environment variable holding the key. Never the key."
    )
    family: str | None = None
    license: str | None = None
    open_weight: bool | None = None
    locality: Locality | None = Field(
        default=None, description="Inferred from the host when omitted; digitalocean is always hosted")
    calibrate: bool = True
    packs: list[str] | None = Field(default=None, description="Packs to calibrate; default all")


class ModelCreated(Strict):
    model: ModelInfo
    job_id: str | None = None


class RunRequest(Strict):
    pack: str
    input: str = Field(min_length=1, max_length=60_000)
    model_ids: list[str] = Field(min_length=1, max_length=6)
    max_rounds: int = Field(default=2, ge=0, le=3)
    judge_model_id: str | None = None
    category: str | None = Field(
        default=None, description="Optional category hint used to pick weights"
    )
    seed: int | None = None
    roles: Literal["off", "profile"] = Field(
        default="off",
        description="profile: give each model the role it measured best at (review packs only)")
    outsider: bool = Field(
        default=False,
        description="With 3+ models, the weakest specialist reviews the bare input in round 0 only: "
        "no framing, no peers, never revises")
    ground: bool = Field(
        default=False,
        description="Run a static analyser (Semgrep) over the input. A tool-confirmed finding is "
        "accepted regardless of the vote. Review packs only; a no-op if the tool is missing")
    escalate: Escalate = Field(
        default="always",
        description="always: full debate. auto: best model + best other-family model first; "
        "full debate only if they disagree or either is unsure")
    frame: bool | None = Field(
        default=None,
        description="Run the framer before round 0. Default: true for escalate=always, false for auto "
        "(so an easy input costs exactly 2 model calls)")


class RunCreated(Strict):
    run_id: str


class RunSummary(Strict):
    run_id: str
    status: Literal["running", "done", "error"]
    request: RunRequest
    created_at: str
    events: list[EventEnvelope] = Field(default_factory=list)
    final: Final | None = None


class ProfilesResponse(Strict):
    profiles: dict[str, dict[str, dict[str, ProfileCell]]] = Field(
        description="model_id -> pack_id -> category -> cell"
    )
    default_weight: float


class CalibrateRequest(Strict):
    model_ids: list[str] = Field(min_length=1)
    pack: str
    limit: int | None = Field(default=None, ge=1, description="Use only the first N items")


class EvalRequest(Strict):
    pack: str
    model_ids: list[str] = Field(min_length=1, max_length=6)
    judge_model_id: str | None = None
    max_rounds: int = Field(default=2, ge=0, le=3)
    limit: int | None = Field(default=None, ge=1)
    roles: bool = Field(
        default=False,
        description="Also run condition 7: the full debate with profile-assigned roles")


class JobCreated(Strict):
    job_id: str


class Job(Strict):
    id: str
    kind: Literal["calibrate", "eval"]
    status: JobStatus
    done: int = 0
    total: int = 0
    message: str = ""
    result: dict | None = None
    error: str | None = None


class Interval(Strict):
    value: float
    lo: float
    hi: float


class ConditionResult(Strict):
    id: int
    name: str
    available: bool
    reason_unavailable: str = ""
    n_items: int = 0
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    precision: Interval | None = None
    recall: Interval | None = None
    f1: Interval | None = None
    fpr: Interval | None = None
    accuracy: Interval | None = None


class DebateMovement(Strict):
    toward_correct: int
    toward_wrong: int
    neutral: int
    note: str = ""


class EvalResult(Strict):
    id: str
    created_at: str
    pack: str
    model_ids: list[str]
    judge_model_id: str
    max_rounds: int
    n_items: int
    n_positive: int = Field(description="Buggy diffs, or false claims")
    n_negative: int = Field(description="Clean controls, or true claims")
    conditions: list[ConditionResult]
    debate_movement: DebateMovement
    notes: list[str] = Field(default_factory=list)


class ApiError(Strict):
    code: str
    message: str


AnyEventData = Annotated[dict, Field(description="Payload; schema depends on `event`")]
