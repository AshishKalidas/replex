// Mirrors of the Python models (replex.trace.model, replex.drift.*). Keep in sync.

export interface Usage {
  input_tokens: number;
  output_tokens: number;
}

export interface ToolRequest {
  id: string | null;
  name: string;
  args: Record<string, unknown>;
}

export interface ModelStep {
  kind: "model";
  index: number;
  model: string;
  system: string | null;
  input_messages_digest: string;
  input_messages: unknown[];
  text: string;
  tool_requests: ToolRequest[];
  stop_reason: string;
  usage: Usage;
  latency_ms: number;
  error: string | null;
  streamed: boolean;
  wire_format: string;
  /** US dollars the provider reported, when it did. */
  reported_cost?: number | null;
  /** US dollars; null when the model has no known price. Absent in older reports. */
  cost?: number | null;
}

export interface ToolStep {
  kind: "tool";
  index: number;
  name: string;
  args: Record<string, unknown>;
  result: unknown;
  error: string | null;
  latency_ms: number;
}

export type Step = ModelStep | ToolStep;

export interface Trace {
  name: string;
  provider: string;
  wire_format: string;
  model: string;
  steps: Step[];
  final_output: string;
  usage: Usage;
  cost?: number | null;
  unpriced_models?: string[];
  total_latency_ms: number;
  notes: string[];
}

export type DiffStatus = "same" | "changed" | "added" | "removed";

export interface FieldChange {
  path: string;
  before: unknown;
  after: unknown;
}

export interface DiffEntry {
  status: DiffStatus;
  kind: "model" | "tool";
  name: string;
  a_index: number | null;
  b_index: number | null;
  changes: FieldChange[];
}

export interface TraceDiff {
  a_name: string;
  b_name: string;
  entries: DiffEntry[];
}

export type Severity = "fail" | "warn" | "info";

export interface Finding {
  check: string;
  severity: Severity;
  message: string;
  step_index: number | null;
  before: unknown;
  after: unknown;
  /** The judge model's explanation, for final_output in judge mode. */
  reason?: string | null;
}

export interface TraceSummary {
  name: string;
  provider: string;
  model: string;
  steps: number;
  tool_names: string[];
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  final_output: string;
  cost?: number | null;
  unpriced_models?: string[];
}

export interface DriftReport {
  name: string;
  passed: boolean;
  findings: Finding[];
  baseline: TraceSummary;
  candidate: TraceSummary;
  diff: TraceDiff;
  checks: Record<string, unknown>;
}

export type RunKind = "baseline" | "candidate";

export interface RunSummary {
  id: string;
  kind: RunKind;
  name: string;
  provider?: string;
  model?: string;
  created_at?: string;
  steps?: number;
  tool_calls?: number;
  tool_names?: string[];
  tokens?: number;
  cost?: number | null;
  latency_ms?: number;
  drift?: "passed" | "failed" | null;
  warnings?: number;
  error?: string;
}

export interface CompareResult {
  a: Trace;
  b: Trace;
  diff: TraceDiff;
  report: DriftReport | null;
}

export interface DriftResult {
  name: string;
  report: DriftReport;
  baseline: Trace;
  candidate: Trace;
}

/** The JSON embedded in a single-file HTML report. */
export interface ReportPayload {
  kind: "replex-report";
  replex_version: string;
  generated_at: string;
  pairs: { name: string; report: DriftReport; baseline: Trace | null; candidate: Trace | null }[];
}
