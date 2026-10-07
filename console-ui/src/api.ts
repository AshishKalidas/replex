import type {
  CompareResult,
  DriftResult,
  ReportPayload,
  RunSummary,
  Trace,
} from "./types";

/** Where the console gets its data: the local server, or JSON inlined in a report file. */
export interface DataSource {
  mode: "server" | "static";
  label: string;
  runs(): Promise<RunSummary[]>;
  run(id: string): Promise<Trace>;
  compare(a: string, b: string): Promise<CompareResult>;
  drift(name: string): Promise<DriftResult>;
  /** Whether two runs can be compared (a static report only holds its baseline/candidate pairs). */
  comparable(a: string, b: string): boolean;
}

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
    label: `Report · ${payload.generated_at}`,
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

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  const body = (await response.json().catch(() => ({}))) as { error?: string };
  if (!response.ok) throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  return body as T;
}

export function serverSource(base = ""): DataSource {
  return {
    mode: "server",
    label: "Local console",
    async runs() {
      return (await getJson<{ runs: RunSummary[] }>(`${base}/api/runs`)).runs;
    },
    async run(id) {
      const { kind, name } = splitRunId(id);
      const data = await getJson<{ trace: Trace }>(
        `${base}/api/runs/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
      );
      return data.trace;
    },
    comparable: () => true,
    compare(a, b) {
      const q = new URLSearchParams({ a, b });
      return getJson<CompareResult>(`${base}/api/compare?${q.toString()}`);
    },
    drift(name) {
      return getJson<DriftResult>(`${base}/api/drift/${encodeURIComponent(name)}`);
    },
  };
}

/** Use inlined report data when present (single-file HTML report), otherwise the server. */
export function detectSource(doc: Pick<Document, "getElementById">): DataSource {
  const el = doc.getElementById("replex-data");
  if (el?.textContent) {
    const payload = JSON.parse(el.textContent) as ReportPayload;
    if (payload.kind === "replex-report") return staticSource(payload);
  }
  return serverSource();
}
