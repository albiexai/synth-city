import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "../src/features/factory/pages/OverviewPage";
import { resetFactoryReads, useObservation, useObservations } from "../src/features/factory/reads";
import { actorFetch } from "../src/features/agent-mail/actorAuth";
import { ApiClient, apiClient } from "../src/lib/api";
import { PRODUCT_STORAGE_KEYS } from "../src/lib/browserStorageMigration";
import type { AgentTeamPreset, AgentTeamSummary } from "../src/types/agentTeams";
import overview from "./fixtures/factory/v1-p04/overview.json";
import { fixtureFetch, jsonResponse, renderRoute } from "./helpers/factory";

// #1173: team summaries are polled at most every 15 s while the tab is visible, paused while it is hidden,
// and refreshed at once, from a fresh read, after a write the user makes.
vi.mock("../src/features/agent-teams/WorkPublicationPanel", () => ({ WorkPublicationPanel: () => null }));

const summaryUrl = (id: number, query = "") => `agent-teams/presets/${id}/summary${query}`;
const flush = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

function stubServer() {
  // onWrite runs on the server when a write arrives, before the write is answered, like a real server.
  const state = { n: 1, hold: false, onWrite: undefined as (() => void) | undefined };
  const calls: { path: string; method: string }[] = [];
  const held: Array<() => void> = [];
  const fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input).replace(/^.*\/api\/v1\//, "");
    const method = init?.method ?? "GET";
    calls.push({ path, method });
    if (method !== "GET") {
      if (!path.includes("fail")) state.onWrite?.();
      return jsonResponse({ ok: true }, path.includes("fail") ? 500 : 200);
    }
    const value = state.n; // the data is fixed when the request is SENT, like a real server
    if (state.hold) await new Promise<void>((resolve) => held.push(resolve));
    return jsonResponse({ n: value });
  });
  vi.stubGlobal("fetch", fetch);
  return { state, calls, held, gets: (path?: string) => calls.filter((c) => c.method === "GET" && (!path || c.path === path)) };
}

function Summaries({ ids, query = "" }: { ids: number[]; query?: string }) {
  const { states } = useObservations<{ n: number }>(ids.map((id) => summaryUrl(id, query)));
  return <ul>{states.map((state, index) => <li key={ids[index]}>{`t${ids[index]}:${state.data?.n ?? "none"}`}</li>)}</ul>;
}
function Other() {
  const state = useObservation<{ n: number }>("agent-teams/presets");
  return <p>{`other:${state.data?.n ?? "none"}`}</p>;
}
function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetFactoryReads();
  setVisibility("visible");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("summary polling cadence", () => {
  it("reads each of 3 visible summaries at most 5 times in 60 s (the first read and one every 15 s)", async () => {
    const server = stubServer();
    render(<Summaries ids={[1, 2, 3]} />);
    await flush(60000);
    for (const id of [1, 2, 3]) {
      const count = server.gets(summaryUrl(id)).length;
      expect(count).toBeGreaterThanOrEqual(4);
      expect(count).toBeLessThanOrEqual(5);
    }
  });

  it("applies the same interval to a summary read with a filter query", async () => {
    const server = stubServer();
    render(<Summaries ids={[1]} query="?scope_id=3" />);
    await flush(60000);
    expect(server.gets(summaryUrl(1, "?scope_id=3")).length).toBeLessThanOrEqual(5);
  });

  it("keeps every other read at 5 s", async () => {
    const server = stubServer();
    render(<Other />);
    await flush(60000);
    expect(server.gets("agent-teams/presets").length).toBeGreaterThanOrEqual(12);
  });

  it("makes no summary request while the tab is hidden, and reads each team at once when it is visible again", async () => {
    setVisibility("hidden");
    const server = stubServer();
    render(<Summaries ids={[1, 2, 3]} />);
    await flush(60000);
    expect(server.gets()).toHaveLength(0);
    setVisibility("visible");
    await flush();
    expect(server.gets()).toHaveLength(3);
  });

  it("shows no data older than 15 s plus one request", async () => {
    const server = stubServer();
    render(<Summaries ids={[1]} />);
    await flush();
    expect(screen.getByText("t1:1")).toBeInTheDocument();
    server.state.n = 2;
    await flush(15000);
    expect(screen.getByText("t1:2")).toBeInTheDocument();
  });

  it("polls the 3 summaries of the Overview at most 5 times each in 60 s", async () => {
    const stamp = "2026-10-12T12:00:00Z";
    const presets: AgentTeamPreset[] = [1, 2, 3].map((id) => ({
      id, name: `Team ${id}`, created_at: stamp, updated_at: stamp, autonomy_enabled: true, slots: [],
    }));
    const summary = (id: number): AgentTeamSummary => ({
      preset_id: id, description: "", parent_issue_url: "", supervisor: "none", checked_at: stamp, valid_until: stamp,
      coverage_complete: true, hold_code: null, attention: [], human_actions: [],
      remaining_work: { state: "known", unfinished_items: 0, reported_items: 0, confidence: "unknown", reason: "complete" },
      next_actors: [], next_actor_coverage_complete: true,
      campaign: {
        backlog_state: "available", done_count: 0, latest_done: [], under_way_count: 0, under_way: [], queued_count: 0, queued: [],
        not_started_count: 0, not_started_groups: [], decisions_ahead: [],
      },
    });
    const { requests } = fixtureFetch((path) => {
      if (path === "factory/overview") return jsonResponse(overview.normal.response);
      if (path === "agent-teams/presets") return jsonResponse({ presets });
      const match = /^agent-teams\/presets\/(\d+)\/summary$/.exec(path);
      if (match) return jsonResponse(summary(Number(match[1])));
      return jsonResponse({ rows: [], items: [], repositories: [], total: 0, has_more: false, generated_at: stamp });
    });
    renderRoute(<OverviewPage />, "/", "/");
    await flush(60000);
    for (const id of [1, 2, 3]) {
      expect(requests.filter((r) => r.path === summaryUrl(id)).length).toBeLessThanOrEqual(5);
    }
  });
});

