import { describe, expect, it } from "vitest";
import { detectSource, splitRunId, staticSource, summarise } from "../src/api";
import { filterRuns, formatDate, formatMs, formatTokens, inlineJson, lastMessageText, stepSubtitle, stepTitle, truncate } from "../src/format";
import { href, parseRoute, type Route } from "../src/router";
import { tokenize, wordDiff } from "../src/textdiff";
import { payload, trace } from "./fixtures";

describe("router", () => {
  const routes: Route[] = [
    { view: "runs", q: "" },
    { view: "runs", q: "get order" },
    { view: "run", id: "baseline:refund_request", step: null },
    { view: "run", id: "candidate:a/b", step: 3 },
    { view: "compare", a: "baseline:x", b: "candidate:x", step: 2 },
    { view: "compare", a: null, b: null, step: null },
    { view: "drift-index" },
    { view: "drift", name: "refund request" },
  ];
  it.each(routes)("round-trips %o", (route) => {
    expect(parseRoute(href(route))).toEqual(route);
  });
  it("defaults to runs and ignores bad steps", () => {
    expect(parseRoute("")).toEqual({ view: "runs", q: "" });
    expect(parseRoute("#/nonsense")).toEqual({ view: "runs", q: "" });
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
    expect(stepTitle(t.steps[0]!)).toBe("requests get_order");
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
  it("detects inlined data, otherwise uses the server", () => {
    const withData = { getElementById: () => ({ textContent: JSON.stringify(payload()) }) } as unknown as Document;
    expect(detectSource(withData).mode).toBe("static");
    const without = { getElementById: () => null } as unknown as Document;
    expect(detectSource(without).mode).toBe("server");
  });
  it("splits ids and summarises traces without executed tools", () => {
    expect(splitRunId("candidate:a:b")).toEqual({ kind: "candidate", name: "a:b" });
    expect(splitRunId("plain")).toEqual({ kind: "", name: "plain" });
    const t = trace("x", ["a"]);
    t.steps = t.steps.filter((s) => s.kind === "model");
    expect(summarise("baseline:x", t, null).tool_names).toEqual(["a"]);
  });
});
