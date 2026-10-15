import { describe, expect, it } from "vitest";
import { detectReport, serverSource, splitRunId, staticSource, summarise } from "../src/api";
import { explain, headline, humanize, scenarios, verdictOf } from "../src/explain";
import { filterRuns, formatDate, formatMs, formatTokens, inlineJson, lastMessageText, stepSubtitle, stepTitle, truncate } from "../src/format";
import { href, parseRoute, type Route } from "../src/router";
import { readableDiff, tokenize, wordDiff } from "../src/textdiff";
import { payload, trace } from "./fixtures";

describe("router", () => {
  const routes: Route[] = [
    { view: "runs", q: "" },
    { view: "runs", q: "get order" },
    { view: "run", id: "baseline:refund_request", step: null },
    { view: "run", id: "candidate:a/b", step: 3 },
    { view: "compare", a: "baseline:x", b: "candidate:x", step: 2 },
    { view: "compare", a: null, b: null, step: null },
    { view: "overview" },
    { view: "scenario", name: "refund request" },
  ];
  it.each(routes)("round-trips %o", (route) => {
    expect(parseRoute(href(route))).toEqual(route);
  });
  it("defaults to the overview, keeps old drift links working and ignores bad steps", () => {
    expect(parseRoute("")).toEqual({ view: "overview" });
    expect(parseRoute("#/nonsense")).toEqual({ view: "overview" });
    expect(parseRoute("#/drift")).toEqual({ view: "overview" });
    expect(parseRoute("#/drift/refund_request")).toEqual({ view: "scenario", name: "refund_request" });
    expect(parseRoute("#/run/baseline/x?step=-1")).toEqual({ view: "run", id: "baseline:x", step: null });
    expect(parseRoute("#/run/baseline/x?step=abc")).toEqual({ view: "run", id: "baseline:x", step: null });
  });
});

describe("format", () => {
  it("formats numbers, durations and dates", () => {
    expect(formatTokens(undefined)).toBe("—");
    expect(formatTokens(1234)).toBe("1,234");
    expect(formatTokens(12345)).toBe("12.3k");
    expect(formatMs(812)).toBe("812 ms");
    expect(formatMs(1500)).toBe("1.5 s");
    expect(formatMs(25000)).toBe("25 s");
    expect(formatDate("2026-10-07T09:00:00Z")).toBe("2026-10-07 09:00");
    expect(formatDate("yesterday")).toBe("yesterday");
    expect(truncate("abcdef", 4)).toBe("abc…");
    expect(inlineJson({ a: 1 })).toBe('{"a":1}');
  });
  it("describes steps", () => {
    const t = trace("x", ["get_order"]);
    expect(stepTitle(t.steps[0]!)).toBe("Asks for get_order");
    expect(stepTitle(t.steps[1]!)).toBe("get_order");
    expect(stepSubtitle(t.steps[1]!)).toBe('{"order_id":"1042"}');
    expect(stepSubtitle(t.steps[2]!)).toBe("Done.");
    expect(lastMessageText(t.steps[2]!.kind === "model" ? t.steps[2]!.input_messages : [])).toBe('{"ok": true}');
    expect(lastMessageText([{ role: "user", content: "hi" }])).toBe("hi");
    expect(lastMessageText([])).toBe("");
  });
  it("filters runs by text and kind", async () => {
    const runs = await staticSource(payload()).runs();
    expect(filterRuns(runs, "", "all")).toHaveLength(2);
    expect(filterRuns(runs, "search_docs", "all").map((r) => r.kind)).toEqual(["candidate"]);
    expect(filterRuns(runs, "", "baseline")).toHaveLength(1);
    expect(filterRuns(runs, "", "failed")).toHaveLength(2);
    expect(filterRuns(runs, "nope", "all")).toHaveLength(0);
  });
});

