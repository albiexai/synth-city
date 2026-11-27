import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../src/contexts/ThemeContext";
import { resetFactoryReads } from "../src/features/factory/reads";
import { fixtureFetch, jsonResponse, settle } from "./helpers/factory";

// #1251: Audit becomes Insights with the tabs Retrospectives and Audit log. The page under Audit log is the
// existing Audit page, so it is replaced by a stub here: these tests check the navigation and the routes.
vi.mock("../src/features/factory/AuditMetricsPanel", () => ({
  AuditMetricsPage: () => <h2>Audit page stub</h2>,
}));

import App from "../src/App";

function openApp(url: string) {
  window.history.replaceState({}, "", url);
  fixtureFetch((path) => path === "factory/campaigns"
    ? jsonResponse({ schema_version: 1, generated_at: "2026-11-27T00:00:00", campaigns: [], total: 0, counts: { live: 0, finished: 0 },
      hidden_unknown_repositories: 0, finished_before_recording: 0, has_more: false, next_cursor: null })
    : jsonResponse({ projects: [], rows: [], items: [], repositories: [], presets: [], providers: [] }));
  return render(<ThemeProvider><App /></ThemeProvider>);
}
const location = () => `${window.location.pathname}${window.location.search}${window.location.hash}`;
const nav = () => screen.getByRole("navigation", { name: "Main navigation" });
const tabs = () => screen.getByRole("navigation", { name: "Insights sections" });

beforeEach(() => {
  resetFactoryReads();
  localStorage.setItem("theme", "dark"); // a stored theme, so the theme provider needs no matchMedia
  vi.stubGlobal("APP_VERSION", "0.0.0-test"); // the footer reads the build constant
});
afterEach(() => {
  cleanup();
  resetFactoryReads();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("the Insights entry in the main navigation", () => {
  it("shows Insights, not Audit, and keeps the 8 entries", async () => {
    openApp("/insights/retrospectives");
    await settle();
    expect(within(nav()).getByRole("link", { name: "Insights" })).toHaveAttribute("href", "/insights");
    expect(within(nav()).queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
    expect(within(nav()).getAllByRole("link")).toHaveLength(8);
  });

  it.each(["/insights/retrospectives", "/insights/audit"])("is the current page on %s", async (path) => {
    openApp(path);
    await settle();
    expect(within(nav()).getByRole("link", { name: "Insights" })).toHaveAttribute("aria-current", "page");
  });
});

describe("the Insights page and its tabs", () => {
  it("opens the Retrospectives tab by default, and /insights keeps the query and the hash", async () => {
    openApp("/insights?team_id=2#top");
    await settle();
    expect(location()).toBe("/insights/retrospectives?team_id=2#top");
    expect(within(tabs()).getByRole("link", { name: "Retrospectives" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs()).getByRole("link", { name: "Audit log" })).not.toHaveAttribute("aria-current");
    expect(await screen.findByText("No campaigns yet. A team starts one for its backlog label or milestone.")).toBeInTheDocument();
  });

  it("shows the existing Audit page under the Audit log tab", async () => {
    openApp("/insights/audit");
    await settle();
    expect(within(tabs()).getByRole("link", { name: "Audit log" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs()).getByRole("link", { name: "Retrospectives" })).not.toHaveAttribute("aria-current");
    expect(screen.getByText("Audit page stub")).toBeInTheDocument();
    expect(screen.queryByText(/No campaigns yet/)).not.toBeInTheDocument();
  });

  it("has one Insights heading and an accessible tab row with two links", async () => {
    openApp("/insights/audit");
    await settle();
    expect(screen.getAllByRole("heading", { name: "Insights" })).toHaveLength(1);
    expect(within(tabs()).getAllByRole("link").map((link) => link.textContent)).toEqual(["Retrospectives", "Audit log"]);
  });

  it("changes the route and the active tab when a tab is clicked", async () => {
    openApp("/insights/retrospectives");
    await settle();
    fireEvent.click(within(tabs()).getByRole("link", { name: "Audit log" }));
    await waitFor(() => expect(location()).toBe("/insights/audit"));
    expect(within(tabs()).getByRole("link", { name: "Audit log" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(tabs()).getByRole("link", { name: "Retrospectives" }));
    await waitFor(() => expect(location()).toBe("/insights/retrospectives"));
    expect(within(tabs()).getByRole("link", { name: "Retrospectives" })).toHaveAttribute("aria-current", "page");
  });
});

describe("old links keep working", () => {
  it("/audit lands on /insights/audit", async () => {
    openApp("/audit");
    await settle();
    expect(location()).toBe("/insights/audit");
    expect(screen.getByText("Audit page stub")).toBeInTheDocument();
  });

  it("/audit keeps its query and its hash", async () => {
    openApp("/audit?window=7d&team_id=2#events");
    await settle();
    expect(location()).toBe("/insights/audit?window=7d&team_id=2#events");
    expect(within(tabs()).getByRole("link", { name: "Audit log" })).toHaveAttribute("aria-current", "page");
  });

  it("replaces the old address, so Back does not return to /audit", async () => {
    window.history.replaceState({}, "", "/");
    const before = window.history.length;
    openApp("/audit?x=1");
    await settle();
    expect(window.history.length).toBe(before);
    expect(location()).toBe("/insights/audit?x=1");
  });
});