describe("a write the user makes refreshes the summaries at once", () => {
  it("starts a fresh read after a write that finishes while an older read is still in flight, and drops the older answer", async () => {
    const server = stubServer();
    server.state.hold = true;
    render(<Summaries ids={[1]} />);
    await flush();
    expect(server.gets(summaryUrl(1))).toHaveLength(1); // read A is waiting, and carries the old data (n = 1)

    server.state.onWrite = () => { server.state.n = 2; server.state.hold = false; }; // the write changes the data
    await new ApiClient().post("agent-teams/presets/1/leader", {});
    await flush();
    expect(server.gets(summaryUrl(1))).toHaveLength(2); // read B started after the write
    expect(screen.getByText("t1:2")).toBeInTheDocument();

    server.held.forEach((release) => release()); // read A answers late, with the old data
    await flush();
    expect(screen.queryByText("t1:1")).not.toBeInTheDocument();
    expect(screen.getByText("t1:2")).toBeInTheDocument();
    expect(server.gets(summaryUrl(1))).toHaveLength(2);
  });

  const writers: Array<[string, () => Promise<unknown>]> = [
    ["apiClient", () => apiClient("agent-teams/presets/1/hold", { method: "POST", body: "{}" })],
    ["the ApiClient class", () => new ApiClient().put("agent-teams/presets/1", {})],
    ["actorFetch", async () => {
      sessionStorage.setItem(PRODUCT_STORAGE_KEYS.actorToken, "synthetic-token");
      return actorFetch("external/agent-mail/messages", { method: "POST", body: "{}" });
    }],
  ];
  it.each(writers)("refreshes every live summary after a successful write through %s", async (_name, write) => {
    const server = stubServer();
    render(<Summaries ids={[1, 2]} />);
    await flush();
    expect(server.gets()).toHaveLength(2);
    server.state.onWrite = () => { server.state.n = 2; };
    await write();
    await flush();
    expect(server.gets()).toHaveLength(4);
    expect(screen.getByText("t1:2")).toBeInTheDocument();
  });

  it("starts no read after a write that fails, or after a read", async () => {
    const server = stubServer();
    render(<Summaries ids={[1]} />);
    await flush();
    await expect(apiClient("agent-teams/presets/fail", { method: "POST", body: "{}" })).rejects.toThrow();
    await apiClient("agent-teams/presets/1");
    await new ApiClient().get("agent-teams/presets/1");
    await flush();
    expect(server.gets(summaryUrl(1))).toHaveLength(1);
  });

  it("makes no request for a summary nobody shows", async () => {
    const server = stubServer();
    render(<Summaries ids={[1]} />);
    await flush();
    cleanup();
    await flush();
    await apiClient("agent-teams/presets/1/hold", { method: "POST", body: "{}" });
    await flush();
    expect(server.gets(summaryUrl(1))).toHaveLength(1);
  });
});

describe("writes that bypass apiClient", () => {
  const sources = import.meta.glob<string>("../src/**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });

  it("tell the summaries: every direct fetch outside lib/api.ts calls notifyApiWrite, except the plugin pages", () => {
    // Plugin pages change plugin data only. They show no team summary, and no team summary depends on them.
    const offenders = Object.entries(sources)
      .filter(([path]) => !/\.test\.tsx?$/.test(path) && !path.endsWith("/lib/api.ts") && !path.includes("/features/plugins/"))
      .filter(([, text]) => /\bfetch\(/.test(text) && !text.includes("notifyApiWrite"))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
