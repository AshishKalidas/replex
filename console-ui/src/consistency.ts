// Plain-language summaries of repeated runs (replex.consistency).
import { humanize } from "./explain";
import type { ConsistencyReport, PathVariant, Spread } from "./types";

/** “Get order › Check refund policy”, plus the answer category when runs were split by one. */
export function variantPath(v: PathVariant): string {
  const path = v.tools.length ? v.tools.map(humanize).join(" › ") : "Used no tools";
  return v.category === null ? path : `${path}, answering “${v.category}”`;
}

/** “7 of 10 runs”. */
export function variantShare(v: PathVariant, total: number): string {
  return `${v.runs.length} of ${total} run${total === 1 ? "" : "s"}`;
}

/** One sentence: how many paths the runs took, and how the recorded path fared. */
export function consistencyHeadline(r: ConsistencyReport): string {
  const recorded = r.variants.find((v) => v.matches_baseline);
  if (r.distinct_paths === 1) {
    const head = r.runs === 1 ? "The one run took" : `All ${r.runs} runs took the same path`;
    if (r.baseline_share === null) return r.runs === 1 ? `${head} a single path.` : `${head}.`;
    if (r.runs === 1) return recorded ? `${head} the recorded path.` : `${head} a different path from the recording.`;
    return recorded ? `${head}: the recorded one.` : `${head}, but not the recorded one.`;
  }
  const head = `The agent took ${r.distinct_paths} different paths in ${r.runs} runs.`;
  if (r.baseline_share === null) return head;
  if (!recorded) return `${head} None of them matches the recording.`;
  const share = variantShare(recorded, r.runs);
  return r.baseline_is_majority
    ? `${head} Most follow the recorded path (${share}).`
    : `${head} Only ${share} follow the recorded path.`;
}

/** “1,820 to 2,410 (typically 1,950)”, or the single value when every run was the same. */
export function spreadText(s: Spread, format: (n: number) => string): string {
  if (s.min === s.max) return format(s.min);
  return `${format(s.min)} to ${format(s.max)} (typically ${format(Math.round(s.median))})`;
}
