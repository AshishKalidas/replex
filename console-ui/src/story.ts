// A run told as a conversation: what the person asked, what the agent did, what it answered.
import { humanize } from "./explain";
import type { Step, Trace } from "./types";

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        const b = block as { type?: string; text?: string };
        return b.type === "text" && typeof b.text === "string" ? b.text : "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

/** What the person said to the agent: the first user message the agent received. */
export function customerMessage(trace: Trace): string {
  const first = trace.steps.find((s) => s.kind === "model");
  if (!first || first.kind !== "model") return "";
  for (const m of first.input_messages) {
    const msg = m as { role?: string; content?: unknown };
    if (msg.role === "user") {
      const text = textOf(msg.content).trim();
      if (text) return text;
    }
  }
  return "";
}

export interface Reply {
  /** The words a person would read. */
  text: string;
  /** Other fields of a structured answer, e.g. ["Decision", "Refund requested"]. */
  facts: [string, string][];
}

const MESSAGE_KEYS = ["message", "reply", "answer", "text", "content"];

/** The agent's final answer. A JSON answer shows its message and lists the other fields. */
export function reply(finalOutput: string): Reply {
  const raw = finalOutput.trim();
  let data: unknown = null;
  if (raw.startsWith("{")) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = null;
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return { text: raw, facts: [] };
  const obj = data as Record<string, unknown>;
  const key = MESSAGE_KEYS.find((k) => typeof obj[k] === "string");
  const facts: [string, string][] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === key) continue;
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "object") continue;
    const shown = typeof v === "string" && /^[a-z][a-z_]*$/.test(v) ? humanize(v) : String(v);
    facts.push([humanize(k), shown]);
  }
  return { text: key ? String(obj[key]) : "", facts };
}

export type ActionStatus = "same" | "added" | "removed";

export interface Action {
  /** Tool name, used to match actions between two runs. */
  tool: string;
  /** "Check refund policy" */
  label: string;
  /** "category footwear, days since delivery 12" */
  detail: string;
  error: string | null;
  status: ActionStatus;
}

function detailOf(args: Record<string, unknown>): string {
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .slice(0, 3)
    .map(([k, v]) => `${humanize(k).toLowerCase()} ${String(v)}`)
    .join(", ");
}

/** The tools the agent used, in order. Falls back to the tools the model asked for. */
export function actions(trace: Trace): Action[] {
  const tools = trace.steps.filter((s): s is Extract<Step, { kind: "tool" }> => s.kind === "tool");
  if (tools.length) {
    return tools.map((t) => ({
      tool: t.name,
      label: humanize(t.name),
      detail: detailOf(t.args),
      error: t.error,
      status: "same",
    }));
  }
  return trace.steps.flatMap((s) =>
    s.kind === "model"
      ? s.tool_requests.map((r) => ({
          tool: r.name,
          label: humanize(r.name),
          detail: detailOf(r.args),
          error: null,
          status: "same" as const,
        }))
      : [],
  );
}

/** Mark the actions only one run took: removed from the recorded run, added in the latest. */
export function compareActions(before: Action[], after: Action[]): { before: Action[]; after: Action[] } {
  const inBefore = new Set(before.map((a) => a.tool));
  const inAfter = new Set(after.map((a) => a.tool));
  return {
    before: before.map((a) => ({ ...a, status: inAfter.has(a.tool) ? "same" : "removed" })),
    after: after.map((a) => ({ ...a, status: inBefore.has(a.tool) ? "same" : "added" })),
  };
}
