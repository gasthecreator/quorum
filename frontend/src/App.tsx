import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Activity, ArrowDown, ArrowRight, BadgeCheck, Braces, Check, ChevronDown, CircleHelp,
  CircleStop, Clock3, Download, FileCode2, FileUp, Gauge, GitBranch, Layers3, LoaderCircle,
  MessageSquareText, Play, Radio, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, Target,
  TriangleAlert, UsersRound, X,
} from "lucide-react";
import { addModel, downloadRunExport, getHealth, getModels, getPacks, getProfiles, loadFixtures, startFixtureRun, startLiveRun, subscribeToJob, subscribeToRun } from "./api";
import type { ModelCreate, ModelInfo, PackInfo, ProfileCell, ProfilesResponse, RunRequest } from "./contracts";
import type { Cluster, EventEnvelope, Fixture, FinalResult, JudgeRuling, ModelMessage, RunStarted } from "./types";

type RoundFinished = { round: number; agreement: number };
type Stopped = { reason: "consensus" | "stalemate" | "max_rounds" | "budget"; round?: number };
const fmt = (value: number) => `${Math.round(value * 100)}%`;
const asRecord = (value: unknown) => (value && typeof value === "object" ? value as Record<string, unknown> : {});
const labels: Record<string, string> = {
  consensus: "Consensus reached", stalemate: "Debate stalled", max_rounds: "Round cap reached", budget: "Budget reached",
};
const apiMode = import.meta.env.VITE_API ?? "mock";
const liveRoot = "http://127.0.0.1:8787";

