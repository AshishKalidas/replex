// Hash routing so the same app works from the server and from a file:// report.

export type Route =
  | { view: "overview" }
  | { view: "scenario"; name: string }
  | { view: "runs"; q: string }
  | { view: "run"; id: string; step: number | null }
  | { view: "compare"; a: string | null; b: string | null; step: number | null }


function stepParam(params: URLSearchParams): number | null {
  const raw = params.get("step");
  if (raw === null || raw === "") return null;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

export function parseRoute(hash: string): Route {
  const clean = hash.replace(/^#/, "");
  const [path = "", query = ""] = clean.split("?", 2);
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const head = parts[0] ?? "overview";
  if (head === "run" && parts.length >= 3) {
    return { view: "run", id: `${parts[1]}:${parts.slice(2).join("/")}`, step: stepParam(params) };
  }
  if (head === "compare") {
    return { view: "compare", a: params.get("a"), b: params.get("b"), step: stepParam(params) };
  }
  // "drift" is the route older links and reports used for what is now the scenario page.
  if ((head === "scenario" || head === "drift") && parts[1]) {
    return { view: "scenario", name: parts.slice(1).join("/") };
  }
  if (head === "runs") return { view: "runs", q: params.get("q") ?? "" };
  return { view: "overview" };
}

export function href(route: Route): string {
  switch (route.view) {
    case "overview":
      return "#/";
    case "scenario":
      return `#/scenario/${encodeURIComponent(route.name)}`;
    case "runs":
      return route.q ? `#/runs?${new URLSearchParams({ q: route.q }).toString()}` : "#/runs";
    case "run": {
      const at = route.id.indexOf(":");
      const kind = route.id.slice(0, at);
      const name = route.id.slice(at + 1);
      const step = route.step === null ? "" : `?step=${route.step}`;
      return `#/run/${encodeURIComponent(kind)}/${encodeURIComponent(name)}${step}`;
    }
    case "compare": {
      const q = new URLSearchParams();
      if (route.a) q.set("a", route.a);
      if (route.b) q.set("b", route.b);
      if (route.step !== null) q.set("step", String(route.step));
      const s = q.toString();
      return s ? `#/compare?${s}` : "#/compare";
    }
  }
}
