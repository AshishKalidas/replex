// Plain-language wording for the drift history (replex.history).

import { formatDate } from "./format";
import type { HistoryEntry, HistoryTrend, ScenarioHistory } from "./types";

export type Marker = "passed" | "changed" | "accepted" | "rejected";

export const MARKER_LABEL: Record<Marker, string> = {
  passed: "Behaves as recorded",
  changed: "Behaviour changed",
  accepted: "Change accepted",
  rejected: "Change rejected",
};

export function markerOf(entry: HistoryEntry): Marker {
  if (entry.kind === "check") return entry.verdict === "passed" ? "passed" : "changed";
  return entry.kind;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "21 Oct" from an ISO timestamp, independent of the browser's locale. */
export function shortDay(iso: string | null): string {
  const m = iso ? /^\d{4}-(\d{2})-(\d{2})/.exec(iso) : null;
  if (!m) return "—";
  return `${Number(m[2])} ${MONTHS[Number(m[1]) - 1] ?? m[1]}`;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** What one history entry means, for the list next to the timeline. */
export function describeEntry(entry: HistoryEntry): string {
  if (entry.kind !== "check") {
    const how = entry.via === "console" ? "in the console" : "with replex approve";
    return entry.kind === "accepted"
      ? `The latest run was accepted as the new recording ${how}.`
      : `The latest run was rejected ${how}; the recording was kept.`;
  }
  const tools = entry.tools.length ? entry.tools.join(" → ") : "no tools";
  if (entry.verdict === "passed") return `Used ${tools}.`;
  const failed = Object.keys(entry.fail).sort().join(", ");
  return `Used ${tools}. Checks that failed: ${failed || "none"}.`;
}

/** "1 of 2 recent checks behaved as recorded." */
export function passSummary(h: ScenarioHistory): string {
  if (!h.checks) return "No drift checks recorded yet.";
  return `${h.passed} of ${plural(h.checks, "recent check")} behaved as recorded.`;
}

/** The overview's one-line trend, or null when there is nothing to report. */
export function trendSentence(t: HistoryTrend): string | null {
  if (!t.checks && !t.accepted && !t.rejected) return null;
  const details = [`${t.failed} found changed behaviour`];
  if (t.accepted) details.push(`${plural(t.accepted, "change")} accepted`);
  if (t.rejected) details.push(`${plural(t.rejected, "change")} rejected`);
  return `${plural(t.checks, "check")} in the ${t.days} days to ${shortDay(t.until)}: ${details.join(", ")}.`;
}

export function entryTime(entry: HistoryEntry): string {
  return `${formatDate(entry.at)} UTC`;
}
