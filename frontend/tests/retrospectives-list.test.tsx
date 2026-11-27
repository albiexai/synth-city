import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { RetrospectivesList } from "../src/features/factory/RetrospectivesList";
import { resetFactoryReads } from "../src/features/factory/reads";
import type { CampaignListResponse, CampaignRow } from "../src/types/factory";
import { fixtureFetch, jsonResponse, settle } from "./helpers/factory";

// #1252: the Retrospectives list. It shows what the server recorded and never turns unknown into zero.
const NOW = new Date().toISOString().slice(0, 19);
const measure = (value: number | null, reason: string | null = null) => ({ value, reason });
const row = (over: Partial<CampaignRow>): CampaignRow => ({
  id: 1, team: { id: 1, name: "synth-city-v4.4" }, selector: "label:release:v4.4", status: "active", started_at: "2026-11-20T09:00:00",
  finished_at: null, merged: measure(41), in_scope: measure(63),
  repositories: [{ repo: "o/a1", issues: 58, reason: null }, { repo: "o/a2", issues: 5, reason: null }],
  repositories_unknown_reason: null, source: { kind: "live", as_of: NOW }, ...over,
});
const LIVE = row({});
const FROZEN = row({ id: 2, selector: "label:release:v4.3", status: "finished", started_at: "2026-11-17T09:00:00", finished_at: "2026-11-18T09:00:00",
  merged: measure(79), in_scope: measure(98), repositories: [{ repo: "o/a1", issues: 98, reason: null }], source: { kind: "frozen", as_of: "2026-11-18T10:00:00" } });
const NO_SNAPSHOT = row({ id: 3, team: { id: 2, name: "tizonia-2026" }, selector: "milestone:v0.22", status: "finished", finished_at: "2026-11-05T09:00:00",
  merged: measure(null, "no_snapshot"), in_scope: measure(null, "no_snapshot"), repositories: [], repositories_unknown_reason: "no_snapshot", source: null });
const RETIRED = row({ id: 4, team: null, selector: "label:old", status: "finished", finished_at: "2026-10-05T09:00:00",
  merged: measure(null, "before_recording"), in_scope: measure(null, "before_recording"), repositories: [], repositories_unknown_reason: "before_recording", source: null });
const REPOSITORIES = [
  { scope_id: 11, github: { owner: "o", name: "a1" }, team: { id: 1, name: "synth-city-v4.4" } },
  { scope_id: 12, github: { owner: "o", name: "a2" }, team: { id: 1, name: "synth-city-v4.4" } },
  { scope_id: 21, github: { owner: "t", name: "b1" }, team: { id: 2, name: "tizonia-2026" } },
];
const reply = (rows: CampaignRow[], over: Partial<CampaignListResponse> = {}): CampaignListResponse => ({
  schema_version: 1, generated_at: NOW, campaigns: rows, total: rows.length, counts: { live: rows.filter((r) => r.status !== "finished").length,
    finished: rows.filter((r) => r.status === "finished").length }, hidden_unknown_repositories: 0, finished_before_recording: 0, has_more: false, next_cursor: null, ...over,
});

let answer: (query: URLSearchParams) => Response = () => jsonResponse(reply([]));
function Probe() {
  const location = useLocation();
  return <div data-testid="url">{location.search}</div>;
}
function open(url = "/insights/retrospectives", body: (query: URLSearchParams) => Response = () => jsonResponse(reply([LIVE, FROZEN, NO_SNAPSHOT, RETIRED]))) {
  answer = body;
  const fixture = fixtureFetch((path, query) => {
    if (path === "factory/campaigns") return answer(query);
    if (path === "factory/repositories") return jsonResponse({ repositories: REPOSITORIES, has_more: false });
    return jsonResponse({});
  });
  render(<MemoryRouter initialEntries={[url]}><RetrospectivesList /><Probe /></MemoryRouter>);
  return fixture;
}
const search = () => screen.getByTestId("url").textContent;
const sent = (fixture: ReturnType<typeof open>) => fixture.requests.filter((r) => r.path === "factory/campaigns").at(-1)!.query;
const choose = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const table = () => screen.getByRole("table", { name: "Campaigns and their repositories" });

beforeEach(() => resetFactoryReads());
afterEach(() => { cleanup(); resetFactoryReads(); });

