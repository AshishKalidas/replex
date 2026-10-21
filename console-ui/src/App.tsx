import { useMemo } from "react";
import type { DataSource, WorkspaceInfo } from "./api";
import { Kbd, Logo, Segmented } from "./components/common";
import { useRoute } from "./hooks";
import { href, type Route } from "./router";
import { CompareView } from "./views/CompareView";
import { OverviewView } from "./views/OverviewView";
import { RunsView } from "./views/RunsView";
import { ScenarioView } from "./views/ScenarioView";
import { TimelineView } from "./views/TimelineView";

function section(route: Route): "overview" | "runs" | "compare" {
  if (route.view === "compare") return "compare";
  if (route.view === "runs" || route.view === "run") return "runs";
  return "overview";
}

/** A workspace's short name in the switcher: "Project", "Sample", or what a further sample shows. */
export function switcherLabel(w: WorkspaceInfo): string {
  if (w.id === "project") return "Project";
  const subject = w.label.startsWith("Sample: ") ? w.label.slice("Sample: ".length) : "";
  return subject ? subject.charAt(0).toUpperCase() + subject.slice(1) : "Sample";
}

interface Props {
  source: DataSource;
  /** The workspace being shown, when served by `replex console`. */
  workspace?: WorkspaceInfo;
  workspaces?: WorkspaceInfo[];
  onSwitchWorkspace?: (id: string) => void;
}

export function App({ source, workspace, workspaces = [], onSwitchWorkspace }: Props) {
  const route = useRoute();
  const current = section(route);
  const view = useMemo(() => {
    const others = workspaces.filter((w) => w.id !== workspace?.id);
    switch (route.view) {
      case "overview":
        return <OverviewView source={source} workspace={workspace} workspaces={workspaces} onSwitchWorkspace={onSwitchWorkspace} />;
      case "scenario":
        return <ScenarioView key={route.name} source={source} name={route.name} />;
      case "runs":
        return (
          <RunsView
            source={source}
            initialQuery={route.q}
            workspace={workspace}
            onSwitchWorkspace={onSwitchWorkspace}
            alternatives={others}
          />
        );
      case "run":
        return <TimelineView key={route.id} source={source} id={route.id} step={route.step} />;
      case "compare":
        return <CompareView source={source} a={route.a} b={route.b} step={route.step} />;
    }
  }, [route, source, workspace, workspaces, onSwitchWorkspace]);

  const tabs = [
    { id: "overview", label: "Overview", to: href({ view: "overview" }) },
    { id: "runs", label: "Runs", to: href({ view: "runs", q: "" }) },
    { id: "compare", label: "Compare", to: href({ view: "compare", a: null, b: null, step: null }) },
  ] as const;

  return (
    <div className="app">
      <header className="globalnav">
        <div className="globalnav-inner">
          <a className="brand" href={href({ view: "overview" })}>
            <Logo />
            <span>RepleX</span>
          </a>
          <nav className="tabs" aria-label="Views">
            {tabs.map((t) => (
              <a key={t.id} href={t.to} aria-current={current === t.id ? "page" : undefined}>
                {t.label}
              </a>
            ))}
          </nav>
          <div className="globalnav-end">
            {workspaces.length > 1 && onSwitchWorkspace ? (
              <Segmented
                label="Workspace"
                value={workspace?.id ?? ""}
                options={workspaces.map((w) => ({ id: w.id, label: switcherLabel(w) }))}
                onChange={onSwitchWorkspace}
              />
            ) : (
              <span className="nav-note">{source.label}</span>
            )}
          </div>
        </div>
      </header>
      <main className="content">{view}</main>
      <footer className="footer">
        <span>
          <Kbd>j</Kbd> <Kbd>k</Kbd> to move, <Kbd>Enter</Kbd> to open, <Kbd>/</Kbd> to search
        </span>
        <span>Runs on this machine only. Secrets are redacted before anything is saved.</span>
      </footer>
    </div>
  );
}