function App() {
  const [fixtures, setFixtures] = useState<Fixture[]>([]);
  const [selectedFile, setSelectedFile] = useState("consensus.jsonl");
  const [inputText, setInputText] = useState("");
  const [packs, setPacks] = useState<PackInfo[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [healthOk, setHealthOk] = useState(false);
  const [backendLoading, setBackendLoading] = useState(apiMode === "live");
  const [selectedPack, setSelectedPack] = useState("code-review");
  const [selectedCategory, setSelectedCategory] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [judgeModelId, setJudgeModelId] = useState("");
  const [maxRounds, setMaxRounds] = useState(2);
  const [escalate, setEscalate] = useState<"always" | "auto">("always");
  const [frameEnabled, setFrameEnabled] = useState(true);
  const [fileName, setFileName] = useState("");
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [runState, setRunState] = useState<"idle" | "loading" | "running" | "done" | "error">("idle");
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [exportError, setExportError] = useState("");
  const [page, setPage] = useState<"review" | "profiles" | "models">("review");
  const closeStream = useRef<(() => void) | null>(null);

  useEffect(() => {
    let active = true;
    async function loadWorkspace() {
      setError("");
      if (apiMode === "mock") {
        try {
          const items = await loadFixtures();
          if (!active) return;
          setFixtures(items);
          const chosen = items.find((item) => item.file === selectedFile) ?? items[0];
          if (chosen) {
            setSelectedFile(chosen.file);
            setInputText(chosen.request.input);
          }
        } catch (caught) {
          if (!active) return;
          setError(caught instanceof Error ? caught.message : "Unable to load the demo fixtures.");
        }
        return;
      }

      setBackendLoading(true);
      try {
        const [health, packItems, modelItems] = await Promise.all([getHealth(), getPacks(), getModels()]);
        if (!active) return;
        setHealthOk(health.ok);
        setPacks(packItems);
        setModels(modelItems);
        const nextPack = packItems.find((item) => item.id === selectedPack) ?? packItems[0];
        if (nextPack) setSelectedPack(nextPack.id);
        const available = modelItems.filter(canUseForReview);
        const preferred = diverseModelSelection(available);
        setSelectedModels(preferred);
        setJudgeModelId(preferred[0] ?? "");
      } catch (caught) {
        if (!active) return;
        setHealthOk(false);
        setError(caught instanceof Error ? caught.message : "Unable to connect to the Quorum API.");
      } finally {
        if (active) setBackendLoading(false);
      }
    }
    void loadWorkspace();
    return () => {
      active = false;
      closeStream.current?.();
    };
  }, [reloadKey]);

  const fixture = fixtures.find((item) => item.file === selectedFile) ?? fixtures[0];
  const selectedPackInfo = packs.find((item) => item.id === selectedPack);
  const availableModels = models.filter((item) => item.open_weight && ["ready", "registered"].includes(item.status));
  const selectedHostedModels = models.filter((item) => selectedModels.includes(item.id) && item.locality === "hosted");
  const started = events.find((event) => event.event === "run_started")?.data as RunStarted | undefined;
  const stopped = events.find((event) => event.event === "stopped")?.data as Stopped | undefined;
  const final = events.find((event) => event.event === "final")?.data as FinalResult | undefined;
  const ruling = events.find((event) => event.event === "judge_ruling")?.data as JudgeRuling | undefined;
  const framed = events.find((event) => event.event === "framed")?.data as { restatement?: string; error?: string | null } | undefined;
  const escalation = events.find((event) => event.event === "escalated")?.data as { reason?: string; detail?: string } | undefined;
  const earlyFinish = events.find((event) => event.event === "finished_early")?.data as { calls_saved?: number; reason?: string } | undefined;
  const rounds = useMemo(() => {
    const byRound = new Map<number, { messages: ModelMessage[]; agreement?: number; finished: boolean }>();
    for (const event of events) {
      if (event.event === "round_started") {
        const round = Number(asRecord(event.data).round ?? 0);
        if (!byRound.has(round)) byRound.set(round, { messages: [], finished: false });
      }
      if (event.event === "model_message") {
        const message = event.data as ModelMessage;
        const round = byRound.get(message.round) ?? { messages: [], finished: false };
        round.messages.push(message);
        byRound.set(message.round, round);
      }
      if (event.event === "round_finished") {
        const data = event.data as RoundFinished;
        const round = byRound.get(data.round) ?? { messages: [], finished: false };
        round.agreement = data.agreement;
        round.finished = true;
        byRound.set(data.round, round);
      }
    }
    return [...byRound.entries()].sort(([a], [b]) => a - b);
  }, [events]);

  const lastRound = rounds.at(-1)?.[0] ?? 0;
  const agreement = rounds.at(-1)?.[1].agreement;
  async function startReview() {
    if (runState === "loading" || runState === "running") return;
    if (apiMode === "mock" && !fixture) return;
    if (apiMode === "live" && (!inputText.trim() || inputText.length > 60000 || selectedModels.length === 0)) return;
    closeStream.current?.();
    setEvents([]);
    setError("");
    setRunState("loading");
    try {
      const runId = apiMode === "mock"
        ? await startFixtureRun(fixture!)
        : await startLiveRun({
            pack: selectedPack,
            input: inputText,
            model_ids: selectedModels,
            max_rounds: maxRounds,
            judge_model_id: judgeModelId || null,
            category: selectedCategory || null,
            escalate,
            frame: frameEnabled,
          } satisfies RunRequest);
      setRunState("running");
      closeStream.current = subscribeToRun(
        apiMode === "mock" ? "/__mock" : liveRoot,
        runId,
        (event) => {
          setEvents((current) => [...current, event]);
          if (event.event === "final") setRunState("done");
          if (event.event === "error") {
            setError(String(asRecord(event.data).message ?? "The run failed."));
            setRunState("error");
          }
        },
        (caught) => {
          setError(caught.message);
          setRunState("error");
        },
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to start this run.");
      setRunState("error");
    }
  }

  const running = runState === "loading" || runState === "running";
  const liveCanRun = Boolean(healthOk && selectedPack && inputText.trim() && inputText.length <= 60000 && selectedModels.length > 0 && !backendLoading);

  function toggleModel(modelId: string) {
    setSelectedModels((current) => {
      if (current.includes(modelId)) return current.filter((id) => id !== modelId);
      if (current.length >= 6) return current;
      return [...current, modelId];
    });
  }

  async function loadInputFile(file?: File) {
    if (!file) return;
    setError("");
    if (file.size > 240000) {
      setError("This file is too large to load here. The API accepts at most 60,000 characters.");
      return;
    }
    try {
      const content = await file.text();
      if (content.length > 60000) {
        setError("This file exceeds the 60,000 character API limit.");
        return;
      }
      setInputText(content);
      setFileName(file.name);
    } catch {
      setError("Unable to read that file. Choose a plain-text diff or source file.");
    }
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><Layers3 size={19} strokeWidth={2.2} /></div>
          <div><strong>quorum</strong><span>LOCAL MODEL COUNCIL</span></div>
        </div>

        <div className="workspace-switcher">
          <div className="workspace-avatar">Q</div>
          <div className="workspace-copy"><strong>Personal workspace</strong><span>Local instance</span></div>
          <ChevronDown size={14} />
        </div>

        <div className="nav-label">WORKSPACE</div>
        <nav className="primary-nav" aria-label="Main navigation">
          <button className={`nav-item ${page === "review" ? "active" : ""}`} onClick={() => setPage("review")}><MessageSquareText size={17} /><span>Review room</span><span className="nav-shortcut">⌘ 1</span></button>
          <button className={`nav-item ${page === "profiles" ? "active" : ""}`} onClick={() => setPage("profiles")}><Gauge size={17} /><span>Skill profiles</span></button>
          <button className="nav-item disabled" title="Available after F6"><Activity size={17} /><span>Evaluations</span><span className="nav-lock">F6</span></button>
        </nav>

        <div className="nav-label nav-label-spaced">TOOLS</div>
        <nav className="primary-nav" aria-label="Tools">
          <button className={`nav-item ${page === "models" ? "active" : ""}`} onClick={() => setPage("models")}><UsersRound size={17} /><span>Model manager</span></button>
          <button className="nav-item disabled" title="Available after F8"><Braces size={17} /><span>Exports</span><span className="nav-lock">F8</span></button>
        </nav>

        <div className="sidebar-spacer" />
        <div className="local-status"><span className="status-dot" /><span><strong>Local API</strong><small>Model locality shown per model</small></span><ShieldCheck size={15} /></div>
        <div className="profile-chip"><div className="profile-avatar">GS</div><span><strong>Gideon Sanni</strong><small>Developer</small></span><CircleHelp size={16} /></div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><ArrowRight size={13} /><strong>{page === "review" ? "Review room" : page === "profiles" ? "Skill profiles" : "Model manager"}</strong></div>
          <div className="topbar-actions">
            <span className={`connection-pill ${apiMode === "live" && !healthOk ? "connection-offline" : ""}`}><span className="status-dot" />{apiMode === "mock" ? "Fixture mode" : backendLoading ? "Connecting to API" : healthOk ? "Live API connected" : "Live API unavailable"}</span>
            <span className="version-label">Contract 1.1.0</span>
            {apiMode === "live" && <button className="icon-button" aria-label="Reconnect to API" title="Reload models and packs" onClick={() => setReloadKey((key) => key + 1)}><RefreshCw size={15} /></button>}
            <button className="icon-button" aria-label="Help"><CircleHelp size={17} /></button>
          </div>
        </header>

        <div className="page-content">
          {page === "review" ? <>
          <section className="page-heading">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" />LOCAL REVIEW COUNCIL</div>
              <h1>Review room</h1>
              <p>Let independent models challenge a diff, then see how the evidence holds up.</p>
            </div>
            <div className="heading-meta"><span className="meta-icon"><GitBranch size={15} /></span><span><strong>Seeded review</strong><small>Demo workspace</small></span></div>
          </section>

          {apiMode === "mock" ? (
            <section className="scenario-strip" aria-label="Demo scenarios">
              <div className="scenario-intro"><span className="scenario-icon"><Sparkles size={15} /></span><span><strong>Explore a fixture</strong><small>Recorded streams · no inference calls</small></span></div>
              <div className="scenario-picker-wrap">
                <select aria-label="Choose demo scenario" value={selectedFile} onChange={(event) => {
                  const nextFile = event.target.value;
                  setSelectedFile(nextFile);
                  setInputText(fixtures.find((item) => item.file === nextFile)?.request.input ?? "");
                  setEvents([]); setRunState("idle"); setError("");
                }}>
                  {fixtures.map((item) => <option key={item.file} value={item.file}>{scenarioName(item.file)}</option>)}
                </select>
                <ChevronDown size={14} />
              </div>
              <button className="primary-button" onClick={startReview} disabled={!fixture || running}>
                {runState === "loading" ? <LoaderCircle className="spin" size={15} /> : running ? <Radio size={15} /> : <Play size={14} fill="currentColor" />}
                {running ? "Playing run" : "Play demo"}
              </button>
            </section>
          ) : (
            <section className="scenario-strip live-strip" aria-label="Live review connection">
              <div className="scenario-intro"><span className="scenario-icon"><Radio size={15} /></span><span><strong>{healthOk ? "Local backend ready" : backendLoading ? "Connecting to local backend" : "Backend connection needed"}</strong><small>{healthOk ? `Quorum ${packs.length} pack${packs.length === 1 ? "" : "s"} · models remain on your configured providers` : "Start `quorum serve` on 127.0.0.1:8787 to review a diff"}</small></span></div>
              <span className="api-version">{healthOk ? `API ${models.length} model${models.length === 1 ? "" : "s"}` : "127.0.0.1:8787"}</span>
              <button className="secondary-button" onClick={() => setReloadKey((key) => key + 1)} disabled={backendLoading}><RefreshCw size={13} />Refresh</button>
            </section>
          )}

          {apiMode === "live" && <LiveSetup
            packs={packs}
            models={models}
            availableModels={availableModels}
            selectedPack={selectedPack}
            selectedCategory={selectedCategory}
            selectedModels={selectedModels}
            judgeModelId={judgeModelId}
            maxRounds={maxRounds}
            escalate={escalate}
            frameEnabled={frameEnabled}
            loading={backendLoading}
            onPackChange={(id) => { setSelectedPack(id); setSelectedCategory(""); }}
            onCategoryChange={setSelectedCategory}
            onToggleModel={toggleModel}
            onJudgeChange={setJudgeModelId}
            onRoundsChange={setMaxRounds}
            onEscalateChange={(value) => { setEscalate(value); setFrameEnabled(value === "always"); }}
            onFrameChange={setFrameEnabled}
          />}

          {error && <div className="error-banner page-error"><TriangleAlert size={17} /><span><strong>{apiMode === "live" && !healthOk ? "Local API unavailable" : "Review setup needs attention"}</strong><small>{error}</small></span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError("")}><X size={15} /></button></div>}

          <div className="workspace-grid">
            <section className="review-column">
              <div className="panel input-panel">
                <div className="panel-heading">
                  <div className="panel-title"><span className="panel-icon"><FileCode2 size={16} /></span><span><strong>Review input</strong><small>{apiMode === "mock" ? `${fixture?.request.pack ?? "code-review"} · ${inputText.split("\n").length} lines` : `${(selectedPackInfo?.name ?? selectedPack) || "Choose a pack"} · ${inputText.split("\n").length} lines`}</small></span></div>
                  {apiMode === "mock" ? <span className="read-only-chip"><span />Fixture</span> : <label className="file-upload"><FileUp size={13} />{fileName ? "Replace file" : "Upload file"}<input type="file" accept=".diff,.patch,.txt,.py,.js,.ts,.tsx,.go,.rs" onChange={(event) => { void loadInputFile(event.currentTarget.files?.[0]); event.currentTarget.value = ""; }} /></label>}
                </div>
                {apiMode === "mock" ? <div className="code-preview">
                  <div className="code-gutter" aria-hidden="true">{inputText.split("\n").map((_, index) => <span key={index}>{String(index + 1).padStart(2, "0")}</span>)}</div>
                  <pre><code>{inputText || "Loading review fixture…"}</code></pre>
                </div> : <textarea className="live-input" aria-label="Paste a diff, source file, or claim" placeholder={selectedPackInfo?.modes.includes("claim") ? "Paste a factual claim to review…" : "Paste a unified diff or source snippet…"} value={inputText} onChange={(event) => { setInputText(event.target.value); setFileName(""); }} maxLength={60000} spellCheck={false} />}
                <div className="input-footer"><span><ShieldAlert size={14} />Input and peer messages are treated as data</span><span className={inputText.length > 55000 ? "input-limit-near" : ""}>{inputText.length.toLocaleString()} / 60,000 chars</span></div>
              </div>

              {apiMode === "live" && <div className="live-submit-row"><span>{selectedModels.length ? `${selectedModels.length} reviewer${selectedModels.length === 1 ? "" : "s"} selected · ${maxRounds} visible round${maxRounds === 1 ? "" : "s"} maximum` : "Select at least one available reviewer to begin."}</span><button className="primary-button" onClick={startReview} disabled={!liveCanRun || running}>{running ? <LoaderCircle className="spin" size={15} /> : <Play size={14} fill="currentColor" />}{running ? "Starting review" : "Start live review"}</button></div>}
              {apiMode === "live" && selectedHostedModels.length > 0 && <div className="hosted-note"><ShieldAlert size={14} /><span><strong>Hosted model selected</strong> Your review input will be sent to {selectedHostedModels.map((model) => model.tag || model.id).join(", ")} through its configured provider.</span></div>}

              <div className="debate-heading">
                <div className="panel-title"><span className="panel-icon purple"><UsersRound size={16} /></span><span><strong>Debate transcript</strong><small>{started ? `${started.model_ids.length} panelists · ${rounds.length} round${rounds.length === 1 ? "" : "s"}` : "Blind first round, then peer review"}</small></span></div>
                {agreement !== undefined && <div className="agreement-chip"><span>Agreement</span><strong>{fmt(agreement)}</strong></div>}
              </div>

              {framed?.restatement && <div className="event-note"><strong>Neutral framing</strong><p>{framed.restatement}</p></div>}
              {framed?.error && <div className="event-note event-warning">Framing failed; reviewers received the original input. {framed.error}</div>}
              {escalation && <div className="event-note"><strong>Panel escalated · {escalation.reason?.replaceAll("_", " ")}</strong><p>{escalation.detail}</p></div>}
              {earlyFinish && <div className="event-note"><strong>Finished early</strong><p>{earlyFinish.reason} {earlyFinish.calls_saved ? `At least ${earlyFinish.calls_saved} model calls saved.` : ""}</p></div>}

              {events.length === 0 && runState !== "error" ? (
                <div className="empty-debate">
                  <div className="empty-orbit"><span className="orbit-node n1">A</span><span className="orbit-node n2">B</span><span className="orbit-node n3">C</span><div className="orbit-center"><UsersRound size={22} /></div></div>
                  <strong>Independent reads. One evidence-based ruling.</strong>
                  <p>{apiMode === "mock" ? fixture?.description ?? "Choose a fixture, then play the run to see each round unfold." : "Choose open-weight reviewers and provide an input to start a live review."}</p>
                  {apiMode === "mock" && <button className="text-button" onClick={startReview} disabled={!fixture || running}>Play this scenario <ArrowRight size={14} /></button>}
                </div>
              ) : null}

              <div className="round-list">
                {rounds.map(([roundNumber, round]) => (
                  <article className="round-card" key={roundNumber}>
                    <div className="round-divider"><span className="round-badge">{roundNumber === 0 ? "01" : String(roundNumber + 1).padStart(2, "0")}</span><span className="round-name">{roundNumber === 0 ? "Blind review" : `Peer review · round ${roundNumber}`}</span><span className="round-line" />{round.finished && round.agreement !== undefined ? <span className="round-agreement"><Target size={12} />{fmt(round.agreement)} agreement</span> : running && roundNumber === lastRound ? <span className="live-label"><span className="pulse-dot" />In progress</span> : null}</div>
                    <div className="message-list">
                      {round.messages.map((message, index) => <MessageCard key={`${message.round}-${message.model_id}-${index}`} message={message} aliases={started?.aliases ?? {}} />)}
                      {running && round.messages.length === 0 && <div className="round-loading"><LoaderCircle size={14} className="spin" />Waiting for independent findings…</div>}
                    </div>
                  </article>
                ))}
              </div>

              {running && rounds.length === 0 && <div className="stream-loading"><LoaderCircle size={15} className="spin" />Opening a local review stream…</div>}
              {stopped && <div className="stop-banner"><CircleStop size={15} /><span><strong>{labels[stopped.reason] ?? "Debate stopped"}</strong><small>{stopDescription(stopped.reason)}</small></span><span className="stop-tag">{stopped.reason.replace("_", " ")}</span></div>}
            </section>

            <aside className="verdict-column">
              <section className={`panel verdict-panel ${final ? "has-result" : ""}`}>
                <div className="panel-heading verdict-panel-heading">
                  <div className="panel-title"><span className="panel-icon green"><BadgeCheck size={16} /></span><span><strong>Final ruling</strong><small>Weighted panel + independent judge</small></span></div>
                  {final && <span className={`verdict-status ${["issue", "false"].includes(final.verdict.overall) ? "status-issue" : "status-clean"}`}>{final.verdict.overall.replace("_", " ")}</span>}
                </div>

                {!final ? <div className="verdict-empty"><div className="verdict-lock"><ShieldCheck size={20} /></div><strong>The ruling appears after the debate</strong><p>Findings, dissent, and any judge override will be collected here.</p><div className="verdict-skeleton"><i /><i /><i /></div></div> : (
                  <div className="verdict-content">
                    <div className="verdict-summary"><span className="summary-label">COUNCIL SUMMARY</span><p>{final.verdict.summary}</p></div>
                    {final.recommendation && <div className="verdict-summary"><span className="summary-label">RECOMMENDATION · {fmt(final.recommendation.confidence)} CONFIDENCE</span><p>{final.recommendation.text}</p></div>}
                    {ruling?.overrides_vote && <div className="override-callout"><TriangleAlert size={15} /><span><strong>Judge overrode the weighted vote</strong><small>{ruling.rulings.find((item) => item.overrides_vote)?.reason || ruling.reason || "The judge recorded a reason for the override."}</small></span></div>}
                    {ruling?.injection_flags?.length ? <div className="injection-callout"><ShieldAlert size={14} /><span>Input steering detected: {ruling.injection_flags.join(", ")}</span></div> : null}
                    <ClusterList title="Confirmed findings" clusters={final.confirmed_findings} tone="confirmed" />
                    <ClusterList title="Disputed findings" clusters={final.disputed_findings} tone="disputed" />
                    {final.clashes?.map((clash) => <div className="dissent-section" key={clash.cluster_id}><div className="section-title">Clash</div><strong>{clash.claim}</strong><div className="dissent-row"><span className="alias-avatar small">+</span><span><strong>{clash.for_side.aliases.join(", ")}</strong><small>{clash.for_side.argument}</small></span></div><div className="dissent-row"><span className="alias-avatar small">−</span><span><strong>{clash.against_side.aliases.join(", ")}</strong><small>{clash.against_side.argument}</small></span></div></div>)}
                    {final.blind_spots?.length ? <div className="dissent-section"><div className="section-title">Blind spots <span>{final.blind_spots.length}</span></div>{final.blind_spots.map((item, index) => <div className="dissent-row" key={`${item.alias}-${index}`}><span className="alias-avatar small">{item.alias}</span><span><strong>{item.kind.replaceAll("_", " ")}</strong><small>{item.text}</small></span></div>)}</div> : null}
                    {final.dissent.length > 0 && <div className="dissent-section"><div className="section-title">Dissent <span>{final.dissent.length}</span></div>{final.dissent.map((item, index) => <div className="dissent-row" key={`${item.model_id}-${index}`}><span className="alias-avatar small">{item.alias}</span><span><strong>{item.alias} · {item.verdict.replace("_", " ")}</strong><small>{item.reason || "Held a different position."}</small></span></div>)}</div>}
                    {final.confirmed_findings.length === 0 && final.disputed_findings.length === 0 && <div className="clean-result"><ShieldCheck size={16} /><span>No actionable findings were confirmed.</span></div>}
                    {final.next_step && <div className="verdict-summary"><span className="summary-label">NEXT STEP</span><p>{final.next_step.text}</p>{final.next_step.patch && <pre className="code-patch"><code>{final.next_step.patch}</code></pre>}</div>}
                    <div className="judge-row"><span className="judge-glyph"><Sparkles size={13} /></span><span><strong>Judge</strong><small>{ruling?.judge_model_id ?? started?.judge_model_id ?? "Awaiting ruling"}</small></span><span className="judge-decision">{ruling?.skipped ? "Skipped · early finish" : <><Check size={13} />{ruling?.overrides_vote ? "Override" : "Reviewed"}</>}</span></div>
                    {apiMode === "live" && final.run_id && <div className="export-row"><button className="secondary-button" onClick={() => { setExportError(""); void downloadRunExport(final.run_id, "md").catch((caught: unknown) => setExportError(caught instanceof Error ? caught.message : "Export failed.")); }}><Download size={13} />Download Markdown</button><button className="secondary-button" onClick={() => { setExportError(""); void downloadRunExport(final.run_id, "html").catch((caught: unknown) => setExportError(caught instanceof Error ? caught.message : "Export failed.")); }}><Download size={13} />HTML report</button>{exportError && <small>{exportError}</small>}</div>}
                  </div>
                )}
              </section>

              <section className="panel panelist-panel">
                <div className="panel-heading compact-heading"><div className="panel-title"><span className="panel-icon purple"><UsersRound size={15} /></span><span><strong>Review panel</strong><small>{started ? `${started.model_ids.length} models · aliases hide identity` : "Anonymized during debate"}</small></span></div><span className="blind-chip"><ShieldCheck size={12} />Blind</span></div>
                <div className="panelist-list">
                  {panelists(started).map((model) => <div className="panelist-row" key={model.id}><span className={`alias-avatar alias-${model.alias.toLowerCase()}`}>{model.alias}</span><span className="panelist-name"><strong>{model.id}</strong><small>{model.id === started?.judge_model_id ? "Also serving as judge" : "Debater · identity hidden"}</small></span><span className={`model-live ${running ? "is-running" : ""}`} />{started?.judge_model_id === model.id && <span className="judge-mini">JUDGE</span>}</div>)}
                  {!started && <div className="panelist-placeholder"><span className="alias-avatar alias-a">A</span><span className="alias-avatar alias-b">B</span><span className="alias-avatar alias-c">C</span><span>Aliases stay hidden from the panelists</span></div>}
                </div>
              </section>

              <div className="trust-note"><ShieldCheck size={14} /><span>Weights are hidden from debaters; the judge sees the weighted tally.</span></div>
            </aside>
          </div>

          <footer className="page-footer"><span><span className="status-dot" /> Local-first review</span><span><Clock3 size={13} />{final?.usage ? `${final.usage.calls} model calls · ${final.usage.elapsed_ms} ms recorded` : apiMode === "mock" ? "Recorded fixture · no model calls" : healthOk ? "Live model review" : "Waiting for backend"}</span><span>Quorum preview <span className="footer-dot">·</span> {apiMode === "mock" ? `${fixtures.length} scenarios` : `${packs.length} packs · ${models.length} models`}</span></footer>
          </> : null}
          <div style={{ display: page === "models" ? "contents" : "none" }}><ModelManagerPage models={models} packs={packs} apiMode={apiMode} onModelsChanged={() => setReloadKey((key) => key + 1)} onViewProfiles={() => setPage("profiles")} /></div>
          <div style={{ display: page === "profiles" ? "contents" : "none" }}><ProfilesPage packs={packs} models={models} defaultPack={selectedPack} apiMode={apiMode} active={page === "profiles"} /></div>
        </div>
      </main>
    </div>
  );
}

type LiveSetupProps = {
  packs: PackInfo[];
  models: ModelInfo[];
  availableModels: ModelInfo[];
  selectedPack: string;
  selectedCategory: string;
  selectedModels: string[];
  judgeModelId: string;
  maxRounds: number;
  escalate: "always" | "auto";
  frameEnabled: boolean;
  loading: boolean;
  onPackChange: (id: string) => void;
  onCategoryChange: (category: string) => void;
  onToggleModel: (id: string) => void;
  onJudgeChange: (id: string) => void;
  onRoundsChange: (rounds: number) => void;
  onEscalateChange: (value: "always" | "auto") => void;
  onFrameChange: (enabled: boolean) => void;
};

function LiveSetup({
  packs, models, availableModels, selectedPack, selectedCategory, selectedModels, judgeModelId,
  maxRounds, escalate, frameEnabled, loading, onPackChange, onCategoryChange, onToggleModel, onJudgeChange, onRoundsChange, onEscalateChange, onFrameChange,
}: LiveSetupProps) {
  const pack = packs.find((item) => item.id === selectedPack);
  const visibleModels = models;
  const selectedFamilyCount = new Set(availableModels.filter((model) => selectedModels.includes(model.id)).map((model) => model.family || model.id)).size;
  return (
    <section className="panel live-setup-panel" aria-label="Live review configuration">
      <div className="panel-heading setup-heading"><div className="panel-title"><span className="panel-icon purple"><UsersRound size={15} /></span><span><strong>Review configuration</strong><small>Choose the pack, panel, and round budget</small></span></div><span className="selection-count">{selectedModels.length} / 6 selected</span></div>
      <div className="setup-controls">
        <label className="setup-field"><span>Review pack</span><select value={selectedPack} onChange={(event) => onPackChange(event.target.value)} disabled={loading || !packs.length}><option value="">Choose a pack</option>{packs.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label className="setup-field"><span>Category hint <small>optional</small></span><select value={selectedCategory} onChange={(event) => onCategoryChange(event.target.value)} disabled={!pack}><option value="">Let the pack choose</option>{pack?.categories.map((category) => <option key={category} value={category}>{category.replaceAll("_", " ")}</option>)}</select></label>
        <label className="setup-field"><span>Visible rounds</span><select value={maxRounds} onChange={(event) => onRoundsChange(Number(event.target.value))}><option value={0}>Blind review only</option><option value={1}>1 peer review round</option><option value={2}>2 peer review rounds</option></select></label>
        <label className="setup-field"><span>Judge <small>optional</small></span><select value={judgeModelId} onChange={(event) => onJudgeChange(event.target.value)} disabled={!availableModels.length}><option value="">Use backend default</option>{availableModels.map((model) => <option key={model.id} value={model.id}>{model.tag || model.id}</option>)}</select></label>
        <label className="setup-field"><span>Escalation</span><select value={escalate} onChange={(event) => onEscalateChange(event.target.value as "always" | "auto")}><option value="always">Full debate</option><option value="auto">Escalate on disagreement</option></select></label>
        <label className="setup-field"><span>Neutral framing</span><select value={String(frameEnabled)} onChange={(event) => onFrameChange(event.target.value === "true")}><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
      </div>
      <div className="model-select-heading"><span>Review panel</span><small>Choose 1–6 registered models. Weights stay hidden from debaters.</small></div>
      {loading ? <div className="model-empty"><LoaderCircle className="spin" size={15} />Loading registered models…</div> : visibleModels.length === 0 ? <div className="model-empty"><UsersRound size={16} />No available models. Add one through the backend CLI, then refresh.</div> : (
        <div className="model-grid">
          {visibleModels.map((model) => {
            const simulated = model.provider === "fake";
            const selectable = !simulated && canUseForReview(model);
            const selected = selectedModels.includes(model.id);
            return <label className={`model-option ${selected ? "selected" : ""} ${!selectable ? "unavailable" : ""}`} key={model.id}>
              <input type="checkbox" checked={selected} disabled={!selectable || (!selected && selectedModels.length >= 6)} onChange={() => onToggleModel(model.id)} />
              <span className="model-check">{selected && <Check size={11} />}</span>
              <span className="model-option-copy"><strong>{model.tag || model.id}</strong><small>{model.family || model.provider} · {model.license || "license unknown"} · {model.status}</small></span>
              <span className={`model-state ${simulated ? "simulated" : !model.open_weight ? "closed" : model.status}`}>{simulated ? "SIMULATED" : !model.open_weight ? "CLOSED" : "OPEN WEIGHT"}</span><span className="locality-tag">{model.locality ?? "locality unknown"}</span>
            </label>;
          })}
        </div>
      )}
      {models.some((model) => model.provider === "fake") && <div className="simulation-note"><ShieldAlert size={13} />Fake providers are shown for development but cannot be selected as real model reviewers.</div>}
      {models.some((model) => !model.open_weight && model.provider !== "fake") && <div className="license-note"><ShieldAlert size={13} />Closed-weight registrations are visible for transparency but excluded from this open-weight demo.</div>}
      {selectedModels.length > 1 && selectedFamilyCount < selectedModels.length && <div className="license-note"><TriangleAlert size={13} />Multiple reviewers share a model family; correlated errors may reduce the value of the vote.</div>}
      <div className="setup-footnote"><ShieldCheck size={13} /><span>Debaters see aliases only. The judge receives the weighted tally.</span></div>
    </section>
  );
}

function diverseModelSelection(models: ModelInfo[]) {
  const selected: ModelInfo[] = [];
  const families = new Set<string>();
  for (const model of models) {
    const family = model.family || model.id;
    if (families.has(family)) continue;
    selected.push(model);
    families.add(family);
    if (selected.length === 3) break;
  }
  if (selected.length < 3) {
    for (const model of models) {
      if (selected.some((item) => item.id === model.id)) continue;
      selected.push(model);
      if (selected.length === 3) break;
    }
  }
  return selected.map((model) => model.id);
}

function canUseForReview(model: ModelInfo) {
  return model.provider !== "fake" && model.open_weight && (model.status === "ready" || model.status === "registered") && model.installed !== false;
}

function MessageCard({ message, aliases }: { message: ModelMessage; aliases: Record<string, string> }) {
  const alias = message.alias || aliases[message.model_id] || "?";
  const failed = Boolean(message.error);
  return (
    <div className={`message-card ${failed ? "message-failed" : ""}`}>
      <span className={`alias-avatar alias-${alias.toLowerCase()}`}>{alias}</span>
      <div className="message-body">
        <div className="message-meta"><strong>Reviewer {alias}</strong><span className="message-verdict">{message.position?.verdict?.replace("_", " ") ?? "unsure"}</span><span className="confidence">{fmt(message.position?.confidence ?? 0)} confidence</span>{message.changed && <span className="flip-badge"><ArrowDown size={11} />Changed</span>}</div>
        <p>{failed ? message.error : message.text || (message.findings.length ? `${message.findings.length} finding(s) reported.` : "No issue found.")}</p>
        {message.changed && message.changed_reason && <div className="change-reason">Changed position: {message.changed_reason}</div>}
        {message.critique && (message.critique.strongest || message.critique.biggest_blind_spot || message.critique.missed_by_all) && <div className="critique-card"><strong>Peer critique</strong>{message.critique.strongest && <small><b>Strongest:</b> {message.critique.strongest}</small>}{message.critique.biggest_blind_spot && <small><b>Blind spot:</b> {message.critique.biggest_blind_spot}</small>}{message.critique.missed_by_all && <small><b>Missed by all:</b> {message.critique.missed_by_all}</small>}</div>}
        {message.findings?.map((finding, index) => <div className="finding-card" key={`${finding.id ?? finding.category}-${index}`}><div className="finding-top"><span className={`severity severity-${finding.severity}`}>{finding.severity}</span><span className="category-chip">{finding.category.replaceAll("_", " ")}</span><span className="line-reference">L{finding.line_start}{finding.line_end !== finding.line_start ? `–${finding.line_end}` : ""}</span></div><strong>{finding.claim}</strong><small>{finding.evidence}</small></div>)}
      </div>
    </div>
  );
}

function ClusterList({ title, clusters, tone }: { title: string; clusters: Cluster[]; tone: "confirmed" | "disputed" }) {
  if (!clusters.length) return null;
  return <div className={`cluster-section ${tone}`}><div className="section-title">{title}<span>{clusters.length}</span></div>{clusters.map((cluster) => <div className="cluster-card" key={cluster.id}><div className="cluster-card-top"><span className={`severity severity-${cluster.severity}`}>{cluster.severity}</span><span className="category-chip">{cluster.category || cluster.verdict || "verdict"}</span><span className="line-reference">{cluster.line_start ? `L${cluster.line_start}${cluster.line_end !== cluster.line_start ? `–${cluster.line_end}` : ""}` : ""}</span></div><strong>{cluster.claim}</strong><p>{cluster.evidence}</p><div className="support-row"><span>{cluster.supporters.length} supporter{cluster.supporters.length === 1 ? "" : "s"}</span><span className="weight-track"><i style={{ width: `${Math.min(cluster.support_share * 100, 100)}%` }} /></span><strong>{fmt(cluster.support_share)}</strong></div><div className="supporter-avatars">{cluster.supporters.map((supporter, index) => <span className={`alias-avatar tiny alias-${supporter.alias.toLowerCase()}`} title={`${supporter.alias}: ${fmt(supporter.weight)} weight (${supporter.weight_source})`} key={`${supporter.model_id}-${index}`}>{supporter.alias}</span>)}<small>weighted support</small>{cluster.tool_verified && <span className="tool-verified"><Check size={11} />Tool verified</span>}</div></div>)}</div>;
}

function panelists(started?: RunStarted) {
  if (!started) return [];
  return started.model_ids.map((id) => ({ id, alias: started.aliases[id] ?? "?" })).sort((a, b) => a.alias.localeCompare(b.alias));
}

function scenarioName(file: string) {
  return file.replace(/\.jsonl$/, "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stopDescription(reason: string) {
  if (reason === "consensus") return "Panel positions converged; the run ended early.";
  if (reason === "stalemate") return "The panel stopped changing before reaching consensus.";
  if (reason === "budget") return "The run reached its configured resource budget.";
  return "The configured round limit was reached.";
}

function ModelManagerPage({ models, packs, apiMode, onModelsChanged, onViewProfiles }: { models: ModelInfo[]; packs: PackInfo[]; apiMode: "mock" | "live"; onModelsChanged: () => void; onViewProfiles: () => void }) {
  const [provider, setProvider] = useState<ModelCreate["provider"]>("ollama");
  const [tag, setTag] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [family, setFamily] = useState("");
  const [license, setLicense] = useState("");
  const [openWeight, setOpenWeight] = useState(false);
  const [locality, setLocality] = useState<"local" | "hosted">("local");
  const [calibrate, setCalibrate] = useState(true);
  const [selectedPacks, setSelectedPacks] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [job, setJob] = useState<{ id: string; done: number; total: number; status: "running" | "done" | "error"; message: string } | null>(null);
  const stopJob = useRef<(() => void) | null>(null);

  useEffect(() => () => stopJob.current?.(), []);
  const usingHostedProvider = provider === "digitalocean" || locality === "hosted";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (apiMode !== "live" || busy || !tag.trim()) return;
    setBusy(true); setError(""); setMessage(""); setJob(null);
    try {
      const payload: ModelCreate = {
        provider,
        tag: tag.trim(),
        ...(provider === "openai_compat" && baseUrl.trim() ? { base_url: baseUrl.trim() } : {}),
        ...(apiKeyEnv.trim() ? { api_key_env: apiKeyEnv.trim() } : {}),
        ...(family.trim() ? { family: family.trim() } : {}),
        ...(license.trim() ? { license: license.trim() } : {}),
        open_weight: openWeight,
        ...(provider !== "ollama" ? { locality: provider === "digitalocean" ? "hosted" : locality } : {}),
        calibrate,
        ...(calibrate && selectedPacks.length ? { packs: selectedPacks } : {}),
      };
      const created = await addModel(payload);
      setTag("");
      onModelsChanged();
      if (!created.job_id) {
        setMessage(`${created.model.tag} was registered. Calibration was not started.`);
        setBusy(false);
        return;
      }
      setJob({ id: created.job_id, done: 0, total: 0, status: "running", message: "Calibration queued" });
      stopJob.current?.();
      stopJob.current = subscribeToJob(created.job_id, (event) => {
        const data = asRecord(event.data);
        if (event.event === "job_progress") {
          setJob((current) => current ? { ...current, done: Number(data.done ?? 0), total: Number(data.total ?? 0), message: String(data.message ?? "Calibrating…") } : current);
        } else if (event.event === "job_done") {
          setJob((current) => current ? { ...current, status: "done", message: "Calibration finished. The model profile is available." } : current);
          setMessage(`${created.model.tag} was registered and calibration finished.`);
          setBusy(false); onModelsChanged();
        } else if (event.event === "job_error") {
          const detail = String(data.message ?? "Calibration failed.");
          setJob((current) => current ? { ...current, status: "error", message: detail } : current);
          setError(`The model was registered, but calibration failed: ${detail}`);
          setBusy(false); onModelsChanged();
        }
      }, (caught) => {
        setError(caught.message); setBusy(false); onModelsChanged();
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to register this model.");
      setBusy(false);
    }
  }

  return <>
    <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />MODEL REGISTRY</div><h1>Model manager</h1><p>Register providers, review locality and license metadata, and calibrate models against a pack.</p></div><div className="heading-meta"><span className="meta-icon"><UsersRound size={15} /></span><span><strong>{models.length} registered</strong><small>Configured by this backend</small></span></div></section>
    {apiMode !== "live" && <div className="event-note event-warning">Model registration requires live mode. Start the backend and launch Vite with <code>VITE_API=live</code>.</div>}
    {error && <div className="error-banner page-error"><TriangleAlert size={16} /><span><strong>Model setup needs attention</strong><small>{error}</small></span></div>}
    {message && !error && <div className="event-note"><strong>Model setup updated</strong><p>{message}</p></div>}
    {job && <section className="panel job-panel"><div className="job-panel-header"><strong>Calibration · {job.id}</strong><span className={`job-status ${job.status}`}>{job.status}</span></div><p>{job.message}</p>{job.status === "running" && <><div className="job-progress-track"><i style={{ width: `${job.total ? Math.min(100, job.done / job.total * 100) : 5}%` }} /></div><small>{job.total ? `${job.done} / ${job.total} items` : "Waiting for calibration work"}</small></>}{job.status === "done" && <button className="text-button" onClick={onViewProfiles}>View skill profile <ArrowRight size={13} /></button>}</section>}
    <div className="manager-grid">
      <section className="panel manager-form-panel"><div className="panel-heading"><div className="panel-title"><span className="panel-icon purple"><UsersRound size={15} /></span><span><strong>Add a model</strong><small>Credentials stay in the backend environment</small></span></div></div>
        <form className="manager-form" onSubmit={(event) => { void submit(event); }}>
          <label className="setup-field"><span>Provider</span><select value={provider} onChange={(event) => { const next = event.target.value as ModelCreate["provider"]; setProvider(next); setLocality(next === "ollama" ? "local" : "hosted"); setOpenWeight(next === "ollama"); setApiKeyEnv(next === "digitalocean" ? "DIGITALOCEAN_MODEL_ACCESS_KEY" : ""); }}><option value="ollama">Ollama</option><option value="openai_compat">OpenAI-compatible API</option><option value="digitalocean">DigitalOcean inference</option></select></label>
          <label className="setup-field"><span>Model tag or deployment ID</span><input required maxLength={200} value={tag} onChange={(event) => setTag(event.target.value)} placeholder={provider === "ollama" ? "qwen2.5:14b" : "model-name"} /></label>
          {provider === "openai_compat" && <label className="setup-field"><span>Base URL</span><input required type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="http://127.0.0.1:1234/v1" /></label>}
          {provider !== "ollama" && <label className="setup-field"><span>API key environment variable <small>{provider === "digitalocean" ? "required in backend environment" : "optional"}</small></span><input required={provider === "digitalocean"} pattern="[A-Za-z_][A-Za-z0-9_]*" value={apiKeyEnv} onChange={(event) => setApiKeyEnv(event.target.value)} placeholder={provider === "digitalocean" ? "DIGITALOCEAN_MODEL_ACCESS_KEY" : "MODEL_API_KEY"} /><small className="field-help">Enter the environment variable name only. Never paste the key. Set its value in the backend process environment first.</small></label>}
          <div className="manager-form-row"><label className="setup-field"><span>Model family <small>optional</small></span><input value={family} onChange={(event) => setFamily(event.target.value)} placeholder="Qwen 2.5" /></label><label className="setup-field"><span>License <small>optional</small></span><input value={license} onChange={(event) => setLicense(event.target.value)} placeholder="Apache-2.0" /></label></div>
          <div className="manager-form-row">{provider !== "ollama" ? <label className="setup-field"><span>Locality</span><select value={provider === "digitalocean" ? "hosted" : locality} disabled={provider === "digitalocean"} onChange={(event) => setLocality(event.target.value as "local" | "hosted")}><option value="local">Local</option><option value="hosted">Hosted</option></select></label> : <div className="field-help locality-help">Ollama locality is inferred from the backend’s configured Ollama URL.</div>}<label className="check-field"><input type="checkbox" checked={openWeight} onChange={(event) => setOpenWeight(event.target.checked)} /><span>Open-weight model</span></label></div><small className="field-help">For API providers, mark open-weight only after checking the model’s license and weight availability.</small>
          <label className="check-field"><input type="checkbox" checked={calibrate} onChange={(event) => setCalibrate(event.target.checked)} /><span>Calibrate after registration</span></label>
          {calibrate && <label className="setup-field"><span>Calibration packs <small>choose none to use all</small></span><select multiple value={selectedPacks} onChange={(event) => setSelectedPacks(Array.from(event.currentTarget.selectedOptions, (option) => option.value))}>{packs.map((pack) => <option key={pack.id} value={pack.id}>{pack.name} · {pack.corpus_counts?.calibration ?? "?"} examples</option>)}</select></label>}
          {usingHostedProvider && <div className="hosted-note"><ShieldAlert size={14} /><span>This provider is hosted. Review inputs sent to this model leave this machine.</span></div>}
          <button className="primary-button manager-submit" type="submit" disabled={apiMode !== "live" || busy || !tag.trim()}>{busy ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{busy ? "Registering / calibrating…" : "Register model"}</button>
        </form>
      </section>
      <section className="panel registry-panel"><div className="panel-heading"><div className="panel-title"><span className="panel-icon"><Layers3 size={15} /></span><span><strong>Registered models</strong><small>Provider, locality, license, and registry state</small></span></div><button className="icon-button" aria-label="Refresh model registry" title="Refresh models" onClick={onModelsChanged}><RefreshCw size={14} /></button></div>
        <div className="registry-list">{models.length ? models.map((model) => <div className="registry-row" key={model.id}><div className="registry-model"><strong>{model.tag || model.id}</strong><small>{model.family || model.provider} · {model.provider} · {model.license || "license unknown"}</small>{model.status_detail && <small className="model-detail">{model.status_detail}</small>}</div><div className="registry-badges"><span className={`locality-tag ${model.locality}`}>{model.locality}</span><span className={`job-status ${model.status}`}>{model.status}</span><span className={`model-state ${model.open_weight ? "ready" : "closed"}`}>{model.open_weight ? "OPEN WEIGHT" : "CLOSED WEIGHT"}</span></div></div>) : <div className="model-empty">No models are registered yet.</div>}</div>
      </section>
    </div>
  </>;
}

function ProfilesPage({ packs, models, defaultPack, apiMode, active }: { packs: PackInfo[]; models: ModelInfo[]; defaultPack: string; apiMode: "mock" | "live"; active: boolean }) {
  const [packId, setPackId] = useState(defaultPack);
  const [response, setResponse] = useState<ProfilesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const pack = packs.find((item) => item.id === packId);
  useEffect(() => { if (defaultPack && !packs.some((item) => item.id === packId)) setPackId(defaultPack); }, [defaultPack, packId, packs]);
  useEffect(() => {
    let active = true;
    if (!active || apiMode !== "live" || !packId) { if (apiMode !== "live") setResponse(null); return () => { active = false; }; }
    setLoading(true); setError("");
    void getProfiles(packId).then((data) => { if (active) setResponse(data); }).catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "Unable to load model profiles."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [active, apiMode, packId, refreshKey]);
  const modelIds = [...new Set([...models.map((model) => model.id), ...Object.keys(response?.profiles ?? {})])];
  return <>
    <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />CALIBRATED SKILL</div><h1>Skill profiles</h1><p>Calibration accuracy and conservative vote weights, broken out by model and issue category.</p></div><label className="profile-pack-select"><span>Pack</span><select value={packId} onChange={(event) => setPackId(event.target.value)} disabled={!packs.length}>{packs.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></section>
    {apiMode !== "live" && <div className="event-note event-warning">Skill profiles require live mode and stored calibration results. Start the backend and launch Vite with <code>VITE_API=live</code>.</div>}
    {error && <div className="error-banner page-error"><TriangleAlert size={16} /><span><strong>Profiles unavailable</strong><small>{error}</small></span></div>}
    <section className="panel profiles-panel"><div className="panel-heading"><div className="panel-title"><span className="panel-icon green"><Gauge size={15} /></span><span><strong>{pack?.name ?? packId ?? "Model profiles"}</strong><small>{pack?.categories.length ?? 0} categories · default weight {response ? fmt(response.default_weight) : "—"}</small></span></div><button className="icon-button" aria-label="Reload profiles" title="Reload profiles" onClick={() => setRefreshKey((key) => key + 1)}><RefreshCw size={14} /></button></div>
      {loading ? <div className="model-empty"><LoaderCircle className="spin" size={15} />Loading calibration profiles…</div> : !pack?.categories.length ? <div className="model-empty">Select a pack to see its calibrated categories.</div> : <div className="profile-table-wrap"><table className="profile-table"><thead><tr><th>Model</th>{pack.categories.map((category) => <th key={category} title={pack.category_descriptions?.[category]}>{category.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{modelIds.map((modelId) => { const model = models.find((item) => item.id === modelId); const cells = response?.profiles[modelId]?.[packId] ?? {}; return <tr key={modelId}><th><strong>{model?.tag || modelId}</strong><small>{model?.family || model?.provider || "Stored profile"} · {model?.locality ?? "unknown locality"}</small></th>{pack.categories.map((category) => <ProfileTableCell key={category} cell={cells[category]} defaultWeight={response?.default_weight ?? 0} />)}</tr>; })}</tbody></table>{!loading && modelIds.length === 0 && <div className="model-empty">No calibration data for this pack yet. Register a model and run calibration in Model Manager.</div>}</div>}
    </section>
    <div className="profile-explainer"><ShieldCheck size={14} /><span><strong>How to read this:</strong> n is the number of calibration examples; rate is the observed correctness; the interval shown is the Wilson 95% lower bound used as the conservative weight. Small synthetic samples can produce unstable weights.</span></div>
  </>;
}

function ProfileTableCell({ cell, defaultWeight }: { cell?: ProfileCell; defaultWeight: number }) {
  if (!cell) return <td className="profile-cell profile-empty"><strong>{fmt(defaultWeight)}</strong><small>n 0 · default</small></td>;
  const hue = Math.round(cell.weight * 112);
  return <td className="profile-cell" style={{ backgroundColor: `hsl(${hue} 34% 95%)` }} title={`Weight ${fmt(cell.weight)} · observed rate ${fmt(cell.rate)} · Wilson 95% lower bound ${fmt(cell.lower)} · ${cell.correct}/${cell.n} correct`}><strong>{fmt(cell.weight)}</strong><small>n {cell.n} · rate {fmt(cell.rate)}</small><small>95% LB {fmt(cell.lower)}</small></td>;
}

export default App;
