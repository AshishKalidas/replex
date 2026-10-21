import { formatDate } from "./format";
import type {
  CompareResult,
  ConsistencyReport,
  DriftResult,
  HistoryData,
  ReportPayload,
  RunSummary,
  Trace,
} from "./types";

/** Where the console gets its data: the local server, or JSON inlined in a report file. */
export interface DataSource {
  mode: "server" | "static";
  label: string;
  /** Workspace id on the server ("project" or "sample"); null for a static report. */
  workspace: string | null;
  runs(): Promise<RunSummary[]>;
  run(id: string): Promise<Trace>;
  compare(a: string, b: string): Promise<CompareResult>;
  drift(name: string): Promise<DriftResult>;
  /** Repeated runs of a scenario grouped by path, or null when it was not run repeatedly. */
  consistency(name: string): Promise<ConsistencyReport | null>;
  /** Whether two runs can be compared (a static report only holds its baseline/candidate pairs). */
  comparable(a: string, b: string): boolean;
  /** Whether this source can accept or discard candidates (only your project, never a report). */
  writable: boolean;
  /** Make the latest run of a scenario its new baseline. */
  accept(name: string): Promise<void>;
  /** Throw away the latest run of a scenario and keep the baseline. */
  discard(name: string): Promise<void>;
  /** Past drift checks and decisions, for every scenario or only `name`. */
  history(name?: string): Promise<HistoryData>;
}

/** What a source without a history file (a static report) answers. */
export const NO_HISTORY: HistoryData = {
  enabled: false,
  limit: 0,
  trend: { days: 7, since: null, until: null, checks: 0, passed: 0, failed: 0, accepted: 0, rejected: 0, scenarios: 0 },
  scenarios: [],
};

export function splitRunId(id: string): { kind: string; name: string } {
  const at = id.indexOf(":");
  return at < 0 ? { kind: "", name: id } : { kind: id.slice(0, at), name: id.slice(at + 1) };
}

export function summarise(id: string, trace: Trace, drift: RunSummary["drift"], warnings = 0): RunSummary {
  const { kind, name } = splitRunId(id);
  const tools = trace.steps.filter((s) => s.kind === "tool");
  const toolNames = tools.length
    ? tools.map((s) => (s.kind === "tool" ? s.name : ""))
    : trace.steps.flatMap((s) => (s.kind === "model" ? s.tool_requests.map((r) => r.name) : []));
  return {
    id,
    kind: kind === "candidate" ? "candidate" : "baseline",
    name,
    provider: trace.provider,
    model: trace.model,
    steps: trace.steps.length,
    tool_calls: tools.length,
    tool_names: toolNames,
    tokens: trace.usage.input_tokens + trace.usage.output_tokens,
    cost: trace.cost ?? null,
    latency_ms: trace.total_latency_ms,
    drift,
    warnings,
  };
}

