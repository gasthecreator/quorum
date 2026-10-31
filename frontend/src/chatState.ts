import type { EventEnvelope } from "./types";

export type ChatItem =
  | { kind: "user"; key: string; turn: number; text: string }
  | { kind: "model"; key: string; turn: number; round: number; modelId: string; alias: string; text: string; passed: boolean; error?: string | null }
  | { kind: "judge"; key: string; turn: number; modelId: string; text: string; consensus: string; disagreement: string; answer: string; error?: string | null }
  | { kind: "typing"; key: string; turn: number; round: number; modelId: string; alias: string; role: "panelist" | "judge" }
  | { kind: "notice"; key: string; turn: number; text: string };

export type ChatView = {
  chatId: string;
  title: string;
  modelIds: string[];
  aliases: Record<string, string>;
  judgeModelId: string;
  rounds: number;
  items: ChatItem[];
  replying: boolean;
  lastSeq: number;
  turns: number;
};

export const emptyChat: ChatView = {
  chatId: "", title: "", modelIds: [], aliases: {}, judgeModelId: "", rounds: 1, items: [], replying: false, lastSeq: 0, turns: 0,
};

const str = (value: unknown) => (typeof value === "string" ? value : "");
const num = (value: unknown) => (typeof value === "number" ? value : 0);

/** Fold one SSE event into the view. Pure, and idempotent per `seq`, so reconnect replays are harmless. */
export function applyChatEvent(view: ChatView, env: EventEnvelope): ChatView {
  if (env.seq > 0 && env.seq <= view.lastSeq) return view;
  const d = env.data as Record<string, unknown>;
  const next: ChatView = { ...view, lastSeq: Math.max(view.lastSeq, env.seq) };
  const turn = num(d.turn);

  switch (env.event) {
    case "chat_started":
      return {
        ...next,
        chatId: str(d.chat_id), title: str(d.title), modelIds: (d.model_ids as string[]) ?? [],
        aliases: (d.aliases as Record<string, string>) ?? {}, judgeModelId: str(d.judge_model_id), rounds: num(d.rounds),
      };
    case "user_message":
      return {
        ...next, replying: true, turns: Math.max(view.turns, turn),
        title: !view.title || view.title === "New chat" ? str(d.text).split("\n")[0].slice(0, 60) : view.title,
        items: [...view.items, { kind: "user", key: `user-${str(d.message_id)}`, turn, text: str(d.text) }],
      };
    case "model_typing": {
      const role = d.role === "judge" ? "judge" : "panelist";
      const key = `typing-${turn}-${num(d.round)}-${role}-${str(d.model_id)}`;
      if (view.items.some((item) => item.key === key)) return next;
      return { ...next, items: [...view.items, { kind: "typing", key, turn, round: num(d.round), modelId: str(d.model_id), alias: str(d.alias), role }] };
    }
    case "chat_message": {
      const modelId = str(d.model_id);
      const items = view.items.filter((item) => !(item.kind === "typing" && item.role === "panelist" && item.turn === turn && item.round === num(d.round) && item.modelId === modelId));
      items.push({
        kind: "model", key: `msg-${str(d.message_id)}`, turn, round: num(d.round), modelId, alias: str(d.alias),
        text: str(d.text), passed: d.passed === true, error: (d.error as string | null | undefined) ?? null,
      });
      return { ...next, items };
    }
    case "judge_message": {
      const items = view.items.filter((item) => !(item.kind === "typing" && item.role === "judge" && item.turn === turn));
      items.push({
        kind: "judge", key: `judge-${str(d.message_id)}`, turn, modelId: str(d.model_id), text: str(d.text),
        consensus: str(d.consensus), disagreement: str(d.disagreement), answer: str(d.answer), error: (d.error as string | null | undefined) ?? null,
      });
      return { ...next, items };
    }
    case "turn_finished":
      return { ...next, replying: false, items: view.items.filter((item) => !(item.kind === "typing" && item.turn === turn)) };
    case "error":
      return { ...next, items: [...view.items, { kind: "notice", key: `notice-${env.seq}-${view.items.length}`, turn: view.turns, text: `${str(d.code)}: ${str(d.message)}` }] };
    default:
      return next;
  }
}

export function viewFromEvents(events: EventEnvelope[]): ChatView {
  return events.reduce(applyChatEvent, emptyChat);
}

/** The thread as plain text, for tests and for copying. */
export function threadText(items: ChatItem[]): string {
  return items.map((item) => {
    if (item.kind === "user") return `You: ${item.text}`;
    if (item.kind === "model") return item.passed ? `${item.alias}: (nothing to add)` : `${item.alias}: ${item.text}`;
    if (item.kind === "judge") return `Judge: ${item.answer || item.text}`;
    if (item.kind === "typing") return `${item.alias} is typing`;
    return `! ${item.text}`;
  }).join("\n");
}
