import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CharterField } from "../src/features/factory/CharterField";
import {
  findUnresolved,
  resolveCharterTemplate,
  type CharterRole,
  type CharterTemplateValues,
} from "../src/features/factory/charterTemplates";

const FULL: CharterTemplateValues = {
  release_branch: "release/v4.4",
  ready_label: "ready-label",
  hold_label: "hold-label",
  lane_label: "v44-ui",
  plan_size_limit: "60",
  supervisor_address: "Supervisor (sup-key)",
};

function Harness({ initial = "", role = "builder", values = FULL }: { initial?: string; role?: CharterRole; values?: CharterTemplateValues }) {
  const [value, setValue] = useState(initial);
  return <CharterField role={role} values={values} value={value} onChange={setValue} />;
}

const box = () => screen.getByLabelText("Charter") as HTMLTextAreaElement;
const start = () => fireEvent.click(screen.getByRole("button", { name: "Start from a template" }));
const typed = (text: string) => fireEvent.change(box(), { target: { value: text } });

describe("CharterField", () => {
  it("fills an empty field from the template with the given values", () => {
    render(<Harness role="leader" />);
    start();
    expect(box().value).toBe(resolveCharterTemplate("leader", FULL).text);
    expect(box().value).toContain("release/v4.4");
  });

  it("uses the template of its own role", () => {
    render(<Harness role="reviewer" />);
    start();
    expect(box().value.startsWith("# Reviewer charter")).toBe(true);
  });

  it("never replaces a non-empty field: it asks first, and Cancel keeps the text byte for byte", () => {
    const own = "My own charter.  \n\nSecond line.\n";
    render(<Harness initial={own} />);
    start();
    expect(box().value).toBe(own);
    expect(screen.getByText("Append the template below your text?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(box().value).toBe(own);
    expect(screen.queryByText("Append the template below your text?")).not.toBeInTheDocument();
  });

  it("appends after confirmation and keeps the existing text as an exact prefix", () => {
    const own = "My own charter.  \n";
    render(<Harness initial={own} />);
    start();
    fireEvent.click(screen.getByRole("button", { name: "Append" }));
    expect(box().value.startsWith(own)).toBe(true);
    expect(box().value.endsWith(resolveCharterTemplate("builder", FULL).text)).toBe(true);
    expect(screen.queryByText("Append the template below your text?")).not.toBeInTheDocument();
  });

  it("keeps an edit made after the template and does not apply the template again", () => {
    const { rerender } = render(<Harness />);
    start();
    typed("Edited by the Director.");
    expect(box().value).toBe("Edited by the Director.");
    rerender(<Harness values={{ ...FULL, release_branch: "other" }} />);
    expect(box().value).toBe("Edited by the Director.");
  });

  it("warns with the exact names left in the current text, and the warning clears when they are replaced", () => {
    render(<Harness role="leader" values={{ ...FULL, hold_label: undefined, supervisor_address: undefined }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    start();
    const left = findUnresolved(box().value);
    expect([...left].sort()).toEqual(["hold_label", "supervisor_address"]);
    const warning = screen.getByRole("status");
    for (const name of left) expect(warning).toHaveTextContent(`{{${name}}}`);
    expect(warning).toHaveTextContent(/replace each one with a real value/i);
    typed(box().value.replace(/\{\{hold_label\}\}/g, "hold-label"));
    expect(screen.getByRole("status")).not.toHaveTextContent("{{hold_label}}");
    expect(screen.getByRole("status")).toHaveTextContent("{{supervisor_address}}");
    typed(box().value.replace(/\{\{supervisor_address\}\}/g, "Supervisor (sup-key)"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows no warning when every value is given", () => {
    render(<Harness />);
    start();
    expect(box().value).not.toContain("{{");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("gives each field instance its own id, so a label focuses its own textarea", () => {
    render(<><Harness /><Harness /></>);
    const boxes = screen.getAllByLabelText("Charter") as HTMLTextAreaElement[];
    expect(boxes).toHaveLength(2);
    expect(boxes[0].id).not.toBe(boxes[1].id);
    for (const label of document.querySelectorAll("label")) {
      expect(document.getElementById(label.htmlFor)?.tagName).toBe("TEXTAREA");
    }
  });

  it("links to the charter templates docs page", () => {
    render(<Harness />);
    const link = screen.getByRole("link", { name: /charter templates/i });
    // The page is published on the release branch. A link to master returns 404 until the release is merged there.
    expect(link).toHaveAttribute("href", "https://github.com/albiexai/synth-city/blob/release/v4.4/docs/features/charter-templates.md");
    expect(link.getAttribute("href")).not.toContain("/master/");
    expect(link).toHaveAttribute("target", "_blank");
  });
});
