import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DiffText, DriftStatus, EmptyState, JsonBlock, Segmented, ToolChips } from "../src/components/common";
import { StepCard } from "../src/components/StepCard";
import { DiffRows } from "../src/views/CompareView";
import { wordDiff } from "../src/textdiff";
import { payload, trace } from "./fixtures";

describe("components render", () => {
  it("renders a model step with details", () => {
    const t = trace("x", ["get_order"]);
    const html = renderToStaticMarkup(<StepCard step={t.steps[0]!} expanded navIndex={0} />);
    expect(html).toContain("Asks for get_order");
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
    expect(renderToStaticMarkup(<ToolChips names={[]} />)).toContain("No tool calls");
    expect(renderToStaticMarkup(<ToolChips names={["a", "b"]} highlight={new Set(["b"])} />)).toContain("chip-new");
    expect(renderToStaticMarkup(<DriftStatus drift="failed" />)).toContain("Behaviour changed");
    expect(renderToStaticMarkup(<DriftStatus drift="passed" warnings={2} />)).toContain("Small differences");
    expect(renderToStaticMarkup(<DriftStatus drift="passed" />)).toContain("Behaves as recorded");
    expect(renderToStaticMarkup(<DriftStatus drift={null} />)).toContain("Not re-run yet");
    expect(renderToStaticMarkup(<DiffText pieces={wordDiff("a b", "a c")} />)).toMatch(/<del>b<\/del><ins>c<\/ins>/);
  });
  it("renders the workspace switcher and empty states", () => {
    const html = renderToStaticMarkup(
      <Segmented
        label="Workspace"
        value="sample"
        options={[
          { id: "project", label: "Your project" },
          { id: "sample", label: "Sample" },
        ]}
        onChange={() => undefined}
      />,
    );
    expect(html).toContain('role="radiogroup"');
    expect(html).toMatch(/aria-checked="true"[^>]*>Sample</);
    expect(renderToStaticMarkup(<EmptyState title="No recordings yet." action={<button>Go</button>} />)).toContain(
      "state-action",
    );
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
