import type { EventEnvelope, Fixture } from "./types";
import type { Health, ModelCreate, ModelCreated, ModelInfo, PackInfo, ProfilesResponse, RunCreated, RunRequest } from "./contracts";

const mockRoot = "/__mock";
const liveRoot = "http://127.0.0.1:8787";

async function request<T>(root: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${root}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { message?: string } | null;
    throw new Error(body?.message ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function loadFixtures(): Promise<Fixture[]> {
  return request<Fixture[]>(mockRoot, "/fixtures");
}

export async function getHealth(): Promise<Health> {
  return request<Health>(liveRoot, "/health");
}

export async function getPacks(): Promise<PackInfo[]> {
  return request<PackInfo[]>(liveRoot, "/packs");
}

export async function getModels(): Promise<ModelInfo[]> {
  return request<ModelInfo[]>(liveRoot, "/models");
}

export async function addModel(payload: ModelCreate): Promise<ModelCreated> {
  return request<ModelCreated>(liveRoot, "/models", { method: "POST", body: JSON.stringify(payload) });
}

export async function getProfiles(pack?: string): Promise<ProfilesResponse> {
  const query = pack ? `?pack=${encodeURIComponent(pack)}` : "";
  return request<ProfilesResponse>(liveRoot, `/profiles${query}`);
}

export function subscribeToJob(jobId: string, onEvent: (event: EventEnvelope) => void, onError: (error: Error) => void): () => void {
  const source = new EventSource(`${liveRoot}/jobs/${encodeURIComponent(jobId)}/events`);
  for (const name of ["job_progress", "job_done", "job_error"]) {
    source.addEventListener(name, (rawEvent) => {
      const event = rawEvent as MessageEvent<string>;
      onEvent({ seq: Number(event.lastEventId) || 0, event: name, data: JSON.parse(event.data) as Record<string, unknown> });
      if (name === "job_done" || name === "job_error") source.close();
    });
  }
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onError(new Error("The calibration job stream closed unexpectedly."));
  };
  return () => source.close();
}

export async function startFixtureRun(fixture: Fixture): Promise<string> {
  const result = await request<RunCreated>(mockRoot, "/runs", {
    method: "POST",
    body: JSON.stringify({ fixture: fixture.file }),
  });
  return result.run_id;
}

export async function startLiveRun(payload: RunRequest): Promise<string> {
  const result = await request<RunCreated>(liveRoot, "/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return result.run_id;
}

export function subscribeToRun(apiRoot: string, runId: string, onEvent: (event: EventEnvelope) => void, onError: (error: Error) => void): () => void {
  const suffix = `/runs/${encodeURIComponent(runId)}/events`;
  const source = new EventSource(`${apiRoot}${suffix}`);
  const names = [
    "run_started", "framed", "round_started", "model_message", "round_finished", "escalated", "finished_early", "stopped", "tally", "judge_ruling", "final", "error",
  ];
  for (const name of names) {
    source.addEventListener(name, (rawEvent) => {
      const event = rawEvent as MessageEvent<string>;
      onEvent({
        seq: Number(event.lastEventId) || 0,
        event: name,
        data: JSON.parse(event.data) as Record<string, unknown>,
      });
      if (name === "final" || name === "error") source.close();
    });
  }
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onError(new Error("The run stream closed before a final ruling arrived."));
  };
  return () => source.close();
}

export async function downloadRunExport(runId: string, format: "md" | "html"): Promise<void> {
  const response = await fetch(`${liveRoot}/runs/${encodeURIComponent(runId)}/export?format=${format}`);
  if (!response.ok) throw new Error(`Export failed (${response.status})`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `quorum-${runId}.${format}`;
  link.click();
  URL.revokeObjectURL(url);
}
