import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixtureDirectory = path.resolve(here, "../contracts/fixtures");
const fixtureIndexPath = path.join(fixtureDirectory, "index.json");

type FixtureIndexEntry = {
  file: string;
  description: string;
  terminal_event: string;
  stop_reason: string | null;
  request: { input: string; pack: string; max_rounds: number; models: string[]; judge: string; escalate?: "auto" | "always" };
};

function fixtureReplay(): Plugin {
  const runs = new Map<string, FixtureIndexEntry>();
  let nextId = 1;

  return {
    name: "quorum-fixture-replay",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const url = new URL(request.url ?? "/", "http://localhost");
        if (!url.pathname.startsWith("/__mock/")) return next();

        response.setHeader("Access-Control-Allow-Origin", "*");
        response.setHeader("Access-Control-Allow-Headers", "Content-Type");
        response.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
        if (request.method === "OPTIONS") {
          response.statusCode = 204;
          response.end();
          return;
        }

        if (url.pathname === "/__mock/fixtures" && request.method === "GET") {
          const entries = JSON.parse(await readFile(fixtureIndexPath, "utf8")) as FixtureIndexEntry[];
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify(entries));
          return;
        }

        const chatDirectory = path.join(fixtureDirectory, "chat");
        if (url.pathname === "/__mock/chat/fixtures" && request.method === "GET") {
          response.setHeader("Content-Type", "application/json");
          response.end(await readFile(path.join(chatDirectory, "index.json"), "utf8"));
          return;
        }
        if (url.pathname === "/__mock/chat/fixture" && request.method === "GET") {
          const file = path.basename(url.searchParams.get("file") ?? "");
          const lines = (await readFile(path.join(chatDirectory, file), "utf8")).split(/\r?\n/).filter(Boolean);
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify(lines.map((line) => JSON.parse(line))));
          return;
        }

        if (url.pathname === "/__mock/runs" && request.method === "POST") {
          let body = "";
          for await (const chunk of request) body += chunk;
          const payload = JSON.parse(body || "{}") as { fixture?: string };
          const entries = JSON.parse(await readFile(fixtureIndexPath, "utf8")) as FixtureIndexEntry[];
          const fixture = entries.find((entry) => entry.file === payload.fixture) ?? entries[0];
          const id = `mock-${nextId++}`;
          runs.set(id, fixture);
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ run_id: id }));
          return;
        }

        const eventMatch = url.pathname.match(/^\/__mock\/runs\/([^/]+)\/events$/);
        if (eventMatch && request.method === "GET") {
          const fixture = runs.get(eventMatch[1]);
          if (!fixture) {
            response.statusCode = 404;
            response.setHeader("Content-Type", "application/json");
            response.end(JSON.stringify({ code: "unknown_run", message: "Fixture run not found." }));
            return;
          }

          const lines = (await readFile(path.join(fixtureDirectory, fixture.file), "utf8"))
            .split(/\r?\n/)
            .filter(Boolean);
          response.statusCode = 200;
          response.setHeader("Content-Type", "text/event-stream");
          response.setHeader("Cache-Control", "no-cache");
          response.setHeader("Connection", "keep-alive");
          response.flushHeaders?.();

          let cursor = 0;
          let timer: ReturnType<typeof setTimeout> | undefined;
          const sendNext = () => {
            if (request.destroyed || response.destroyed || cursor >= lines.length) {
              response.end();
              runs.delete(eventMatch[1]);
              return;
            }
            const frame = JSON.parse(lines[cursor++]) as { seq: number; event: string; data: unknown; t_ms?: number };
            response.write(`id: ${frame.seq}\nevent: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`);
            const next = lines[cursor] ? JSON.parse(lines[cursor]) as { t_ms?: number } : undefined;
            const currentTime = (frame as { t_ms?: number }).t_ms;
            const delay = next?.t_ms === undefined || currentTime === undefined ? 500 : Math.max(0, next.t_ms - currentTime);
            timer = setTimeout(sendNext, delay);
          };
          response.on("close", () => {
            if (timer) clearTimeout(timer);
          });
          sendNext();
          return;
        }

        response.statusCode = 404;
        response.end();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), fixtureReplay()],
  server: { port: 5173, strictPort: true },
  preview: { port: 5173, strictPort: true },
});
