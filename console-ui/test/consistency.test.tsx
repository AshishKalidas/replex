import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { serverSource, staticSource } from "../src/api";
import { consistencyHeadline, spreadText, variantPath, variantShare } from "../src/consistency";
import { formatMs } from "../src/format";
import type { ConsistencyReport, PathVariant } from "../src/types";
import { ConsistencyPanel } from "../src/views/Consistency";
import { payload } from "./fixtures";

const POLICY = ["get_order", "check_refund_policy", "create_refund_request"];

function variant(tools: string[], runs: number, total: number, matches = false, category: string | null = null): PathVariant {
  return {
    tools,
    category,
    runs: Array.from({ length: runs }, (_, i) => `run-${i + 1}`),
    share: runs / total,
    matches_baseline: matches,
  };
}

function report(variants: PathVariant[], baselineShare: number | null): ConsistencyReport {
  const runs = variants.reduce((n, v) => n + v.runs.length, 0);
  return {
    name: "refund_request",
    runs,
    distinct_paths: variants.length,
    variants,
    most_common: variants[0]!.tools,
    baseline_tools: baselineShare === null ? null : POLICY,
    baseline_category: null,
    baseline_share: baselineShare,
    baseline_is_majority: baselineShare === null ? null : baselineShare > 0.5,
    tokens: { min: 1820, median: 1950, max: 2410 },
    latency_ms: { min: 3100, median: 3600, max: 5400 },
    steps: { min: 7, median: 7, max: 7 },
    categorised: false,
  };
}

describe("consistency wording", () => {
  it("names paths and shares in plain language", () => {
    const v = variant(POLICY, 7, 10, true);
    expect(`${variantShare(v, 10)}: ${variantPath(v)}`).toBe(
      "7 of 10 runs: Get order › Check refund policy › Create refund request",
    );
    expect(variantPath(variant([], 1, 1))).toBe("Used no tools");
    expect(variantPath(variant(["get_order"], 1, 1, false, "refund_denied"))).toBe("Get order, answering “refund_denied”");
    expect(variantShare(variant([], 1, 1), 1)).toBe("1 of 1 run");
  });

  it("sums up how consistent the runs were", () => {
    const most = report([variant(POLICY, 7, 10, true), variant(["get_order"], 3, 10)], 0.7);
    expect(consistencyHeadline(most)).toBe("The agent took 2 different paths in 10 runs. Most follow the recorded path (7 of 10 runs).");
    const few = report([variant(["get_order"], 7, 10), variant(POLICY, 3, 10, true)], 0.3);
    expect(consistencyHeadline(few)).toBe("The agent took 2 different paths in 10 runs. Only 3 of 10 runs follow the recorded path.");
    const none = report([variant(["a"], 1, 2), variant(["b"], 1, 2)], 0);
    expect(consistencyHeadline(none)).toBe("The agent took 2 different paths in 2 runs. None of them matches the recording.");
    expect(consistencyHeadline(report([variant(["a"], 1, 2), variant(["b"], 1, 2)], null))).toBe(
      "The agent took 2 different paths in 2 runs.",
    );
    expect(consistencyHeadline(report([variant(POLICY, 5, 5, true)], 1))).toBe("All 5 runs took the same path: the recorded one.");
    expect(consistencyHeadline(report([variant(POLICY, 5, 5)], 0))).toBe("All 5 runs took the same path, but not the recorded one.");
    expect(consistencyHeadline(report([variant(POLICY, 5, 5)], null))).toBe("All 5 runs took the same path.");
    expect(consistencyHeadline(report([variant(POLICY, 1, 1, true)], 1))).toBe("The one run took the recorded path.");
    expect(consistencyHeadline(report([variant(POLICY, 1, 1)], 0))).toBe("The one run took a different path from the recording.");
    expect(consistencyHeadline(report([variant(POLICY, 1, 1)], null))).toBe("The one run took a single path.");
  });

  it("describes spreads", () => {
    expect(spreadText({ min: 3100, median: 3600, max: 5400 }, formatMs)).toBe("3.1 s to 5.4 s (typically 3.6 s)");
    expect(spreadText({ min: 7, median: 7, max: 7 }, String)).toBe("7");
  });
});

describe("consistency panel and data", () => {
  it("renders every path with its share", () => {
    const html = renderToStaticMarkup(
      <ConsistencyPanel report={report([variant(POLICY, 7, 10, true), variant(["get_order"], 3, 10)], 0.7)} />,
    );
    expect(html).toContain("How consistent is it?");
    expect(html).toContain("7 of 10 runs");
    expect(html).toContain("Get order › Check refund policy › Create refund request");
    expect(html).toContain("Recorded path");
    expect(html).toContain("width:70%");
    expect(html).toContain("1,820 to 2,410 (typically 1,950)");
  });

  it("is absent from reports and fetched from the server", async () => {
    expect(await staticSource(payload()).consistency("refund_request")).toBeNull();
    const body = { name: "x", report: report([variant(POLICY, 1, 1, true)], 1) };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const source = serverSource({
      id: "project", label: "Your project", baseline_dir: "", candidate_dir: "", policy: null,
      baselines: 1, candidates: 0, writable: true, description: null,
    });
    expect((await source.consistency("refund request"))?.runs).toBe(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/w/project/consistency/refund%20request", expect.anything());
    vi.unstubAllGlobals();
  });
});
