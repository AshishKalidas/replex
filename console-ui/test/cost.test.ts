import { describe, expect, it } from "vitest";
import { staticSource, summarise } from "../src/api";
import { costChange, costChangePct, formatCost } from "../src/cost";
import { explain } from "../src/explain";
import type { DriftReport } from "../src/types";
import { payload, trace } from "./fixtures";

function priced(baseline: number | null | undefined, candidate: number | null | undefined): DriftReport {
  const report = payload().pairs[0]!.report;
  return { ...report, baseline: { ...report.baseline, cost: baseline }, candidate: { ...report.candidate, cost: candidate } };
}

describe("cost", () => {
  it("formats dollars, unknown and missing costs", () => {
    expect(formatCost(undefined)).toBe("—");
    expect(formatCost(null)).toBe("Unknown");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(0.00004)).toBe("<$0.0001");
    expect(formatCost(0.0123)).toBe("$0.0123");
    expect(formatCost(1234.5)).toBe("$1,234.50");
  });
  it("describes the change between the recorded and the latest run", () => {
    expect(costChange(priced(0.05, 0.003))).toBe("Latest run costs 94% less.");
    expect(costChange(priced(0.01, 0.015))).toBe("Latest run costs 50% more.");
    expect(costChange(priced(0.01, 0.01))).toBeNull();
    expect(costChange(priced(0, 0.01))).toBeNull();
    expect(costChange(priced(null, 0.01))).toBeNull();
    expect(costChange(priced(undefined, undefined))).toBeNull();
    expect(costChangePct("1", 2)).toBeNull();
  });
  it("explains a cost finding and leaves reported-only ones out", () => {
    const report = priced(0.01, 0.02);
    const warn = { check: "cost", severity: "warn" as const, message: "", step_index: null, before: 0.01, after: 0.02 };
    expect(explain({ ...report, findings: [warn] })).toEqual(["Costs 100% more per run."]);
    expect(explain({ ...report, findings: [{ ...warn, before: 0 }] })).toEqual(["Costs more per run."]);
    expect(explain({ ...report, findings: [{ ...warn, severity: "info" }] })).toEqual([]);
  });
  it("carries the cost of each run into run summaries", async () => {
    expect(summarise("baseline:x", { ...trace("x", []), cost: 0.25 }, null).cost).toBe(0.25);
    expect(summarise("baseline:x", trace("x", []), null).cost).toBeNull();
    const runs = await staticSource(payload()).runs();
    expect(runs.map((r) => r.cost)).toEqual([null, null]);
  });
});
