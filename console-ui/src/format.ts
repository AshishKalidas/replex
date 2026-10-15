import type { RunSummary, Step } from "./types";

export function formatTokens(n: number | undefined): string {
  if (n === undefined) return "—";
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString("en-US");
}

export function formatMs(ms: number | undefined): string {
  if (ms === undefined) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(ms >= 10_000 ? 0 : 1)} s`;
  return `${ms} ms`;
}

export function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : iso;
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export function toJson(value: unknown): string {
  if (value === undefined) return "undefined";
  if (typeof value === "string") return JSON.stringify(value);
  return JSON.stringify(value, null, 2) ?? String(value);
}

export function inlineJson(value: unknown, max = 80): string {
  const text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  return truncate(text.replace(/\s+/g, " "), max);
}

export function stepTitle(step: Step): string {
  if (step.kind === "tool") return step.name;
  if (step.tool_requests.length) return `Asks for ${step.tool_requests.map((r) => r.name).join(", ")}`;
  if (step.error) return "Error";
  return step.text ? "Replies" : step.stop_reason;
}

export function stepSubtitle(step: Step): string {
  if (step.kind === "tool") {
    return step.error ? step.error : inlineJson(step.args, 90);
  }
  if (step.error) return step.error;
  return step.text ? truncate(step.text.replace(/\s+/g, " "), 110) : `${step.model} · ${step.stop_reason}`;
}

/** Text of the last message sent to the model (what the step was answering). */
export function lastMessageText(messages: unknown[]): string {
  const last = messages[messages.length - 1] as { content?: unknown } | undefined;
  if (!last) return "";
  const content = last.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        const b = block as { type?: string; text?: string; content?: unknown };
        if (b.type === "text") return b.text ?? "";
        if (b.type === "tool_result") return typeof b.content === "string" ? b.content : JSON.stringify(b.content);
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

export type RunFilter = "all" | "baseline" | "candidate" | "failed";

export function filterRuns(runs: RunSummary[], query: string, filter: RunFilter): RunSummary[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return runs.filter((r) => {
    if (filter === "baseline" && r.kind !== "baseline") return false;
    if (filter === "candidate" && r.kind !== "candidate") return false;
    if (filter === "failed" && r.drift !== "failed") return false;
    const hay = [r.name, r.kind, r.provider, r.model, ...(r.tool_names ?? [])].join(" ").toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}