export function staticSource(payload: ReportPayload): DataSource {
  const traces = new Map<string, Trace>();
  for (const pair of payload.pairs) {
    if (pair.baseline) traces.set(`baseline:${pair.name}`, pair.baseline);
    if (pair.candidate) traces.set(`candidate:${pair.name}`, pair.candidate);
  }
  const pairFor = (name: string) => payload.pairs.find((p) => p.name === name);
  const missing = (what: string) => Promise.reject(new Error(`${what} is not part of this report`));

  return {
    mode: "static",
    label: `Report from ${formatDate(payload.generated_at)} UTC`,
    workspace: null,
    writable: false,
    consistency: () => Promise.resolve(null),
    accept: () => missing("accepting changes"),
    discard: () => missing("discarding changes"),
    history: () => Promise.resolve(NO_HISTORY),
    async runs() {
      const rows: RunSummary[] = [];
      for (const pair of payload.pairs) {
        const status = pair.report.passed ? "passed" : "failed";
        const warnings = pair.report.findings.filter((f) => f.severity === "warn").length;
        if (pair.baseline) rows.push(summarise(`baseline:${pair.name}`, pair.baseline, status, warnings));
        if (pair.candidate) rows.push(summarise(`candidate:${pair.name}`, pair.candidate, status, warnings));
      }
      return rows;
    },
    run(id) {
      const trace = traces.get(id);
      return trace ? Promise.resolve(trace) : missing(id);
    },
    comparable(a, b) {
      const x = splitRunId(a);
      const y = splitRunId(b);
      return x.name === y.name && x.kind === "baseline" && y.kind === "candidate" && !!pairFor(x.name);
    },
    compare(a, b) {
      const { name } = splitRunId(a);
      const pair = pairFor(name);
      if (!pair || !pair.baseline || !pair.candidate || !this.comparable(a, b)) {
        return missing(`the comparison ${a} ↔ ${b}`);
      }
      return Promise.resolve({ a: pair.baseline, b: pair.candidate, diff: pair.report.diff, report: pair.report });
    },
    drift(name) {
      const pair = pairFor(name);
      if (!pair || !pair.baseline || !pair.candidate) return missing(`drift for ${name}`);
      return Promise.resolve({ name, report: pair.report, baseline: pair.baseline, candidate: pair.candidate });
    },
  };
}

async function getJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, headers: { accept: "application/json", ...init.headers } });
  const body = (await response.json().catch(() => ({}))) as { error?: string };
  if (!response.ok) throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  return body as T;
}

/** A workspace the server offers: your project, or the bundled sample. */
export interface WorkspaceInfo {
  id: string;
  label: string;
  baseline_dir: string;
  candidate_dir: string;
  policy: string | null;
  baselines: number;
  candidates: number;
  writable: boolean;
}

export async function listWorkspaces(base = ""): Promise<{ workspaces: WorkspaceInfo[]; default: string }> {
  return getJson(`${base}/api/workspaces`);
}

export function serverSource(workspace: WorkspaceInfo, base = ""): DataSource {
  const api = `${base}/api/w/${encodeURIComponent(workspace.id)}`;
  return {
    mode: "server",
    label: workspace.label,
    workspace: workspace.id,
    async runs() {
      return (await getJson<{ runs: RunSummary[] }>(`${api}/runs`)).runs;
    },
    async run(id) {
      const { kind, name } = splitRunId(id);
      const data = await getJson<{ trace: Trace }>(
        `${api}/runs/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
      );
      return data.trace;
    },
    comparable: () => true,
    compare(a, b) {
      const q = new URLSearchParams({ a, b });
      return getJson<CompareResult>(`${api}/compare?${q.toString()}`);
    },
    drift(name) {
      return getJson<DriftResult>(`${api}/drift/${encodeURIComponent(name)}`);
    },
    async consistency(name) {
      const data = await getJson<{ report: ConsistencyReport | null }>(`${api}/consistency/${encodeURIComponent(name)}`);
      return data.report;
    },
    writable: workspace.writable,
    async accept(name) {
      await getJson(`${api}/scenarios/${encodeURIComponent(name)}/accept`, POST);
    },
    async discard(name) {
      await getJson(`${api}/scenarios/${encodeURIComponent(name)}/discard`, POST);
    },
    history(name) {
      const q = name === undefined ? "" : `?${new URLSearchParams({ name }).toString()}`;
      return getJson<HistoryData>(`${api}/history${q}`);
    },
  };
}

// The custom header makes the browser preflight any cross-origin attempt, which the server refuses.
const POST: RequestInit = { method: "POST", headers: { "x-replex-action": "1" } };

/** The inlined report data of a single-file HTML report, or null when served by the console. */
export function detectReport(doc: Pick<Document, "getElementById">): DataSource | null {
  const el = doc.getElementById("replex-data");
  if (el?.textContent) {
    const payload = JSON.parse(el.textContent) as ReportPayload;
    if (payload.kind === "replex-report") return staticSource(payload);
  }
  return null;
}
