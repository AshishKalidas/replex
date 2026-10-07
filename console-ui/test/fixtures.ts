import type { ReportPayload, Trace } from "../src/types";

export function trace(name: string, tools: string[], final = "Done."): Trace {
  const steps: Trace["steps"] = [];
  for (const t of tools) {
    steps.push({
      kind: "model", index: steps.length, model: "mock-sonnet", system: "Be careful.", input_messages_digest: "sha256:abc",
      input_messages: [{ role: "user", content: "refund 1042" }], text: "", tool_requests: [{ id: "t", name: t, args: {} }],
      stop_reason: "tool_use", usage: { input_tokens: 10, output_tokens: 5 }, latency_ms: 0, error: null, streamed: false,
      wire_format: "anthropic",
    });
    steps.push({ kind: "tool", index: steps.length, name: t, args: { order_id: "1042" }, result: { ok: true }, error: null, latency_ms: 1 });
  }
  steps.push({
    kind: "model", index: steps.length, model: "mock-sonnet", system: null, input_messages_digest: "sha256:def",
    input_messages: [{ role: "user", content: [{ type: "tool_result", content: "{\"ok\": true}" }] }], text: final,
    tool_requests: [], stop_reason: "end_turn", usage: { input_tokens: 20, output_tokens: 8 }, latency_ms: 0, error: null,
    streamed: true, wire_format: "anthropic",
  });
  return {
    name, provider: "mock", wire_format: "anthropic", model: "mock-sonnet", steps, final_output: final,
    usage: { input_tokens: 10 * tools.length + 20, output_tokens: 5 * tools.length + 8 }, total_latency_ms: tools.length, notes: [],
  };
}

export function payload(): ReportPayload {
  const baseline = trace("refund_request", ["get_order", "check_refund_policy"], "Refund approved.");
  const candidate = trace("refund_request", ["get_order", "search_docs"], "Refund approved quickly.");
  return {
    kind: "replex-report", replex_version: "0.1.0", generated_at: "2026-10-07T09:00:00Z",
    pairs: [{
      name: "refund_request", baseline, candidate,
      report: {
        name: "refund_request", passed: false, checks: {},
        findings: [
          { check: "tool_sequence", severity: "fail", message: "tool sequence changed", step_index: 3, before: [], after: [] },
          { check: "final_output", severity: "warn", message: "similarity 0.7", step_index: 4, before: "a", after: "b" },
        ],
        baseline: { name: "refund_request", provider: "mock", model: "mock-sonnet", steps: 5, tool_names: ["get_order", "check_refund_policy"], input_tokens: 40, output_tokens: 18, latency_ms: 2, final_output: "Refund approved." },
        candidate: { name: "refund_request", provider: "mock", model: "mock-sonnet", steps: 5, tool_names: ["get_order", "search_docs"], input_tokens: 40, output_tokens: 18, latency_ms: 2, final_output: "Refund approved quickly." },
        diff: { a_name: "refund_request", b_name: "refund_request", entries: [
          { status: "same", kind: "model", name: "model", a_index: 0, b_index: 0, changes: [] },
          { status: "removed", kind: "tool", name: "check_refund_policy", a_index: 3, b_index: null, changes: [] },
          { status: "added", kind: "tool", name: "search_docs", a_index: null, b_index: 3, changes: [] },
        ] },
      },
    }],
  };
}
