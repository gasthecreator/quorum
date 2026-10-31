import type { ChatCreate, ChatCreated, ChatListItem, ChatMessageAccepted, ChatSummary } from "./contracts";
import type { EventEnvelope } from "./types";

const liveRoot = "http://127.0.0.1:8787";
const mockRoot = "/__mock";

async function request<T>(root: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${root}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { message?: string } | null;
    throw new Error(body?.message ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export const createChat = (payload: ChatCreate) => request<ChatCreated>(liveRoot, "/chats", { method: "POST", body: JSON.stringify(payload) });
export const listChats = () => request<ChatListItem[]>(liveRoot, "/chats");
export const getChat = (id: string) => request<ChatSummary>(liveRoot, `/chats/${encodeURIComponent(id)}`);
export const sendChatMessage = (id: string, text: string) =>
  request<ChatMessageAccepted>(liveRoot, `/chats/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify({ text }) });

export const CHAT_EVENTS = ["chat_started", "user_message", "model_typing", "chat_message", "judge_message", "turn_finished", "error"];

export type ChatStreamStatus = "open" | "reconnecting" | "closed";

/**
 * A chat stream never ends by itself: keep it open while the chat is on screen. The browser reconnects
 * with Last-Event-ID, and the reducer ignores events it has already seen.
 */
export function subscribeToChat(
  chatId: string, after: number, onEvent: (event: EventEnvelope) => void, onStatus: (status: ChatStreamStatus) => void,
): () => void {
  const source = new EventSource(`${liveRoot}/chats/${encodeURIComponent(chatId)}/events?after=${after}`);
  source.onopen = () => onStatus("open");
  for (const name of CHAT_EVENTS) {
    source.addEventListener(name, (raw) => {
      const event = raw as MessageEvent<string>;
      onEvent({ seq: Number(event.lastEventId) || 0, event: name, data: JSON.parse(event.data) as Record<string, unknown> });
    });
  }
  source.onerror = () => onStatus(source.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
  return () => source.close();
}

// ---------------------------------------------------------------------------- mock mode (recorded fixtures)

export type ChatFixture = { file: string; description: string; messages: string[]; models: string[]; judge: string; rounds: number };
type FixtureFrame = { seq: number; t_ms: number; event: string; data: Record<string, unknown> };

export const loadChatFixtures = () => request<ChatFixture[]>(mockRoot, "/chat/fixtures");
const loadChatFixtureFrames = (file: string) => request<FixtureFrame[]>(mockRoot, `/chat/fixture?file=${encodeURIComponent(file)}`);

const MAX_GAP_MS = 1400;

/**
 * Replays one turn of a recorded chat fixture for the message the user typed. Turn n of the
 * conversation replays fixture turn n, looping back to the last one when the fixture is shorter.
 */
export class MockChat {
  private frames: FixtureFrame[] = [];
  private timers: ReturnType<typeof setTimeout>[] = [];
  private seq = 0;
  private turns = 0;
  readonly chatId: string;

  constructor(private readonly fixture: ChatFixture, private readonly onEvent: (event: EventEnvelope) => void) {
    this.chatId = `mock-chat-${fixture.file}`;
  }

  async start(): Promise<void> {
    this.frames = await loadChatFixtureFrames(this.fixture.file);
    const started = this.frames.find((f) => f.event === "chat_started");
    if (started) this.emit("chat_started", { ...started.data, chat_id: this.chatId, title: "" });
  }

  send(text: string): number {
    this.turns += 1;
    const total = Math.max(1, ...this.frames.map((f) => (typeof f.data.turn === "number" ? f.data.turn : 0)));
    const source = ((this.turns - 1) % total) + 1;
    const turnFrames = this.frames.filter((f) => f.event !== "chat_started" && f.data.turn === source);
    let at = 0;
    let previous = turnFrames[0]?.t_ms ?? 0;
    for (const frame of turnFrames) {
      at += Math.min(MAX_GAP_MS, Math.max(0, frame.t_ms - previous));
      previous = frame.t_ms;
      const data = { ...frame.data, turn: this.turns, ...(frame.event === "user_message" ? { text, message_id: `mock-${this.turns}` } : {}) };
      this.timers.push(setTimeout(() => this.emit(frame.event, data), at));
    }
    return this.turns;
  }

  stop() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }

  private emit(event: string, data: Record<string, unknown>) {
    this.seq += 1;
    this.onEvent({ seq: this.seq, event, data });
  }
}