describe("the rows", () => {
  it("shows each campaign with its status, merged against scope and its repositories with their counts", async () => {
    open();
    await screen.findByRole("table");
    const live = within(table()).getByRole("link", { name: "release:v4.4" }).closest("tr")!;
    expect(live).toHaveTextContent("label");
    expect(live).toHaveTextContent("Live");
    expect(live).toHaveTextContent("41 / 63");
    expect(live).toHaveTextContent("o/a1 (58)");
    expect(live).toHaveTextContent("o/a2 (5)");
    const frozen = within(table()).getByRole("link", { name: "release:v4.3" }).closest("tr")!;
    expect(frozen).toHaveTextContent("Finished");
    expect(frozen).toHaveTextContent("79 / 98");
    expect(frozen).toHaveTextContent("o/a1 (98)");
  });

  it("says where the counts come from and how old they are", async () => {
    open();
    await screen.findByRole("table");
    expect(screen.getByText(/Counts as of .*, frozen /)).toBeInTheDocument();
    expect(screen.getAllByText(/Counts as of/)).toHaveLength(2);
  });

  it("links each campaign to its page", async () => {
    open();
    await screen.findByRole("table");
    expect(within(table()).getByRole("link", { name: "release:v4.4" })).toHaveAttribute("href", "/insights/retrospectives/1");
    expect(within(table()).getByRole("link", { name: "v0.22" })).toHaveAttribute("href", "/insights/retrospectives/3");
  });

  it("shows an unknown count as unknown with its reason, never as zero", async () => {
    open();
    await screen.findByRole("table");
    const none = within(table()).getByRole("link", { name: "v0.22" }).closest("tr")!;
    expect(none).toHaveTextContent("unknown · no snapshot recorded");
    expect(none).not.toHaveTextContent(/\b0\b/);
    expect(none.querySelector('[data-l="Merged / in scope"]')).toHaveTextContent("unknown · no snapshot recorded");
    const retired = within(table()).getByRole("link", { name: "old" }).closest("tr")!;
    expect(retired).toHaveTextContent("unknown · ran before recording began");
  });

  it("shows a repository whose count is unknown beside the ones that are known", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([row({ repositories: [{ repo: "o/a1", issues: 58, reason: null },
      { repo: "o/a2", issues: null, reason: "not_read_yet" }], merged: measure(null, "not_read_yet"), in_scope: measure(null, "not_read_yet") })])));
    await screen.findByRole("table");
    const live = within(table()).getByRole("link", { name: "release:v4.4" }).closest("tr")!;
    expect(live).toHaveTextContent("o/a1 (58)");
    expect(live).toHaveTextContent("o/a2 (unknown · not read yet)");
    expect(live).toHaveTextContent("unknown · not read yet");
  });
});

describe("the summary line", () => {
  it("counts campaigns, live and finished, and names the evidenced ones that ended before recording", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([LIVE, FROZEN], { finished_before_recording: 2 })));
    expect(await screen.findByRole("status")).toHaveTextContent("2 campaigns · 1 live · 1 finished (2 before recording began in 4.4)");
  });

  it("does not mention recording when no campaign has that evidence", async () => {
    open();
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("4 campaigns · 1 live · 3 finished");
    expect(status).not.toHaveTextContent("before recording began");
    expect(status).toHaveTextContent("unknown means the factory did not record it. It never means zero.");
  });

  it("says that a repository filter hides campaigns with unknown repositories, in the singular and the plural", async () => {
    open("/insights/retrospectives?scope_id=11", () => jsonResponse(reply([LIVE], { hidden_unknown_repositories: 1 })));
    expect(await screen.findByText(/1 campaign has unknown repositories, so the repository filter does not show it\./)).toBeInTheDocument();
    cleanup(); resetFactoryReads();
    open("/insights/retrospectives?scope_id=11", () => jsonResponse(reply([LIVE], { hidden_unknown_repositories: 2 })));
    expect(await screen.findByText(/2 campaigns have unknown repositories, so the repository filter does not show them\./)).toBeInTheDocument();
  });

});

