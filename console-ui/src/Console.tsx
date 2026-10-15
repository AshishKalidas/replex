import { useEffect, useMemo, useState } from "react";
import { listWorkspaces, serverSource, type WorkspaceInfo } from "./api";
import { App } from "./App";
import { ErrorState, Loading } from "./components/common";
import { href } from "./router";

const STORAGE_KEY = "replex.workspace";

function remembered(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function remember(id: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Private windows and blocked storage: the choice just isn't kept.
  }
}

/**
 * The console served by `replex console`: picks a workspace (your project or the bundled
 * sample) and re-reads it whenever the window regains focus, so new recordings and drift runs
 * show up without a reload.
 */
export function Console() {
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    listWorkspaces().then(
      (data) => {
        setWorkspaces(data.workspaces);
        const saved = remembered();
        setActive(data.workspaces.some((w) => w.id === saved) ? saved : data.default);
      },
      (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
    );
  }, []);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") setRevision((n) => n + 1);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  const workspace = workspaces?.find((w) => w.id === active) ?? null;
  // A new source object per revision makes every view re-read its data in place.
  const source = useMemo(() => (workspace ? serverSource(workspace) : null), [workspace, revision]);

  if (error) return <ErrorState error={`The console server did not answer: ${error}`} />;
  if (!workspaces || !source || !workspace) return <Loading what="workspaces" />;

  const switchTo = (id: string) => {
    if (id === active) return;
    remember(id);
    setActive(id);
    window.location.hash = href({ view: "overview" });
  };

  return (
    <App
      key={workspace.id}
      source={source}
      workspace={workspace}
      workspaces={workspaces}
      onSwitchWorkspace={switchTo}
    />
  );
}
