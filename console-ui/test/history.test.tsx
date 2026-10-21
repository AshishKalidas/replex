import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NO_HISTORY, serverSource, staticSource } from "../src/api";
import { HistoryTimeline, ScenarioHistoryView } from "../src/components/History";
import { describeEntry, markerOf, passSummary, shortDay, trendSentence } from "../src/history";
import type { CheckEntry, DecisionEntry, ScenarioHistory } from "../src/types";
import { payload } from "./fixtures";

function check(verdict: "passed" | "failed", at: string): CheckEntry {
  return {
    kind: "check", at, name: "refund_request", verdict, model: "mock-sonnet", baseline_model: "mock-sonnet",
    fail: verdict === "failed" ? { tool_sequence: 1, new_tools: 1 } : {}, warn: {},
    tools: verdict === "failed" ? ["get_order", "search_docs"] : ["get_order", "check_refund_policy"],
    steps: 5, tokens: 100, latency_ms: 2,
  };
}

const accepted: DecisionEntry = { kind: "accepted", at: "2026-10-21T10:00:00Z", name: "refund_request", via: "console" };
const rejected: DecisionEntry = { kind: "rejected", at: "2026-10-20T10:00:00Z", name: "refund_request", via: "cli" };

const scenario: ScenarioHistory = {
  name: "refund_request", checks: 2, passed: 1, failed: 1, pass_rate: 0.5, total_checks: 2,
  last_check_at: "2026-10-20T09:00:00Z", last_change_at: "2026-10-20T09:00:00Z", last_decision: accepted,
  events: [check("passed", "2026-10-19T09:00:00Z"), check("failed", "2026-10-20T09:00:00Z"), rejected, accepted],
};

describe("history wording", () => {
  it("names each event and describes it", () => {
    expect(scenario.events.map(markerOf)).toEqual(["passed", "changed", "rejected", "accepted"]);
    expect(describeEntry(scenario.events[0]!)).toBe("Used get_order → check_refund_policy.");
    expect(describeEntry(scenario.events[1]!)).toBe("Used get_order → search_docs. Checks that failed: new_tools, tool_sequence.");
    expect(describeEntry({ ...check("failed", "2026-10-20T09:00:00Z"), fail: {}, tools: [] })).toBe(
      "Used no tools. Checks that failed: none.",
    );
    expect(describeEntry(accepted)).toContain("accepted as the new recording in the console");
    expect(describeEntry(rejected)).toContain("rejected with replex approve");
  });
  it("summarises pass rates and the weekly trend", () => {
    expect(passSummary(scenario)).toBe("1 of 2 recent checks behaved as recorded.");
    expect(passSummary({ ...scenario, checks: 0 })).toBe("No drift checks recorded yet.");
    expect(shortDay("2026-10-01T09:00:00Z")).toBe("1 Oct");
    expect(shortDay(null)).toBe("—");
    expect(trendSentence(NO_HISTORY.trend)).toBeNull();
    expect(trendSentence({ ...NO_HISTORY.trend, checks: 3, passed: 2, failed: 1, accepted: 1, until: "2026-10-21T10:00:00Z" })).toBe(
      "3 checks in the 7 days to 21 Oct: 1 found changed behaviour, 1 change accepted.",
    );
    expect(trendSentence({ ...NO_HISTORY.trend, checks: 1, passed: 1, rejected: 2, until: "2026-10-21T10:00:00Z" })).toBe(
      "1 check in the 7 days to 21 Oct: 0 found changed behaviour, 2 changes rejected.",
    );
  });
});

describe("history views", () => {
  it("draws one marker per event, with a text equivalent", () => {
    const html = renderToStaticMarkup(<ScenarioHistoryView history={scenario} />);
    expect(html.match(/class="history-mark history-/g)).toHaveLength(4 + 4); // timeline + legend
    expect(html).toContain('role="img"');
    expect(html).toContain("Timeline of 4 events. 1 of 2 recent checks behaved as recorded.");
    expect(html).toContain("<title>Behaviour changed, 2026-10-20 09:00 UTC</title>");
    expect(html.indexOf("Change accepted</span>")).toBeLessThan(html.indexOf("Behaves as recorded</span>"));
    expect(html).toContain("19 Oct");
  });
  it("centres a single event", () => {
    const html = renderToStaticMarkup(<HistoryTimeline events={[accepted]} label="one" />);
    expect(html).toContain('text-anchor="middle"');
  });
  it("is fetched per workspace, and empty in a static report", async () => {
    const urls: string[] = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (url: string) => {
      urls.push(url);
      return new Response(JSON.stringify(NO_HISTORY), { status: 200 });
    }) as typeof fetch;
    try {
      const source = serverSource({
        id: "project", label: "Your project", baseline_dir: "b", candidate_dir: "c", policy: null,
        baselines: 0, candidates: 0, writable: true,
      });
      await source.history();
      await source.history("refund request");
    } finally {
      globalThis.fetch = realFetch;
    }
    expect(urls).toEqual(["/api/w/project/history", "/api/w/project/history?name=refund+request"]);
    expect((await staticSource(payload()).history()).enabled).toBe(false);
  });
});
