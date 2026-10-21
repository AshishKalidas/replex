// Test fixtures: a temporary project copied from e2e/fixtures/project, served by a real
// `replex console`. The Python interpreter is $REPLEX_PYTHON (default: python), which must
// have replex and its console extra importable.
import { spawn, type ChildProcess } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";

export { expect } from "@playwright/test";

const FIXTURES = fileURLToPath(new URL("./fixtures/project", import.meta.url));
export const PYTHON = process.env.REPLEX_PYTHON ?? "python";

/** The scenarios in the fixture project, as the console orders and names them. */
export const SCENARIOS = {
  /** Behaviour changed: skips the refund-policy check. */
  ineligible: "refund_ineligible_openai",
  /** Behaviour changed: skips the refund-policy check and searches the docs instead. */
  refund: "refund_request",
  /** Re-run and unchanged. */
  status: "order_status_stream",
  /** Recorded only, never re-run. */
  pending: "order_status_openai_stream",
} as const;

/** A project directory with cassettes, candidates and a policy, restored before every test. */
export class Project {
  readonly root: string;

  constructor() {
    this.root = mkdtempSync(join(tmpdir(), "replex-e2e-"));
    this.reset();
  }

  get baselineDir(): string {
    return join(this.root, "cassettes");
  }

  get candidateDir(): string {
    return join(this.root, "candidates");
  }

  get policy(): string {
    return join(this.root, "replex.policy.yaml");
  }

  baseline(name: string): string {
    return join(this.baselineDir, `${name}.replex.json`);
  }

  candidate(name: string): string {
    return join(this.candidateDir, `${name}.replex.json`);
  }

  reset(): void {
    rmSync(this.baselineDir, { recursive: true, force: true });
    rmSync(this.candidateDir, { recursive: true, force: true });
    cpSync(FIXTURES, this.root, { recursive: true, filter: (path) => !basename(path).startsWith("._") });
  }

  remove(): void {
    rmSync(this.root, { recursive: true, force: true });
  }
}

/** Run the replex CLI to completion and return its output; throws when it fails. */
export function replex(args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ["-m", "replex.cli", ...args], { cwd, env: localEnv() });
    let output = "";
    child.stdout.on("data", (chunk: Buffer) => (output += chunk.toString()));
    child.stderr.on("data", (chunk: Buffer) => (output += chunk.toString()));
    child.on("error", reject);
    child.on("close", (code) =>
      code === 0 ? resolve(output) : reject(new Error(`replex ${args[0]} exited with ${code}:\n${output}`)),
    );
  });
}

/** The environment of a developer's machine: the console refuses to accept changes in CI. */
function localEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  delete env.CI;
  return env;
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() =>
        typeof address === "object" && address ? resolve(address.port) : reject(new Error("no port")),
      );
    });
  });
}

async function waitUntilServing(url: string, child: ChildProcess, output: () => string): Promise<void> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`replex console exited with ${child.exitCode}:\n${output()}`);
    try {
      if ((await fetch(`${url}api/health`)).ok) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`replex console did not start within 30s:\n${output()}`);
}

async function startConsole(project: Project): Promise<{ url: string; stop: () => Promise<void> }> {
  const index = fileURLToPath(new URL("../../src/replex/console/static/index.html", import.meta.url));
  if (!existsSync(index)) throw new Error("The console is not built. Run `npm run build` in console-ui first.");
  const port = await freePort();
  const args = [
    "-m", "replex.cli", "console", project.baselineDir,
    "--candidates", project.candidateDir,
    "--policy", project.policy,
    "--port", String(port),
    "--no-open",
  ];
  const child = spawn(PYTHON, args, { cwd: project.root, env: localEnv() });
  let output = "";
  child.stdout.on("data", (chunk: Buffer) => (output += chunk.toString()));
  child.stderr.on("data", (chunk: Buffer) => (output += chunk.toString()));
  const url = `http://127.0.0.1:${port}/`;
  const stop = () =>
    new Promise<void>((resolve) => {
      if (child.exitCode !== null) return resolve();
      child.once("exit", () => resolve());
      child.kill();
    });
  try {
    await waitUntilServing(url, child, () => output);
  } catch (err) {
    await stop();
    throw err;
  }
  return { url, stop };
}

interface WorkerFixtures {
  workerProject: Project;
  consoleUrl: string;
}

export const test = base.extend<{ project: Project }, WorkerFixtures>({
  workerProject: [
    async ({}, use) => {
      const project = new Project();
      await use(project);
      project.remove();
    },
    { scope: "worker" },
  ],
  consoleUrl: [
    async ({ workerProject }, use) => {
      const server = await startConsole(workerProject);
      await use(server.url);
      await server.stop();
    },
    { scope: "worker", timeout: 60_000 },
  ],
  baseURL: async ({ consoleUrl }, use) => use(consoleUrl),
  project: [
    async ({ workerProject }, use) => {
      workerProject.reset();
      await use(workerProject);
    },
    { auto: true },
  ],
});
