import { describe, expect, it } from "vitest";
import type { WorkspaceInfo } from "../src/api";
import { switcherLabel } from "../src/App";

function workspace(id: string, label: string): WorkspaceInfo {
  return { id, label, baseline_dir: "", candidate_dir: "", policy: null, baselines: 0, candidates: 0, writable: false, description: null };
}

describe("workspace switcher", () => {
  it("names the project, the sample and further samples by their subject", () => {
    expect(switcherLabel(workspace("project", "Your project"))).toBe("Project");
    expect(switcherLabel(workspace("sample", "Sample workspace"))).toBe("Sample");
    expect(switcherLabel(workspace("sample-models", "Sample: model swap"))).toBe("Model swap");
  });
});
