import { useMemo } from "react";
import type { DataSource } from "./api";
import { Kbd } from "./components/common";
import { useRoute } from "./hooks";
import { href, type Route } from "./router";
import { CompareView } from "./views/CompareView";
import { DriftIndex, DriftView } from "./views/DriftView";
import { RunsView } from "./views/RunsView";
import { TimelineView } from "./views/TimelineView";

function section(route: Route): "runs" | "compare" | "drift" {
  if (route.view === "compare") return "compare";
  if (route.view === "drift" || route.view === "drift-index") return "drift";
  return "runs";
}

export function App({ source }: { source: DataSource }) {
  const route = useRoute();
  const current = section(route);
  const view = useMemo(() => {
    switch (route.view) {
      case "runs":
        return <RunsView source={source} initialQuery={route.q} />;
      case "run":
        return <TimelineView key={route.id} source={source} id={route.id} step={route.step} />;
      case "compare":
        return <CompareView source={source} a={route.a} b={route.b} step={route.step} />;
      case "drift-index":
        return <DriftIndex source={source} />;
      case "drift":
        return <DriftView key={route.name} source={source} name={route.name} />;
    }
  }, [route, source]);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href={href({ view: "runs", q: "" })}>
          <span className="logo" aria-hidden="true">
            R
          </span>
          RepleX
        </a>
        <nav aria-label="Views">
          <a className={current === "runs" ? "on" : ""} href={href({ view: "runs", q: "" })}>
            Runs
          </a>
          <a className={current === "compare" ? "on" : ""} href={href({ view: "compare", a: null, b: null, step: null })}>
            Compare
          </a>
          <a className={current === "drift" ? "on" : ""} href={href({ view: "drift-index" })}>
            Drift
          </a>
        </nav>
        <span className={`source source-${source.mode}`} title={source.mode === "static" ? "Data is inlined in this file" : "Served by replex console on this machine"}>
          {source.label}
        </span>
      </header>
      <main>{view}</main>
      <footer className="footer">
        <span>
          <Kbd>j</Kbd>/<Kbd>k</Kbd> move · <Kbd>Enter</Kbd> open / expand · <Kbd>/</Kbd> search
        </span>
        <span>Read-only · local only · redacted values appear as their tokens</span>
      </footer>
    </div>
  );
}