describe("the filters and the URL", () => {
  it("asks for live and finished campaigns by default, with every filter at its default", async () => {
    const fixture = open();
    await screen.findByRole("table");
    expect(sent(fixture).get("status")).toBe("all");
    expect(sent(fixture).get("limit")).toBe("100");
    expect(sent(fixture).get("team_id")).toBeNull();
    expect(screen.getByLabelText("Status")).toHaveValue("all");
    expect(screen.getByLabelText("Group by")).toHaveValue("team");
    expect(screen.getByLabelText("Repository")).toHaveValue("");
    expect(within(screen.getByLabelText("Repository")).getByRole("option", { name: "All repositories (3)" })).toBeInTheDocument();
    expect(within(screen.getByLabelText("Team")).getByRole("option", { name: "All teams (2)" })).toBeInTheDocument();
  });

  it("sends the status, and keeps it in the URL", async () => {
    const fixture = open();
    await screen.findByRole("table");
    choose("Status", "live");
    await waitFor(() => expect(sent(fixture).get("status")).toBe("live"));
    expect(search()).toBe("?status=live");
    choose("Status", "all");
    await waitFor(() => expect(search()).toBe(""));
  });

  it("sends a team, and a repository replaces the team because a repository spans teams", async () => {
    const fixture = open();
    await screen.findByRole("table");
    choose("Team", "2");
    await waitFor(() => expect(sent(fixture).get("team_id")).toBe("2"));
    expect(search()).toBe("?team_id=2");
    choose("Repository", "11");
    await waitFor(() => expect(sent(fixture).get("scope_id")).toBe("11"));
    expect(sent(fixture).get("team_id")).toBeNull();
    expect(search()).toBe("?scope_id=11");
    choose("Team", "1");
    await waitFor(() => expect(sent(fixture).get("team_id")).toBe("1"));
    expect(sent(fixture).get("scope_id")).toBeNull();
  });

  it("opens from a URL with its filters and ignores a status or grouping that it does not know", async () => {
    const fixture = open("/insights/retrospectives?scope_id=11&status=live&group=repository");
    await screen.findByRole("table");
    expect(screen.getByLabelText("Repository")).toHaveValue("11");
    expect(screen.getByLabelText("Status")).toHaveValue("live");
    expect(screen.getByLabelText("Group by")).toHaveValue("repository");
    expect(sent(fixture).get("scope_id")).toBe("11");
    cleanup(); resetFactoryReads();
    const other = open("/insights/retrospectives?status=bogus&group=bogus");
    await screen.findByRole("table");
    expect(sent(other).get("status")).toBe("all");
    expect(screen.getByLabelText("Group by")).toHaveValue("team");
  });

  it("drops a repository that no team watches before it reaches the server", async () => {
    const fixture = open("/insights/retrospectives?scope_id=999");
    await screen.findByRole("table");
    expect(sent(fixture).get("scope_id")).toBeNull();
  });
});

describe("grouping", () => {
  const headings = () => within(table()).getAllByRole("rowheader", { hidden: true }).length;
  const groupHeads = () => within(table()).queryAllByRole("columnheader").map((h) => h.textContent).filter((t) => t && !/^(Campaign|Status|Merged|Repositories \(issues\))/.test(t));

  it("groups by team by default, with a name for a retired team", async () => {
    open();
    await screen.findByRole("table");
    expect(groupHeads()).toEqual(["Team synth-city-v4.4", "Team tizonia-2026", "Retired team"]);
    expect(headings()).toBe(4);
  });

  it("groups by repository: a campaign in two repositories is under both, and unknown ones have their own group", async () => {
    open("/insights/retrospectives?group=repository");
    await screen.findByRole("table");
    expect(groupHeads()).toEqual(["o/a1", "o/a2", "Repositories unknown"]);
    expect(within(table()).getAllByRole("link", { name: "release:v4.4" })).toHaveLength(2);
    expect(within(table()).getAllByRole("link", { name: "release:v4.3" })).toHaveLength(1);
  });

  it("shows no group headings without grouping", async () => {
    open("/insights/retrospectives?group=none");
    await screen.findByRole("table");
    expect(groupHeads()).toEqual([]);
    expect(headings()).toBe(4);
  });
});

describe("empty, loading and failed reads", () => {
  it("says that there are no campaigns yet, and that no campaign matches a filter", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([])));
    expect(await screen.findByText("No campaigns yet. A team starts one for its backlog label or milestone.")).toBeInTheDocument();
    cleanup(); resetFactoryReads();
    open("/insights/retrospectives?status=finished", () => jsonResponse(reply([])));
    expect(await screen.findByText("No campaign matches these filters.")).toBeInTheDocument();
  });

  it("says that it is loading before the first answer", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([LIVE])));
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await screen.findByRole("table");
  });

  it("shows a failed read as a failure and never as an empty list", async () => {
    open("/insights/retrospectives", () => jsonResponse({ detail: { code: "projection_failed", message: "no" } }, 500));
    await settle();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No campaigns yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No campaign matches/)).not.toBeInTheDocument();
  });
});

