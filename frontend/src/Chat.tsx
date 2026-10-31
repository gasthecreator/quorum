import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LoaderCircle, MessageCirclePlus, MessageSquareText, SendHorizontal, ShieldAlert, Sparkles, TriangleAlert } from "lucide-react";
import { createChat, getChat, listChats, loadChatFixtures, MockChat, sendChatMessage, subscribeToChat, type ChatFixture, type ChatStreamStatus } from "./chatApi";
import { applyChatEvent, emptyChat, viewFromEvents, type ChatItem, type ChatView } from "./chatState";
import type { ChatListItem, ModelInfo } from "./contracts";
import type { EventEnvelope } from "./types";
import "./chat.css";

const PALETTE = ["#27794f", "#7558b8", "#b86a27", "#2a6fa8", "#a8326a", "#5b7f2a"];

type Props = { apiMode: "mock" | "live"; models: ModelInfo[]; active: boolean; onOpenModels: () => void };

function usable(model: ModelInfo) {
  return model.provider !== "fake" && (model.status === "ready" || model.status === "registered") && model.installed !== false;
}

function diverse(models: ModelInfo[], count = 3): string[] {
  const picked: ModelInfo[] = [];
  const families = new Set<string>();
  for (const model of models) {
    if (families.has(model.family || model.id)) continue;
    picked.push(model);
    families.add(model.family || model.id);
    if (picked.length === count) break;
  }
  for (const model of models) {
    if (picked.length >= count) break;
    if (!picked.some((item) => item.id === model.id)) picked.push(model);
  }
  return picked.map((model) => model.id);
}

