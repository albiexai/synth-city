import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  appendTemplate,
  findUnresolved,
  resolveCharterTemplate,
  type CharterRole,
  type CharterTemplateValues,
} from "./charterTemplates";

// #1309: one charter field with Start from a template. Part A (#1113) owns the templates and the resolver.
const CHARTER_DOCS_URL = "https://github.com/albiexai/synth-city/blob/master/docs/features/charter-templates.md";

interface CharterFieldProps {
  id: string;
  role: CharterRole;
  values: CharterTemplateValues;
  value: string;
  onChange: (value: string) => void;
}

/** A field never replaces text: an empty field is filled, and a field with text asks before it appends. */
export function CharterField({ id, role, values, value, onChange }: CharterFieldProps) {
  const [asking, setAsking] = useState(false);
  const unresolved = findUnresolved(value);
  const template = () => resolveCharterTemplate(role, values).text;

  return (
    <div className="space-y-2">
      <label htmlFor={id} className="block text-sm font-medium">Charter</label>
      <Textarea id={id} rows={6} value={value} onChange={(event) => { setAsking(false); onChange(event.target.value); }} />
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => (value === "" ? onChange(template()) : setAsking(true))}>
          Start from a template
        </Button>
        <a className="text-sm underline" href={CHARTER_DOCS_URL} target="_blank" rel="noreferrer">About the charter templates</a>
      </div>
      {asking && (
        <div className="space-y-2 rounded border p-3 text-sm" aria-live="polite">
          <p>Append the template below your text?</p>
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={() => { onChange(appendTemplate(value, template())); setAsking(false); }}>Append</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setAsking(false)}>Cancel</Button>
          </div>
        </div>
      )}
      {unresolved.length > 0 && (
        <p role="status" className="rounded border border-amber-500/60 p-2 text-sm">
          Unresolved placeholders: {unresolved.map((name) => `{{${name}}}`).join(", ")}. Replace each one with a real value in the text.
        </p>
      )}
    </div>
  );
}