describe("textdiff", () => {
  it("tokenizes words, spaces and punctuation", () => {
    expect(tokenize("Hi, there!")).toEqual(["Hi", ",", " ", "there", "!"]);
  });
  it("produces a word-level diff", () => {
    expect(wordDiff("Refund approved.", "Refund approved quickly.")).toEqual([
      { op: "same", text: "Refund approved" },
      { op: "add", text: " quickly" },
      { op: "same", text: "." },
    ]);
    expect(wordDiff("a", "a")).toEqual([{ op: "same", text: "a" }]);
    expect(wordDiff("", "new")).toEqual([{ op: "add", text: "new" }]);
  });
  it("replaces wholesale when texts share little", () => {
    expect(readableDiff("Refund approved.", "Refund approved quickly.")).toEqual(wordDiff("Refund approved.", "Refund approved quickly."));
    expect(readableDiff("I have requested your refund", "Done! It is on its way")).toEqual([
      { op: "del", text: "I have requested your refund" },
      { op: "add", text: "\nDone! It is on its way" },
    ]);
    expect(readableDiff("", "x")).toEqual([{ op: "add", text: "x" }]);
  });
  it("falls back for very long texts", () => {
    const long = "word ".repeat(1000);
    expect(wordDiff(long, "x")).toEqual([{ op: "del", text: long }, { op: "add", text: "x" }]);
  });
});

describe("data sources", () => {
  it("serves a static report", async () => {
    const source = staticSource(payload());
    expect(source.mode).toBe("static");
    const runs = await source.runs();
    expect(runs.map((r) => r.id)).toEqual(["baseline:refund_request", "candidate:refund_request"]);
    expect(runs[0]).toMatchObject({ steps: 5, tool_calls: 2, tokens: 58, drift: "failed", warnings: 1 });
    expect((await source.run("candidate:refund_request")).final_output).toBe("Refund approved quickly.");
    await expect(source.run("baseline:other")).rejects.toThrow("not part of this report");
    expect(source.comparable("baseline:refund_request", "candidate:refund_request")).toBe(true);
    expect(source.comparable("candidate:refund_request", "baseline:refund_request")).toBe(false);
    const cmp = await source.compare("baseline:refund_request", "candidate:refund_request");
    expect(cmp.diff.entries).toHaveLength(3);
    await expect(source.compare("baseline:x", "candidate:x")).rejects.toThrow();
    expect((await source.drift("refund_request")).report.passed).toBe(false);
    await expect(source.drift("x")).rejects.toThrow();
  });
  it("detects inlined report data, otherwise defers to the server", () => {
    const withData = { getElementById: () => ({ textContent: JSON.stringify(payload()) }) } as unknown as Document;
    expect(detectReport(withData)?.mode).toBe("static");
    const without = { getElementById: () => null } as unknown as Document;
    expect(detectReport(without)).toBeNull();
  });
  it("scopes server requests to the chosen workspace", async () => {
    const urls: string[] = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (url: string) => {
      urls.push(url);
      return new Response(JSON.stringify({ runs: [], trace: {} }), { status: 200 });
    }) as typeof fetch;
    try {
      const source = serverSource({
        id: "sample",
        label: "Sample workspace",
        baseline_dir: "b",
        candidate_dir: "c",
        policy: null,
        baselines: 0,
        candidates: 0,
        writable: false,
      });
      expect(source.workspace).toBe("sample");
      await source.runs();
      await source.run("candidate:refund request");
      await source.drift("refund_request");
      await source.compare("baseline:a", "candidate:a");
    } finally {
      globalThis.fetch = realFetch;
    }
    expect(urls).toEqual([
      "/api/w/sample/runs",
      "/api/w/sample/runs/candidate/refund%20request",
      "/api/w/sample/drift/refund_request",
      "/api/w/sample/compare?a=baseline%3Aa&b=candidate%3Aa",
    ]);
  });
  it("splits ids and summarises traces without executed tools", () => {
    expect(splitRunId("candidate:a:b")).toEqual({ kind: "candidate", name: "a:b" });
    expect(splitRunId("plain")).toEqual({ kind: "", name: "plain" });
    const t = trace("x", ["a"]);
    t.steps = t.steps.filter((s) => s.kind === "model");
    expect(summarise("baseline:x", t, null).tool_names).toEqual(["a"]);
  });
});