export function Chat({ apiMode, models, active, onOpenModels }: Props) {
  const [chats, setChats] = useState<ChatListItem[]>([]);
  const [view, setView] = useState<ChatView>(emptyChat);
  const [stream, setStream] = useState<ChatStreamStatus>("closed");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [judgeId, setJudgeId] = useState("");
  const [rounds, setRounds] = useState(1);
  const [fixtures, setFixtures] = useState<ChatFixture[]>([]);
  const [fixtureFile, setFixtureFile] = useState("basic.jsonl");
  const [slow, setSlow] = useState(false);
  const closeStream = useRef<(() => void) | null>(null);
  const mock = useRef<MockChat | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const textarea = useRef<HTMLTextAreaElement | null>(null);

  const candidates = useMemo(() => models.filter(usable), [models]);
  const byId = useMemo(() => new Map(models.map((model) => [model.id, model])), [models]);
  const nameOf = useCallback((id: string) => byId.get(id)?.tag ?? id.replace(/^[a-z_]+:/, ""), [byId]);
  const colorOf = useCallback((id: string) => {
    const order = view.modelIds.length ? view.modelIds : selected;
    const index = order.indexOf(id);
    return PALETTE[(index < 0 ? Math.abs([...id].reduce((a, c) => a + c.charCodeAt(0), 0)) : index) % PALETTE.length];
  }, [view.modelIds, selected]);

  useEffect(() => {
    if (selected.length || !candidates.length) return;
    const picks = diverse(candidates);
    setSelected(picks);
    setJudgeId((current) => current || picks[0] || "");
  }, [candidates, selected.length]);

  useEffect(() => {
    if (apiMode === "mock") loadChatFixtures().then(setFixtures).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not load the chat fixtures."));
  }, [apiMode]);

  const refreshList = useCallback(() => {
    if (apiMode !== "live") return;
    listChats().then(setChats).catch(() => undefined);
  }, [apiMode]);
  useEffect(refreshList, [refreshList]);

  const onEvent = useCallback((env: EventEnvelope) => setView((current) => applyChatEvent(current, env)), []);

  const leave = useCallback(() => {
    closeStream.current?.();
    closeStream.current = null;
    mock.current?.stop();
    mock.current = null;
  }, []);
  useEffect(() => leave, [leave]);

  // refresh the sidebar when a turn ends, and keep the "this is slow" hint honest
  useEffect(() => { if (!view.replying && view.chatId) refreshList(); }, [view.replying, view.chatId, refreshList]);
  useEffect(() => {
    setSlow(false);
    if (!view.replying) return;
    const timer = setTimeout(() => setSlow(true), 25_000);
    return () => clearTimeout(timer);
  }, [view.replying, view.items.length]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [view.items, active]);

  function onScroll() {
    const el = scroller.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  function newChat() {
    leave();
    setView(emptyChat);
    setStream("closed");
    setError("");
    textarea.current?.focus();
  }

  async function openChat(id: string) {
    leave();
    setError("");
    try {
      const summary = await getChat(id);
      const next = viewFromEvents(summary.events);
      setView(next);
      stickToBottom.current = true;
      closeStream.current = subscribeToChat(id, next.lastSeq, onEvent, setStream);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not open that chat.");
    }
  }

  async function send() {
    const text = draft.trim();
    if (!text || sending || view.replying) return;
    setSending(true);
    setError("");
    stickToBottom.current = true;
    try {
      if (apiMode === "mock") {
        if (!mock.current) {
          const fixture = fixtures.find((item) => item.file === fixtureFile) ?? fixtures[0];
          if (!fixture) throw new Error("No chat fixtures are available.");
          setView(emptyChat);
          mock.current = new MockChat(fixture, onEvent);
          await mock.current.start();
          setStream("open");
        }
        mock.current.send(text);
      } else if (!view.chatId) {
        if (!selected.length) throw new Error("Pick at least one model to chat with.");
        const created = await createChat({ model_ids: selected, judge_model_id: judgeId || selected[0], rounds, message: text });
        setView({ ...emptyChat, chatId: created.chat_id });
        closeStream.current = subscribeToChat(created.chat_id, 0, onEvent, setStream);
        refreshList();
      } else {
        await sendChatMessage(view.chatId, text);
      }
      setDraft("");
      textarea.current?.focus();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not send the message.");
    } finally {
      setSending(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  }

  const started = Boolean(view.chatId) || Boolean(mock.current);
  const participants = view.modelIds.length ? view.modelIds : selected;
  const hosted = selected.filter((id) => byId.get(id)?.locality === "hosted");
  const busy = view.replying || sending;

  return (
    <section className="chat-page" aria-label="Group chat">
      <aside className="chat-list">
        <div className="chat-list-head">
          <strong>Group chats</strong>
          <button className="chat-new" onClick={newChat} title="Start a new chat"><MessageCirclePlus size={16} /><span>New</span></button>
        </div>
        {apiMode === "mock" ? <p className="chat-list-empty">Replaying recorded conversations. Type a message and the recorded models answer.</p>
          : chats.length === 0 ? <p className="chat-list-empty">No chats yet. Type a message to start one.</p>
          : <ul>{chats.map((chat) => (
            <li key={chat.chat_id}>
              <button className={`chat-list-item ${chat.chat_id === view.chatId ? "active" : ""}`} onClick={() => void openChat(chat.chat_id)}>
                <span className="chat-list-title">{chat.title}</span>
                <span className="chat-list-last">{chat.last_message || "No messages yet"}</span>
                <span className="chat-list-meta">{chat.turns} {chat.turns === 1 ? "message" : "messages"} · {chat.model_ids.length} models</span>
              </button>
            </li>
          ))}</ul>}
      </aside>

      <div className="chat-main">
        <header className="chat-head">
          <div>
            <h1>{view.title || "New group chat"}</h1>
            <p>{started ? `${participants.length} models and a judge${view.rounds ? ` · ${view.rounds} reaction ${view.rounds === 1 ? "round" : "rounds"}` : ""}` : "Everyone answers in the same thread"}</p>
          </div>
          <div className="chat-faces" aria-label="Participants">
            {participants.map((id) => <Avatar key={id} name={nameOf(id)} color={colorOf(id)} title={`${nameOf(id)}${view.aliases[id] ? ` (${view.aliases[id]})` : ""}`} />)}
            {started && <span className={`chat-stream chat-stream-${stream}`}>{stream === "open" ? "Live" : stream === "reconnecting" ? "Reconnecting…" : "Offline"}</span>}
          </div>
        </header>

        <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
          {view.items.length === 0 ? (
            <div className="chat-empty">
              <MessageSquareText size={34} />
              <h2>Start a conversation</h2>
              <p>Type one message. Every model answers here, reacts to the others, and the judge sums it up. Keep typing to continue the thread.</p>
            </div>
          ) : view.items.map((item) => <Row key={item.key} item={item} nameOf={nameOf} colorOf={colorOf} aliases={view.aliases} />)}
        </div>

        <footer className="chat-foot">
          {!started && apiMode === "mock" && (
            <div className="chat-panel">
              <label>Recorded conversation
                <select value={fixtureFile} onChange={(event) => setFixtureFile(event.target.value)}>
                  {fixtures.map((item) => <option key={item.file} value={item.file}>{item.file.replace(".jsonl", "").replace(/_/g, " ")}</option>)}
                </select>
              </label>
              <span className="chat-hint">{fixtures.find((item) => item.file === fixtureFile)?.description}</span>
            </div>
          )}
          {!started && apiMode === "live" && (
            <div className="chat-panel">
              {candidates.length === 0 ? (
                <span className="chat-hint">No models are ready. <button className="chat-link" onClick={onOpenModels}>Open the model manager</button> to add one.</span>
              ) : <>
                <div className="chat-chips" role="group" aria-label="Who is in this chat">
                  {candidates.map((model) => {
                    const on = selected.includes(model.id);
                    return (
                      <button key={model.id} className={`chat-chip ${on ? "on" : ""}`} aria-pressed={on}
                        onClick={() => setSelected((current) => on ? current.filter((id) => id !== model.id) : current.length < 6 ? [...current, model.id] : current)}>
                        <Avatar name={model.tag} color={on ? colorOf(model.id) : "#b8beb9"} size={18} />{model.tag}
                        <small>{model.locality}</small>
                      </button>
                    );
                  })}
                </div>
                <label>Judge
                  <select value={judgeId} onChange={(event) => setJudgeId(event.target.value)}>
                    {selected.map((id) => <option key={id} value={id}>{nameOf(id)}</option>)}
                  </select>
                </label>
                <label>Reactions
                  <select value={rounds} onChange={(event) => setRounds(Number(event.target.value))}>
                    <option value={0}>None: one reply each</option><option value={1}>1 round</option><option value={2}>2 rounds</option>
                  </select>
                </label>
              </>}
              {hosted.length > 0 && <span className="chat-hint chat-warn"><ShieldAlert size={13} /> {hosted.map(nameOf).join(", ")} runs on a hosted provider: your messages go to it.</span>}
            </div>
          )}
          {error && <div className="chat-error" role="alert"><TriangleAlert size={14} />{error}</div>}
          {slow && view.replying && <div className="chat-hint chat-slow">Local models answer one at a time, so a reply round can take a few minutes.</div>}
          <div className="chat-composer">
            <textarea ref={textarea} value={draft} rows={1} placeholder={view.replying ? "The panel is replying…" : "Message the group…"}
              onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown} disabled={view.replying} aria-label="Message the group" />
            <button className="chat-send" onClick={() => void send()} disabled={busy || !draft.trim() || (apiMode === "live" && !started && selected.length === 0)} aria-label="Send">
              {busy ? <LoaderCircle size={18} className="spin" /> : <SendHorizontal size={18} />}
            </button>
          </div>
        </footer>
      </div>
    </section>
  );
}

function Avatar({ name, color, size = 34, title }: { name: string; color: string; size?: number; title?: string }) {
  return <span className="chat-avatar" title={title ?? name} style={{ background: color, width: size, height: size, fontSize: Math.round(size * 0.42) }}>{name.charAt(0).toUpperCase()}</span>;
}

function Row({ item, nameOf, colorOf, aliases }: { item: ChatItem; nameOf: (id: string) => string; colorOf: (id: string) => string; aliases: Record<string, string> }) {
  if (item.kind === "user") {
    return <div className="chat-row chat-row-me"><div className="chat-bubble chat-bubble-me"><span className="chat-text">{item.text}</span></div></div>;
  }
  if (item.kind === "notice") {
    return <div className="chat-notice"><TriangleAlert size={13} />{item.text}</div>;
  }
  if (item.kind === "typing") {
    const judge = item.role === "judge";
    return (
      <div className="chat-row">
        <Avatar name={judge ? "J" : nameOf(item.modelId)} color={judge ? "#6b4fb3" : colorOf(item.modelId)} />
        <div className="chat-bubble chat-bubble-typing" aria-live="polite">
          <span className="chat-name" style={{ color: judge ? "#6b4fb3" : colorOf(item.modelId) }}>{judge ? "Judge" : nameOf(item.modelId)}</span>
          <span className="chat-dots"><i /><i /><i /></span><span className="chat-typing-label">{judge ? "summarising…" : "typing…"}</span>
        </div>
      </div>
    );
  }
  if (item.kind === "judge") {
    return (
      <div className="chat-judge">
        <div className="chat-judge-head"><Sparkles size={15} /><strong>Judge</strong><span>{nameOf(item.modelId)}</span></div>
        {item.error ? <p className="chat-error-line">The judge could not summarise this turn: {item.error}</p> : <>
          <p className="chat-judge-answer">{item.answer || item.text}</p>
          {(item.consensus || item.disagreement) && <dl>
            {item.consensus && <><dt>Agreed</dt><dd>{item.consensus}</dd></>}
            {item.disagreement && <><dt>Disagree</dt><dd>{item.disagreement}</dd></>}
          </dl>}
        </>}
      </div>
    );
  }
  const color = colorOf(item.modelId);
  if (item.error) {
    return <div className="chat-pass"><b style={{ color }}>{nameOf(item.modelId)}</b> couldn’t reply: {item.error}</div>;
  }
  if (item.passed) {
    return <div className="chat-pass"><b style={{ color }}>{nameOf(item.modelId)}</b> has nothing to add</div>;
  }
  return (
    <div className="chat-row">
      <Avatar name={nameOf(item.modelId)} color={color} />
      <div className="chat-bubble">
        <span className="chat-name" style={{ color }}>{nameOf(item.modelId)}<small title="Models refer to each other by letter">{aliases[item.modelId] ?? item.alias}</small>{item.round > 0 && <em>reacting</em>}</span>
        <span className="chat-text">{item.text}</span>
      </div>
    </div>
  );
}
