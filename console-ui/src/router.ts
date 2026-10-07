// Hash routing so the same app works from the server and from a file:// report.

export type Route =
  | { view: "runs"; q: string }
  | { view: "run"; id: string; step: number | null }
  | { view: "compare"; a: string | null; b: string | null; step: number | null }
  | { view: "drift-index" }
  | { view: "drift"; name: string };

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
  const head = parts[0] ?? "runs";
  if (head === "run" && parts.length >= 3) {
    return { view: "run", id: `${parts[1]}:${parts.slice(2).join("/")}`, step: stepParam(params) };
  }
  if (head === "compare") {
    return { view: "compare", a: params.get("a"), b: params.get("b"), step: stepParam(params) };
  }
  if (head === "drift") {
    return parts[1] ? { view: "drift", name: parts.slice(1).join("/") } : { view: "drift-index" };
  }
  return { view: "runs", q: params.get("q") ?? "" };
}

export function href(route: Route): string {
  switch (route.view) {
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
    case "drift-index":
      return "#/drift";
    case "drift":
      return `#/drift/${encodeURIComponent(route.name)}`;
  }
}