describe("coverage, identity and paging (V1 review 114)", () => {
  it("keeps a partial value and says how many outcomes are unknown and why", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([row({ merged: { value: 41, reason: null, coverage: "partial", unknown_count: 2, reasons: ["delivery_history_missing"] } })])));
    await screen.findByRole("table");
    const live = within(table()).getByRole("link", { name: "release:v4.4" }).closest("tr")!;
    expect(live).toHaveTextContent("41 / 63");
    expect(live).toHaveTextContent("partial · 2 unknown · delivery history missing");
  });

  it("says why a repository count is unknown when the read came after the cutoff", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([row({ repositories: [{ repo: "o/a1", issues: null, reason: "membership_read_after_cutoff" }] })])));
    await screen.findByRole("table");
    expect(screen.getByText("o/a1 (", { exact: false })).toHaveTextContent("o/a1 (unknown · read after the cutoff)");
  });

  it("names the freeze time apart from the cutoff of the frozen counts", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([row({ status: "finished", finished_at: "2026-11-18T09:00:00",
      source: { kind: "frozen", as_of: "2026-11-18T09:00:00", frozen_at: "2026-11-25T10:00:00" } })])));
    await screen.findByRole("table");
    expect(screen.getByText(/Counts as of Nov 18, 2026, frozen Nov 25, 2026/)).toBeInTheDocument();
  });

  it("keeps two teams with one name apart, and groups one repository under one heading whatever its spelling", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([
      row({ id: 1, team: { id: 5, name: "Same" }, repositories: [{ repo: "O/A1", issues: 1, reason: null }, { repo: "o/a1", issues: 1, reason: null }] }),
      row({ id: 2, team: { id: 6, name: "Same" }, repositories: [{ repo: "o/a1", issues: 2, reason: null }] }),
    ])));
    await screen.findByRole("table");
    expect(within(table()).getAllByRole("columnheader").map((h) => h.textContent).filter((t) => t?.startsWith("Team"))).toEqual(["Team Same", "Team Same"]);
    expect(within(table()).getAllByRole("link", { name: "release:v4.4" })[0].closest("tr")!.querySelectorAll("li")).toHaveLength(1); // listed once
    cleanup(); resetFactoryReads();
    open("/insights/retrospectives?group=repository", () => jsonResponse(reply([
      row({ id: 1, repositories: [{ repo: "O/A1", issues: 1, reason: null }] }), row({ id: 2, repositories: [{ repo: "o/a1", issues: 2, reason: null }] })])));
    await screen.findByRole("table");
    expect(within(table()).getAllByRole("columnheader").map((h) => h.textContent).filter((t) => t?.toLowerCase() === "o/a1")).toEqual(["O/A1"]);
  });

  it("reaches every campaign with Show more, follows the cursor, and starts again when a filter changes", async () => {
    const pages = (query: URLSearchParams) => query.get("cursor") === "100"
      ? jsonResponse(reply([row({ id: 101, selector: "label:last" })], { total: 101 }))
      : jsonResponse(reply([LIVE, FROZEN], { total: 101, has_more: true, next_cursor: "100" }));
    const fixture = open("/insights/retrospectives", pages);
    await screen.findByRole("table");
    expect(screen.getByText("Showing 2 of 101 campaigns.")).toBeInTheDocument();
    expect(within(table()).queryByRole("link", { name: "last" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show more campaigns" }));
    expect(await within(table()).findByRole("link", { name: "last" })).toBeInTheDocument();
    expect(sent(fixture).get("cursor")).toBe("100");
    expect(screen.queryByRole("button", { name: "Show more campaigns" })).not.toBeInTheDocument();
    choose("Status", "live");
    await waitFor(() => expect(sent(fixture).get("status")).toBe("live"));
    expect(sent(fixture).get("cursor")).toBeNull();
    expect(await screen.findByRole("button", { name: "Show more campaigns" })).toBeInTheDocument();
  });

  it("carries the selected repository in the link to a campaign, and nothing when none is selected", async () => {
    open("/insights/retrospectives?scope_id=11", () => jsonResponse(reply([LIVE])));
    await screen.findByRole("table");
    expect(within(table()).getByRole("link", { name: "release:v4.4" })).toHaveAttribute("href", "/insights/retrospectives/1?scope_id=11");
    cleanup(); resetFactoryReads();
    open("/insights/retrospectives", () => jsonResponse(reply([LIVE])));
    await screen.findByRole("table");
    expect(within(table()).getByRole("link", { name: "release:v4.4" })).toHaveAttribute("href", "/insights/retrospectives/1");
  });
});

describe("every combination of known and unknown numbers (V1 review 116)", () => {
  const cell = (r: CampaignRow) => {
    open("/insights/retrospectives", () => jsonResponse(reply([r])));
    return screen.findByRole("table").then(() => within(table()).getByRole("link", { name: "release:v4.4" }).closest("tr")!.querySelector('[data-l="Merged / in scope"]')!);
  };
  const unknownMerged = { value: null, reason: "delivery_history_missing", coverage: "partial", unknown_count: 2, reasons: ["delivery_history_missing"] };

  it("keeps the known denominator, the unknown count and the reason when the numerator is unknown", async () => {
    const merged = await cell(row({ merged: unknownMerged, in_scope: { value: 3, reason: null, coverage: "full", unknown_count: 0, reasons: [] } }));
    expect(merged).toHaveTextContent("unknown / 3");
    expect(merged).toHaveTextContent("merged: unknown · delivery history missing · 2 unknown");
    expect(merged).not.toHaveTextContent("in scope:");
  });

  it("keeps the known numerator when the denominator is unknown", async () => {
    const merged = await cell(row({ merged: measure(5), in_scope: { value: null, reason: "membership_read_after_cutoff", reasons: ["membership_read_after_cutoff"] } }));
    expect(merged).toHaveTextContent("5 / unknown");
    expect(merged).toHaveTextContent("in scope: unknown · read after the cutoff");
  });

  it("shows both known values with the partial coverage of each", async () => {
    const part = (n: number) => ({ value: n, reason: null, coverage: "partial", unknown_count: 1, reasons: ["delivery_history_missing"] });
    const merged = await cell(row({ merged: part(4), in_scope: part(9) }));
    expect(merged).toHaveTextContent("4 / 9");
    expect(merged).toHaveTextContent("merged: partial · 1 unknown · delivery history missing");
    expect(merged).toHaveTextContent("in scope: partial · 1 unknown · delivery history missing");
  });

  it("keeps one line when both are unknown in the same way, and two notes when they differ", async () => {
    const both = await cell(row({ merged: measure(null, "no_snapshot"), in_scope: measure(null, "no_snapshot") }));
    expect(both.textContent).toBe("unknown · no snapshot recorded");
    cleanup(); resetFactoryReads();
    const differ = await cell(row({ merged: measure(null, "no_snapshot"), in_scope: measure(null, "not_read_yet") }));
    expect(differ).toHaveTextContent("unknown / unknown");
    expect(differ).toHaveTextContent("merged: unknown · no snapshot recorded");
    expect(differ).toHaveTextContent("in scope: unknown · not read yet");
  });

  it("keeps the shared coverage, unknown count and every reason on the one line when both are unknown in the same way", async () => {
    const shared = { value: null, reason: "incomplete_read", coverage: "partial", unknown_count: 1, reasons: ["incomplete_read", "membership_read_after_cutoff"] };
    const merged = await cell(row({ merged: shared, in_scope: shared }));
    expect(merged.textContent).toBe("unknown · could not be read, read after the cutoff · partial · 1 unknown");
  });

  it("does not combine two unknown measures with different coverage", async () => {
    const part = { value: null, reason: "no_snapshot", coverage: "partial", unknown_count: 1, reasons: ["no_snapshot"] };
    const merged = await cell(row({ merged: part, in_scope: { ...part, coverage: "unavailable" } }));
    expect(merged).toHaveTextContent("unknown / unknown");
    expect(merged).toHaveTextContent("merged: unknown · no snapshot recorded · 1 unknown");
    expect(merged).toHaveTextContent("in scope: unknown · no snapshot recorded · 1 unknown");
  });

  it("shows two plain known numbers with no note", async () => {
    expect((await cell(row({}))).textContent).toBe("41 / 63");
  });
});

describe("a team that watches no repository now", () => {
  const EMPTY = row({ repositories: [], repositories_unknown_reason: "no_repositories_watched" });

  it("says that it watches none, and does not call that unknown", async () => {
    open("/insights/retrospectives", () => jsonResponse(reply([EMPTY])));
    await screen.findByRole("table");
    const line = within(table()).getByRole("link", { name: "release:v4.4" }).closest("tr")!;
    expect(line).toHaveTextContent("None · the team watched no repositories");
    expect(line.querySelector('[data-l="Repositories (issues)"]')).not.toHaveTextContent("unknown");
  });

  it("has its own group when grouped by repository, apart from unknown repositories", async () => {
    open("/insights/retrospectives?group=repository", () => jsonResponse(reply([EMPTY, NO_SNAPSHOT])));
    await screen.findByRole("table");
    expect(within(table()).getAllByRole("columnheader").map((h) => h.textContent).filter((t) => /^(No repositories|Repositories unknown)/.test(t ?? ""))).toEqual(["No repositories watched", "Repositories unknown"]);
  });
});
