import type { DataSource, WorkspaceInfo } from "../api";
import { EmptyState, ErrorState, Loading, PageHead, Status, ToolChips } from "../components/common";
import { headline, humanize, scenarios, verdictOf, VERDICT_LABEL, VERDICT_TONE, type Scenario } from "../explain";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";
import type { DriftReport } from "../types";

interface Loaded {
  list: Scenario[];
  reports: Map<string, DriftReport>;
}

async function load(source: DataSource): Promise<Loaded> {
  const list = scenarios(await source.runs());
  const pairs = await Promise.all(
    list
      .filter((s) => s.baseline && s.candidate)
      .map(async (s) => [s.name, (await source.drift(s.name)).report] as const),
  );
  return { list, reports: new Map(pairs) };
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function title(list: Scenario[], reports: Map<string, DriftReport>): string {
  const changed = list.filter((s) => verdictOf(reports.get(s.name) ?? null) === "changed").length;
  if (changed) return `${plural(changed, "scenario")} ${changed === 1 ? "needs" : "need"} your review.`;
  if (reports.size) return "Everything behaves as recorded.";
  return `${plural(list.length, "scenario")} recorded.`;
}

const PURPOSE =
  "RepleX keeps a recording of how your agent handles each scenario: every model call and every tool call. After you change a prompt, a model or your code, it runs them again and shows you what the agent now does differently.";

const SAMPLE_STORY =
  "This sample is a customer-support agent. Its system prompt was reworded to sound friendlier, and RepleX caught that it now skips the refund-policy check before issuing refunds.";

export function HowItWorks() {
  return (
    <section className="how" aria-labelledby="how-title">
      <h2 id="how-title">How RepleX works</h2>
      <ol className="how-steps">
        <li>
          <h3>Record once</h3>
          <p>Run your tests against the real model one time. RepleX saves each run to a readable file in your repository, with secrets removed.</p>
          <code>pytest --replex-mode=record</code>
        </li>
        <li>
          <h3>Replay for free</h3>
          <p>Every later test run replays those files. No network, no API costs, and the same result every time, so CI stays fast and reliable.</p>
          <code>pytest</code>
        </li>
        <li>
          <h3>Review what changed</h3>
          <p>After a prompt or model change, run the scenarios live again. RepleX compares them with the recordings and flags behaviour you did not intend.</p>
          <code>pytest --replex-mode=drift</code>
        </li>
      </ol>
    </section>
  );
}

function Onboarding({ workspace, sample, onSwitch }: { workspace: WorkspaceInfo; sample?: WorkspaceInfo; onSwitch?: (id: string) => void }) {
  return (
    <EmptyState
      title="No recordings in this project yet."
      action={
        sample &&
        onSwitch && (
          <button type="button" className="button" onClick={() => onSwitch(sample.id)}>
            Explore the sample workspace
          </button>
        )
      }
    >
      <p>
        RepleX is looking in <code>{workspace.baseline_dir}</code>. Recordings you make there appear here the next time you
        come back to this window.
      </p>
    </EmptyState>
  );
}

export function OverviewView({
  source,
  workspace,
  workspaces = [],
  onSwitchWorkspace,
}: {
  source: DataSource;
  workspace?: WorkspaceInfo;
  workspaces?: WorkspaceInfo[];
  onSwitchWorkspace?: (id: string) => void;
}) {
  const data = useAsync(() => load(source), [source]);
  const list = data.state === "ready" ? data.data.list : [];
  const [active, setActive] = useKeyNav(list.length, (i) => {
    const s = list[i];
    if (s) window.location.hash = href({ view: "scenario", name: s.name });
  });

  const isSample = workspace?.id === "sample";
  const eyebrow = workspace ? (isSample ? "Sample workspace · read-only" : "Your project") : source.label;

  if (data.state === "loading") return <Loading what="scenarios" />;
  if (data.state === "error") return <ErrorState error={data.error} />;

  if (list.length === 0) {
    return (
      <section>
        <PageHead eyebrow={eyebrow} title="Is your agent still behaving?" lede={PURPOSE} />
        {workspace ? (
          <Onboarding workspace={workspace} sample={workspaces.find((w) => w.id === "sample")} onSwitch={onSwitchWorkspace} />
        ) : (
          <EmptyState title="This report is empty." />
        )}
        <HowItWorks />
      </section>
    );
  }

  const { reports } = data.data;
  return (
    <section>
      <PageHead eyebrow={eyebrow} title={title(list, reports)} lede={isSample ? SAMPLE_STORY : PURPOSE} />

      <div className="section-head section-head-first">
        <h2>Scenarios</h2>
        <a className="cta" href={href({ view: "runs", q: "" })}>
          All runs
        </a>
      </div>
      <ul className="scenarios">
        {list.map((s, i) => {
          const report = reports.get(s.name) ?? null;
          const verdict = verdictOf(report);
          const latest = s.candidate ?? s.baseline;
          return (
            <li key={s.name} data-nav-index={i} className={i === active ? "row-active" : ""} onMouseEnter={() => setActive(i)}>
              <a href={href({ view: "scenario", name: s.name })} className="scenario">
                <div className="scenario-name">
                  <span className="scenario-title">{humanize(s.name)}</span>
                  <code className="scenario-id">{s.name}</code>
                </div>
                <div className="scenario-what">
                  <p>{headline(report)}</p>
                  <ToolChips names={latest?.tool_names ?? []} />
                </div>
                <div className="scenario-status">
                  <Status tone={VERDICT_TONE[verdict]}>{VERDICT_LABEL[verdict]}</Status>
                  <span className="chevron" aria-hidden="true" />
                </div>
              </a>
            </li>
          );
        })}
      </ul>

      <HowItWorks />
    </section>
  );
}
