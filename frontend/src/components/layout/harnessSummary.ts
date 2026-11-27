import { scopedVersionBadge } from "../../features/harnesses/versionLine";

type HarnessStatus = {
  display_name: string; installed: boolean; version?: string | null; unavailable_reason?: string | null;
  version_user?: string | null; version_source?: string | null; version_unknown_reason?: string | null;
};

/** One header badge for every harness, so the header keeps one row as harnesses are added. */
export function harnessSummary(harnesses: HarnessStatus[]) {
  const installed = harnesses.filter((harness) => harness.installed).length;
  const noun = harnesses.length === 1 ? "harness" : "harnesses";
  return {
    label: `${installed} installed`,
    problem: installed < harnesses.length,
    title: [`${installed} of ${harnesses.length} supported ${noun} installed for this factory`,
      ...harnesses.map((harness) => {
        // A chosen user's version (or its unknown message) is named whether or not the controller has a claude of its own.
        const scoped = scopedVersionBadge(harness);
        if (scoped) {
          const named = harness.version ? `${scoped} (${harness.version_user})` : scoped;
          return harness.installed
            ? `${harness.display_name} ${named}`
            : `${harness.display_name} ${named}; controller binary missing${harness.unavailable_reason ? `: ${harness.unavailable_reason}` : ""}`;
        }
        return harness.installed
          ? `${harness.display_name} ${harness.version ? `v${harness.version}` : "installed"}`
          : `${harness.display_name} missing${harness.unavailable_reason ? `: ${harness.unavailable_reason}` : ""}`;
      }),
    ].join("\n"),
  };
}
