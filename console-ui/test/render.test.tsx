import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DiffText, DriftPill, JsonBlock, ToolChips } from "../src/components/common";
import { StepCard } from "../src/components/StepCard";
import { DiffRows } from "../src/views/CompareView";
import { wordDiff } from "../src/textdiff";
import { payload, trace } from "./fixtures";

describe("components render", () => {
  it("renders a model step with details", () => {
    const t = trace("x", ["get_order"]);
    const html = renderToStaticMarkup(<StepCard step={t.steps[0]!} expanded navIndex={0} />);
    expect(html).toContain("requests get_order");
    expect(html).toContain("System prompt");
    expect(html).toContain('data-nav-index="0"');
  });
  it("renders a tool step", () => {
    const t = trace("x", ["get_order"]);
    const html = renderToStaticMarkup(<StepCard step={t.steps[1]!} expanded />);
    expect(html).toContain("Arguments");
    expect(html).toContain("Result");
  });
  it("collapses long JSON", () => {
    const html = renderToStaticMarkup(<JsonBlock value={Array.from({ length: 40 }, (_, i) => i)} />);
    expect(html).toContain("code-collapsed");
    expect(html).toContain("Show all");
  });
  it("renders chips, pills and text diffs", () => {
    expect(renderToStaticMarkup(<ToolChips names={[]} />)).toContain("no tool calls");
    expect(renderToStaticMarkup(<ToolChips names={["a", "b"]} highlight={new Set(["b"])} />)).toContain("chip-hot");
    expect(renderToStaticMarkup(<DriftPill drift="failed" />)).toContain("drift");
    expect(renderToStaticMarkup(<DriftPill drift="passed" warnings={2} />)).toContain("2⚠");
    expect(renderToStaticMarkup(<DiffText pieces={wordDiff("a b", "a c")} />)).toMatch(/<del>b<\/del><ins>c<\/ins>/);
  });
  it("renders aligned diff rows", () => {
    const p = payload().pairs[0]!;
    const html = renderToStaticMarkup(
      <DiffRows result={{ a: p.baseline!, b: p.candidate!, diff: p.report.diff, report: p.report }} focusStep={3} />,
    );
    expect(html).toContain("diff-removed");
    expect(html).toContain("diff-added");
    expect(html).toContain("search_docs");
  });
});
