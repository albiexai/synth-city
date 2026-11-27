import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkPage } from "../src/features/factory/pages/WorkPage";
import { resetFactoryReads } from "../src/features/factory/reads";
import { clearOperatorToken } from "../src/features/agent-teams/operatorAuth";
import work from "./fixtures/factory/v1-p04/work-items.json";
import workItem from "./fixtures/factory/v1-p04/work-item.json";
import repositories from "./fixtures/factory/v1-p04/repositories.json";
import { fixtureFetch, jsonResponse, renderRoute, settle, visibility } from "./helpers/factory";

beforeEach(async () => {
  resetFactoryReads();
  clearOperatorToken();
  localStorage.clear();
  await visibility("visible");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const first = work.first_page.response;
const finished = work.finished.response;

function serve(page: unknown) {
  return fixtureFetch((path) =>
    path === "factory/repositories"
      ? jsonResponse(repositories.normal.response)
      : jsonResponse(page),
  );
}

describe("compact Work list", () => {
  it("shows one compact row per item with issue, repository, team, state, owner, PR and age", async () => {
    const item = workItem.dispatched.response.work_item;
    serve({ ...first, items: [item, ...first.items.slice(1)] });
    renderRoute(<WorkPage />, "/work");
    await settle();
    const table = document.querySelector("[data-work-table]") as HTMLElement;
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Issue", "Team", "State", "Owner", "PR", "Updated"]);
    expect(within(table).queryByText(/Record observation|Open details|Stage|Actions/)).not.toBeInTheDocument();
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getByRole("link", { name: new RegExp(`^#${item.item.issue_number} `) })).toHaveAttribute("href", `/work/${item.item.id}`);
    expect(within(row).getByText(item.repository.display_name)).toBeInTheDocument();
    expect(within(row).getByText(item.team.name)).toBeInTheDocument();
    expect(within(row).getByText("In progress")).toBeInTheDocument();
    expect(within(row).queryByText(/raw status|phase|implementation/i)).not.toBeInTheDocument();
  });

  it("shows Waiting for only while the work is not finished", async () => {
    const waitingFinished = structuredClone(finished);
    waitingFinished.items[0].waiting = { actor: "leader", reason_code: "x", summary: "Stale waiting text." };
    serve(waitingFinished);
    renderRoute(<WorkPage />, "/work?category=finished");
    await settle();
    expect(screen.queryByText(/Waiting for|No waiting actor established|Stale waiting text/)).not.toBeInTheDocument();
    cleanup();
    resetFactoryReads();
    serve(first);
    renderRoute(<WorkPage />, "/work");
    await settle();
    const active = first.items[0];
    expect(screen.getByText(`Waiting for ${active.waiting!.actor === "leader" ? "Leader" : active.waiting!.actor}: ${active.waiting!.summary}`)).toBeInTheDocument();
    const noWaiter = structuredClone(first);
    noWaiter.items[0].waiting = null;
    cleanup();
    resetFactoryReads();
    serve(noWaiter);
    renderRoute(<WorkPage />, "/work");
    await settle();
    expect(screen.queryByText(/No waiting actor established/)).not.toBeInTheDocument();
  });

  it("shows the age and keeps the exact local time in the hover text", async () => {
    vi.useFakeTimers();
    const aged = structuredClone(first);
    const now = new Date("2026-10-13T12:00:00Z");
    vi.setSystemTime(now);
    aged.items[0].item.updated_at = "2026-10-13T10:00:00Z";
    aged.items[1].item.updated_at = "2026-10-13T11:30:00Z";
    serve(aged);
    renderRoute(<WorkPage />, "/work");
    await settle();
    const hours = screen.getByText("2 h ago").closest("time")!;
    const minutes = screen.getByText("30 min ago").closest("time")!;
    expect(hours).toHaveAttribute("datetime", "2026-10-13T10:00:00Z");
    const exact = new Date("2026-10-13T10:00:00Z").toLocaleString();
    expect(hours.getAttribute("title")).toBe(exact);
    // The exact local time is also text, so one item reads the same on every screen (#533).
    expect(hours.textContent).toContain(exact);
    expect(minutes).toHaveAttribute("datetime", "2026-10-13T11:30:00Z");
  });

  it("defaults to active work, and Finished and All are one click away", async () => {
    const { requests } = serve(first);
    renderRoute(<WorkPage />, "/work");
    await settle();
    const categories = () => requests.filter((r) => r.path === "factory/work-items").map((r) => r.query.get("category"));
    expect(categories()).toEqual(["open"]);
    const chips = within(screen.getByRole("group", { name: "Category" }));
    expect(chips.getByRole("button", { name: "Active" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("link", { name: "Reset filters" })).not.toBeInTheDocument();
    fireEvent.click(chips.getByRole("button", { name: "Finished" }));
    await settle();
    expect(categories().at(-1)).toBe("finished");
    expect(chips.getByRole("button", { name: "Finished" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(chips.getByRole("button", { name: "All" }));
    await settle();
    expect(categories().at(-1)).toBe("all");
    fireEvent.click(chips.getByRole("button", { name: "Active" }));
    await settle();
    expect(categories().at(-1)).toBe("open");
    expect(screen.queryByRole("link", { name: "Reset filters" })).not.toBeInTheDocument();
  });

  it("keeps a link that names one category and shows it as a chip", async () => {
    const { requests } = serve(first);
    renderRoute(<WorkPage />, "/work?category=attention");
    await settle();
    expect(requests.find((r) => r.path === "factory/work-items")!.query.get("category")).toBe("attention");
    const chips = within(screen.getByRole("group", { name: "Category" }));
    expect(chips.getByRole("button", { name: "Needs attention" })).toHaveAttribute("aria-pressed", "true");
    expect(chips.getByRole("button", { name: "Active" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("link", { name: "Reset filters" })).toHaveAttribute("href", "/work");
  });

  it("keeps the owner and other filters in the URL and shows the ranked queue", async () => {
    const repo = repositories.normal.response.repositories[0];
    const queued = work.queued.response.items[0];
    const { requests } = fixtureFetch((path) => {
      if (path === "factory/repositories") return jsonResponse(repositories.normal.response);
      if (path === "agent-teams/presets") return jsonResponse({ presets: [
        { ...repo.team, slots: [{ id: 23, display_name: "Avery", role: "Builder" }] },
      ] });
      return jsonResponse({ ...first, items: [
        { ...queued, owner_queue_position: 1 },
        { ...queued, item: { ...queued.item, id: 900, issue_number: 900 }, owner_queue_position: 3 },
      ] });
    });
    const initial = `team_id=${repo.team.id}&scope_id=${repo.scope_id}&provider=codex-cli`;
    renderRoute(<WorkPage />, `/work?${initial}`);
    await settle();
    fireEvent.change(screen.getByRole("combobox", { name: "Owner" }), { target: { value: "23" } });
    fireEvent.click(screen.getByRole("button", { name: "Queued" }));
    await settle();
    const query = requests.filter((r) => r.path === "factory/work-items").at(-1)!.query;
    expect(Object.fromEntries(query)).toEqual({
      team_id: String(repo.team.id), scope_id: String(repo.scope_id), provider: "codex-cli",
      owner_slot_id: "23", category: "queued",
    });
    cleanup();
    resetFactoryReads();
    renderRoute(<WorkPage />, `/work?${query}`);
    await settle();
    expect(screen.getByRole("combobox", { name: "Owner" })).toHaveValue("23");
    expect(screen.getByRole("option", { name: "Avery · Builder" })).toBeInTheDocument();
    expect(screen.getByText(/Expected order, recomputed on each poll/)).toBeInTheDocument();
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    for (const [index, label] of ["next", "3rd"].entries()) {
      const cells = within(rows[index]).getAllByRole("cell");
      expect(within(cells[0]).getByText(queued.repository.display_name)).toBeInTheDocument();
      expect(within(cells[2]).getByText(label, { exact: true })).toBeInTheDocument();
    }
  });

  it("uses one plain sentence as the intro", async () => {
    serve(first);
    renderRoute(<WorkPage />, "/work");
    await settle();
    expect(screen.getByText("Work across your teams. Open an item to see its history and actions.")).toBeInTheDocument();
    expect(screen.queryByText(/Assigned harness filters describe/)).not.toBeInTheDocument();
    await act(async () => undefined);
  });
});
