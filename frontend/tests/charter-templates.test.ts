import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CHARTER_PLACEHOLDERS,
  CHARTER_ROLES,
  CHARTER_TEMPLATES,
  appendTemplate,
  charterTemplateValues,
  findUnresolved,
  planSizeLimit,
  resolveCharterTemplate,
  supervisorAddress,
  type CharterTemplateValues,
} from "../src/features/factory/charterTemplates";

const RULES = [
  "Plan size limit",
  "Light review tier",
  "Focused local tests",
  "Test evidence block",
  "Review hold",
  "Escalation order",
  "Process safety",
  "No interactive questions",
];

const ALL_VALUES: CharterTemplateValues = {
  release_branch: "release/v4.4",
  ready_label: "synth-city-v44-ready",
  hold_label: "synth-city-v44-review-hold",
  lane_label: "v44-ui",
  plan_size_limit: "60",
  supervisor_address: "Rename implementation Supervisor (supervisor-key)",
};

const repoRoot = resolve(__dirname, "../..");
const read = (path: string) => readFileSync(resolve(repoRoot, path), "utf8");

describe("charter templates: the rules and their reasons", () => {
  for (const role of CHARTER_ROLES) {
    describe(role, () => {
      for (const rule of RULES) {
        it(`${rule} is one rule line followed by a one-line reason`, () => {
          const pattern = new RegExp(`^- \\*\\*${rule}:\\*\\* .+\\n  Why: .+$`, "m");
          expect(CHARTER_TEMPLATES[role]).toMatch(pattern);
        });
      }
    });
  }

  it("states the review hold as team practice and the exact-head acceptance as the enforced gate", () => {
    for (const role of CHARTER_ROLES) {
      const hold = CHARTER_TEMPLATES[role].match(/^- \*\*Review hold:\*\* .+\n {2}Why: .+$/m)?.[0] ?? "";
      expect(hold).toMatch(/team practice/i);
      expect(hold).toMatch(/exact head/i);
      expect(hold).toMatch(/enforced/i);
    }
  });

  it("defines the light review tier as docs, UI or tests only, without mutation probes", () => {
    for (const role of CHARTER_ROLES) {
      const tier = CHARTER_TEMPLATES[role].match(/^- \*\*Light review tier:\*\* .+$/m)?.[0] ?? "";
      expect(tier).toContain("docs, UI or tests");
      expect(tier).toContain("without mutation probes");
    }
  });

  it("keeps a full review for a PR with the security label, whatever files it changes", () => {
    for (const role of CHARTER_ROLES) {
      const tier = CHARTER_TEMPLATES[role].match(/^- \*\*Light review tier:\*\* .+$/m)?.[0] ?? "";
      expect(tier).toMatch(/`security` label.*full review.*(whatever|regardless)/i);
    }
  });

  it("routes each kind of question to one role: plans and assignments, incidents and gates, releases", () => {
    for (const role of CHARTER_ROLES) {
      const rule = CHARTER_TEMPLATES[role].match(/^- \*\*Escalation order:\*\* .+$/m)?.[0] ?? "";
      const [leader, supervisor, director] = ["Leader", "Supervisor", "Director"].map((name) => rule.indexOf(name));
      expect(leader).toBeGreaterThanOrEqual(0);
      expect(supervisor).toBeGreaterThan(leader);
      expect(director).toBeGreaterThan(supervisor);
      expect(rule).toMatch(/assignments? and plans? to the Leader/);
      expect(rule).toMatch(/incidents.* gate.* to the Supervisor/);
      expect(rule).toMatch(/release.* to the Director/);
      expect(rule).not.toMatch(/first/i);
    }
  });

  it("gives plan decisions to the Leader: the reviewer flags oversized work and never rejects a plan", () => {
    const reviewerRule = CHARTER_TEMPLATES.reviewer.match(/^- \*\*Plan size limit:\*\* .+$/m)?.[0] ?? "";
    expect(reviewerRule).toMatch(/Flag .*\{\{plan_size_limit\}\}.* to the Leader/);
    expect(reviewerRule).toMatch(/Leader decides/);
    expect(reviewerRule).not.toMatch(/reject/i);
    expect(CHARTER_TEMPLATES.leader.match(/^- \*\*Plan size limit:\*\* .+$/m)?.[0]).toMatch(/Reject a plan over/);
  });

  it("sends the reviewer's blockers by kind, not all through the Leader", () => {
    expect(CHARTER_TEMPLATES.reviewer).toMatch(/plan and assignment questions to the Leader/);
    expect(CHARTER_TEMPLATES.reviewer).toMatch(/incidents and gate problems to the Supervisor \(\{\{supervisor_address\}\}\)/);
  });

  it("forbids pattern kills and interactive question tools by name", () => {
    for (const role of CHARTER_ROLES) {
      const text = CHARTER_TEMPLATES[role];
      for (const word of ["pkill", "killall", "pgrep -f"]) expect(text).toContain(word);
      for (const word of ["request_user_input", "AskUserQuestion"]) expect(text).toContain(word);
    }
  });

  it("asks for the full test evidence block and focused tests with full suites on CI", () => {
    for (const role of CHARTER_ROLES) {
      const text = CHARTER_TEMPLATES[role];
      expect(text).toMatch(/^- \*\*Test evidence block:\*\* .*exact head.*pass\/fail counts.*hosted CI run URL/m);
      expect(text).toMatch(/^- \*\*Focused local tests:\*\* .*full suites.* CI/m);
    }
  });

  it("carries the plan-size limit placeholder in every template", () => {
    for (const role of CHARTER_ROLES) {
      expect(CHARTER_TEMPLATES[role].match(/^- \*\*Plan size limit:\*\* .+$/m)?.[0]).toContain("{{plan_size_limit}}");
    }
  });

  it("gives the builder a lane section", () => {
    expect(CHARTER_TEMPLATES.builder).toMatch(/^## Lane\n[\s\S]*\{\{lane_label\}\}/m);
  });
});

describe("charter templates: placeholders", () => {
  it("use only the six documented placeholders, and together use all six", () => {
    const used = new Set<string>();
    for (const role of CHARTER_ROLES) {
      for (const name of findUnresolved(CHARTER_TEMPLATES[role])) used.add(name);
    }
    expect([...used].sort()).toEqual([...CHARTER_PLACEHOLDERS].sort());
  });

  it("resolve completely when every value is given", () => {
    for (const role of CHARTER_ROLES) {
      const { text, unresolved } = resolveCharterTemplate(role, ALL_VALUES);
      expect(unresolved).toEqual([]);
      expect(text).not.toContain("{{");
    }
    expect(resolveCharterTemplate("builder", ALL_VALUES).text).toContain("v44-ui");
  });

  it.each(CHARTER_PLACEHOLDERS)("keep {{%s}} as literal text and list it when its value is missing", (name) => {
    const values: CharterTemplateValues = { ...ALL_VALUES };
    delete values[name];
    for (const role of CHARTER_ROLES) {
      const { text, unresolved } = resolveCharterTemplate(role, values);
      const used = findUnresolved(CHARTER_TEMPLATES[role]).includes(name);
      expect(unresolved.includes(name)).toBe(used);
      expect(text.includes(`{{${name}}}`)).toBe(used);
    }
  });

  it("never fill a blank value", () => {
    const { text, unresolved } = resolveCharterTemplate("leader", { ...ALL_VALUES, supervisor_address: "  " });
    expect(unresolved).toContain("supervisor_address");
    expect(text).toContain("{{supervisor_address}}");
  });

  it("list each unresolved name once, in the order of first use", () => {
    const { unresolved } = resolveCharterTemplate("leader", {});
    expect(new Set(unresolved).size).toBe(unresolved.length);
    expect(unresolved).toEqual(findUnresolved(CHARTER_TEMPLATES.leader));
  });
});

describe("charter template values", () => {
  it("plan_size_limit is the documented 60, clamped to a lower scope maximum", () => {
    expect(planSizeLimit(undefined)).toBe(60);
    expect(planSizeLimit(null)).toBe(60);
    expect(planSizeLimit(120)).toBe(60);
    expect(planSizeLimit(60)).toBe(60);
    expect(planSizeLimit(45)).toBe(45);
    expect(planSizeLimit(15)).toBe(15);
  });

  it("plan_size_limit is the scope's own plan limit when the scope states one", () => {
    expect(planSizeLimit(120, 45)).toBe(45);
    expect(planSizeLimit(undefined, 90)).toBe(90);
    expect(planSizeLimit(30, 45)).toBe(30);
    expect(charterTemplateValues({ maxPlanEstimateMinutes: 45 }).plan_size_limit).toBe("45");
  });

  it("plan_size_limit ignores a plan limit that is not a positive whole number", () => {
    for (const bad of [0, -5, 12.5, Number.NaN, null, undefined]) expect(planSizeLimit(45, bad)).toBe(45);
  });

  it("the template text shows the scope's plan limit", () => {
    const text = resolveCharterTemplate("builder", charterTemplateValues({ maxPlanEstimateMinutes: 45 })).text;
    expect(text).toContain("at most 45 active minutes");
    expect(resolveCharterTemplate("builder", charterTemplateValues({})).text).toContain("at most 60 active minutes");
  });

  it("plan_size_limit ignores a maximum that is not a positive whole number", () => {
    for (const bad of [0, -5, 12.5, Number.NaN, Number.POSITIVE_INFINITY]) expect(planSizeLimit(bad)).toBe(60);
  });

  it("supervisor_address is the bound supervisor's name and key, else unresolved", () => {
    expect(supervisorAddress({ display_name: "Rename implementation Supervisor", actor_key: "sup-key" }))
      .toBe("Rename implementation Supervisor (sup-key)");
    expect(supervisorAddress(null)).toBeUndefined();
    expect(supervisorAddress(undefined)).toBeUndefined();
    expect(supervisorAddress({ display_name: "", actor_key: "sup-key" })).toBeUndefined();
    expect(supervisorAddress({ display_name: "Name", actor_key: "  " })).toBeUndefined();
  });

  it("release_branch drops a leading origin/ and never accepts the symbolic origin/HEAD", () => {
    expect(charterTemplateValues({ baseRef: "origin/release/v4.4" }).release_branch).toBe("release/v4.4");
    expect(charterTemplateValues({ baseRef: "origin/HEAD" }).release_branch).toBeUndefined();
    expect(charterTemplateValues({ baseRef: "HEAD" }).release_branch).toBeUndefined();
    expect(charterTemplateValues({ baseRef: "origin/" }).release_branch).toBeUndefined();
    expect(charterTemplateValues({}).release_branch).toBeUndefined();
  });

  it("maps the scope's labels, the lane and the cap, and leaves a blank label unresolved", () => {
    const values = charterTemplateValues({
      baseRef: "origin/release/v4.4",
      dispatchLabel: "synth-city-v44-ready",
      reviewHoldLabel: "synth-city-v44-review-hold",
      areaLabels: ["v44-ui", "area:docs"],
      maxItemEffortMinutes: 45,
      supervisor: { display_name: "Name", actor_key: "key" },
    });
    expect(values).toEqual({
      release_branch: "release/v4.4",
      ready_label: "synth-city-v44-ready",
      hold_label: "synth-city-v44-review-hold",
      lane_label: "v44-ui, area:docs",
      plan_size_limit: "45",
      supervisor_address: "Name (key)",
    });
    const sparse = charterTemplateValues({ reviewHoldLabel: null, dispatchLabel: " ", areaLabels: [] });
    expect(sparse.hold_label).toBeUndefined();
    expect(sparse.ready_label).toBeUndefined();
    expect(sparse.lane_label).toBeUndefined();
    expect(sparse.supervisor_address).toBeUndefined();
    expect(sparse.plan_size_limit).toBe("60");
  });
});

describe("appendTemplate", () => {
  it("keeps the existing text first and never replaces it", () => {
    const joined = appendTemplate("My own charter.", "TEMPLATE");
    expect(joined.startsWith("My own charter.")).toBe(true);
    expect(joined).toContain("TEMPLATE");
    expect(joined.indexOf("TEMPLATE")).toBeGreaterThan(joined.indexOf("My own charter."));
  });

  it.each([
    ["trailing spaces", "Line one.  "],
    ["a Markdown hard break", "Line one.  \nLine two.  \n"],
    ["trailing blank lines", "Line one.\n\n\n"],
    ["only whitespace", "  \n\t"],
    ["a final newline", "Line one.\n"],
  ])("keeps the existing text byte for byte as the prefix with %s", (_name, existing) => {
    const joined = appendTemplate(existing, "TEMPLATE");
    expect(joined.startsWith(existing)).toBe(true);
    expect(joined.endsWith("TEMPLATE")).toBe(true);
    expect(joined.slice(existing.length)).toMatch(/^\n*TEMPLATE$/);
  });

  it("separates the template from existing text by a blank line", () => {
    expect(appendTemplate("Line one.", "TEMPLATE")).toBe("Line one.\n\nTEMPLATE");
    expect(appendTemplate("Line one.\n", "TEMPLATE")).toBe("Line one.\n\nTEMPLATE");
    expect(appendTemplate("Line one.\n\n", "TEMPLATE")).toBe("Line one.\n\nTEMPLATE");
  });

  it("returns the template alone only when the existing text is empty", () => {
    expect(appendTemplate("", "TEMPLATE")).toBe("TEMPLATE");
  });
});

describe("charter templates: documentation", () => {
  const docs = read("docs/features/charter-templates.md");

  it("explains every rule and its reason", () => {
    for (const rule of RULES) expect(docs).toContain(rule);
    expect(docs.match(/\bWhy\b/g)?.length ?? 0).toBeGreaterThanOrEqual(RULES.length);
  });

  it("names each placeholder and where its value comes from", () => {
    for (const name of CHARTER_PLACEHOLDERS) expect(docs).toContain(`{{${name}}}`);
    expect(docs).toMatch(/60 minutes/);
    expect(docs).toMatch(/max_item_effort_minutes/);
    expect(docs).toMatch(/bound supervisor/i);
  });

  it("names the source of the light review tier and the team-practice wording of the hold", () => {
    expect(docs).toMatch(/Director, 2026-11-16/);
    expect(docs).toMatch(/team practice/i);
  });

  it("renders the double braces verbatim: every placeholder is inside a v-pre container", () => {
    // VitePress evaluates {{ }} as Vue, even inside inline code. Text outside v-pre would render as an empty cell.
    const open = docs.indexOf("::: v-pre");
    expect(open).toBeGreaterThanOrEqual(0);
    expect(docs.indexOf("{{")).toBeGreaterThan(open);
    expect(docs.trimEnd().endsWith(":::")).toBe(true);
    expect(docs.slice(0, open)).not.toContain("{{");
  });

  it("documents the security-label exception and the routing of each kind of question", () => {
    expect(docs).toMatch(/`security` label[^\n]*full review/i);
    expect(docs).toMatch(/assignments and plans to the Leader/);
    expect(docs).toMatch(/incidents and gate problems to the Supervisor/);
    expect(docs).toMatch(/release[^\n]* to the Director/);
    expect(docs).toMatch(/reviewer flags/i);
  });

  it("is in the Features navigation", () => {
    expect(read("docs/.vitepress/config.ts")).toContain("link: '/features/charter-templates'");
  });
});
