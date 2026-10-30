"""Wire contract for Quorum.

Everything the frontend sees (HTTP bodies, SSE payloads, pack files) is defined here.
`scripts/export_contracts.py` turns these models into the frozen files under `contracts/`.
Bump CONTRACT_VERSION on any change that alters a field.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0.0"

Verdict = Literal["issue", "no_issue", "true", "false", "unsure"]
Severity = Literal["low", "medium", "high", "critical"]
StopReason = Literal["consensus", "stalemate", "max_rounds", "budget"]
Mode = Literal["review", "claim"]
Provider = Literal["ollama", "openai_compat", "fake"]
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


class Finding(FindingDraft):
    id: str
    tool_verified: bool = False


class ProfileCell(Strict):
    n: int = Field(ge=0)
    correct: int = Field(ge=0)
    rate: float = Field(ge=0, le=1)
    lower: float = Field(ge=0, le=1, description="Wilson 95% lower bound of the rate")
    weight: float = Field(ge=0, le=1, description="max(lower, 0.05)")


class Pack(Strict):
    id: str
    name: str
    description: str = ""
    categories: list[str]
    category_descriptions: dict[str, str] = Field(default_factory=dict)
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


class DebaterOutput(Strict):
    """The JSON a debating model must return, every round."""

    position: Position
    findings: list[FindingDraft] = Field(default_factory=list, max_length=12)
    changed: bool = False
    changed_reason: str = Field(default="", max_length=800)
    text: str = Field(default="", max_length=2000)


class JudgeRulingDraft(Strict):
    cluster_id: str
    accept: bool
    reason: str = Field(default="", max_length=800)


class JudgeOutput(Strict):
    rulings: list[JudgeRulingDraft]
    reason: str = Field(default="", max_length=1200)


# --------------------------------------------------------------------------- tally / verdict


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
    weighted_score: float = Field(description="sum(weight * confidence) over supporters")
    total_weight: float = Field(description="Sum of the weights of all answering debaters")
    support_share: float = Field(
        ge=0, le=1, description="supporters' weight / total_weight"
    )
    vote_accept: bool = Field(description="Weighted vote outcome, before the judge")
    tool_verified: bool = False


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
    input_flags: list[str] = Field(
        default_factory=list, description="Suspected prompt-injection patterns found in the input"
    )


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


class Final(Strict):
    run_id: str
    verdict: FinalVerdict
    confirmed_findings: list[Cluster]
    disputed_findings: list[Cluster]
    dissent: list[Dissent]
    usage: Usage


class ErrorEvent(Strict):
    code: str
    message: str


RUN_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "run_started": RunStarted,
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


class ModelInfo(Strict):
    id: str
    provider: Provider
    tag: str
    base_url: str | None = None
    family: str
    license: str
    open_weight: bool
    status: ModelStatus
    status_detail: str = ""
    installed: bool | None = Field(default=None, description="Ollama only: tag is pulled locally")


class ModelCreate(Strict):
    provider: Provider
    tag: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(
        default=None, description="Required for openai_compat; e.g. http://127.0.0.1:1234/v1"
    )
    api_key_env: str | None = Field(
        default=None, description="Name of an environment variable holding the key. Never the key."
    )
    family: str | None = None
    license: str | None = None
    open_weight: bool | None = None
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