describe("explain", () => {
  const finding = (check: string, severity: "fail" | "warn" | "info", before: unknown, after: unknown, message = "") => ({
    check,
    severity,
    message,
    step_index: 1,
    before,
    after,
  });
  const report = (findings: ReturnType<typeof finding>[], passed = !findings.some((f) => f.severity === "fail")) => ({
    ...payload().pairs[0]!.report,
    passed,
    findings,
  });

  it("names scenarios for people", () => {
    expect(humanize("refund_ineligible_openai")).toBe("Refund ineligible openai");
    expect(humanize("order-status")).toBe("Order status");
    expect(humanize("")).toBe("");
  });
  it("says which tools the agent stopped and started calling, once", () => {
    const r = report([
      finding("tool_sequence", "fail", ["get_order", "check_refund_policy"], ["get_order", "search_docs"]),
      finding("new_tools", "fail", ["check_refund_policy", "get_order"], ["search_docs"]),
      finding("latency", "info", 1, 2),
    ]);
    expect(explain(r)).toEqual(["Stopped calling check_refund_policy and started calling search_docs."]);
    expect(verdictOf(r)).toBe("changed");
    expect(headline(r)).toBe("Stopped calling check_refund_policy and started calling search_docs.");
  });
  it("covers every policy check in plain words", () => {
    const r = report([
      finding("tool_sequence", "fail", ["a", "b"], ["b", "a"]),
      finding("new_tools", "warn", [], ["c", "d", "e"]),
      finding("forbidden_tools", "fail", null, "delete_order"),
      finding("tool_args", "fail", { id: 1 }, { id: 2 }, "arguments of 'get_order' changed"),
      finding("final_output", "warn", "a", "b", "final output similarity 0.73 is below 0.80"),
      finding("steps", "warn", 5, 8),
      finding("tokens", "warn", 100, 180),
    ]);
    expect(explain(r)).toEqual([
      "Calls the same tools in a different order.",
      "Called delete_order, which the policy forbids.",
      "Calls get_order with different arguments.",
      "The final answer is worded differently (73% similar).",
      "Takes 8 steps where it used to take 5.",
      "Uses 80% more tokens.",
    ]);
    const alone = report([finding("new_tools", "fail", [], ["c", "d", "e"])]);
    expect(explain(alone)).toEqual(["Started calling c, d and e."]);
    const format = report([finding("final_output", "fail", "{}", "hi", "candidate final output is not valid JSON (json_schema)")]);
    expect(explain(format)).toEqual(["The final answer no longer has the required format."]);
  });
  it("gives a verdict for every state", () => {
    expect(verdictOf(null)).toBe("pending");
    expect(verdictOf(report([]))).toBe("same");
    expect(verdictOf(report([finding("steps", "warn", 5, 8)]))).toBe("minor");
    expect(headline(null)).toMatch(/not re-run/);
    expect(headline(report([]))).toMatch(/same thing/);
  });
  it("groups runs into scenarios, the ones needing review first", () => {
    const list = scenarios([
      { id: "baseline:b", kind: "baseline", name: "b" },
      { id: "baseline:a", kind: "baseline", name: "a" },
      { id: "candidate:a", kind: "candidate", name: "a", drift: "passed" },
      { id: "baseline:c", kind: "baseline", name: "c" },
      { id: "candidate:c", kind: "candidate", name: "c", drift: "failed" },
    ]);
    expect(list.map((s) => s.name)).toEqual(["c", "a", "b"]);
    expect(list[2]!.candidate).toBeNull();
  });
});
