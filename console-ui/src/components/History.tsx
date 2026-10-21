import type { DataSource } from "../api";
import { describeEntry, entryTime, MARKER_LABEL, markerOf, passSummary, shortDay, trendSentence, type Marker } from "../history";
import { useAsync } from "../hooks";
import type { HistoryEntry, ScenarioHistory } from "../types";
import "./history.css";

const WIDTH = 640;
const HEIGHT = 56;
const PAD = 16;
const AXIS = 24;

function Shape({ marker, x }: { marker: Marker; x: number }) {
  const y = AXIS;
  switch (marker) {
    case "passed":
      return <circle cx={x} cy={y} r={6} />;
    case "changed":
      return <polygon points={`${x},${y - 7} ${x + 7},${y + 6} ${x - 7},${y + 6}`} />;
    case "accepted":
      return <polygon points={`${x},${y - 7} ${x + 7},${y} ${x},${y + 7} ${x - 7},${y}`} />;
    case "rejected":
      return <rect x={x - 5.5} y={y - 5.5} width={11} height={11} />;
  }
}

/** Checks and decisions in order, evenly spaced: the order matters more than the gaps. */
export function HistoryTimeline({ events, label }: { events: HistoryEntry[]; label: string }) {
  const step = events.length > 1 ? (WIDTH - 2 * PAD) / (events.length - 1) : 0;
  const x = (i: number) => (events.length > 1 ? PAD + i * step : WIDTH / 2);
  const first = events[0];
  const last = events[events.length - 1];
  return (
    <svg className="history-svg" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={label}>
      <line className="history-axis" x1={PAD} x2={WIDTH - PAD} y1={AXIS} y2={AXIS} />
      {events.map((e, i) => {
        const marker = markerOf(e);
        return (
          <g key={i} className={`history-mark history-${marker}`}>
            <title>{`${MARKER_LABEL[marker]}, ${entryTime(e)}`}</title>
            <Shape marker={marker} x={x(i)} />
          </g>
        );
      })}
      {first && (
        <text className="history-date" x={events.length > 1 ? PAD : WIDTH / 2} y={HEIGHT - 4} textAnchor={events.length > 1 ? "start" : "middle"}>
          {shortDay(first.at)}
        </text>
      )}
      {last && events.length > 1 && (
        <text className="history-date" x={WIDTH - PAD} y={HEIGHT - 4} textAnchor="end">
          {shortDay(last.at)}
        </text>
      )}
    </svg>
  );
}

function Legend() {
  const markers: Marker[] = ["passed", "changed", "accepted", "rejected"];
  return (
    <ul className="history-legend" aria-hidden="true">
      {markers.map((m) => (
        <li key={m}>
          <svg viewBox="15 15 18 18" className={`history-mark history-${m}`}>
            <Shape marker={m} x={24} />
          </svg>
          {MARKER_LABEL[m]}
        </li>
      ))}
    </ul>
  );
}

/** The timeline, its legend and the same events as a list (newest first). */
export function ScenarioHistoryView({ history }: { history: ScenarioHistory }) {
  const summary = passSummary(history);
  return (
    <div className="history">
      <p className="history-summary">{summary}</p>
      <HistoryTimeline events={history.events} label={`Timeline of ${history.events.length} events. ${summary}`} />
      <Legend />
      <ol className="history-list" aria-label="Checks and decisions, newest first">
        {[...history.events].reverse().map((e, i) => {
          const marker = markerOf(e);
          return (
            <li key={i}>
              <span className={`history-tag history-tag-${marker}`}>{MARKER_LABEL[marker]}</span>
              <time dateTime={e.at}>{entryTime(e)}</time>
              <span className="history-detail">{describeEntry(e)}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** The "History" section of a scenario page. Hidden where there is no history file. */
export function HistorySection({ source, name, revision }: { source: DataSource; name: string; revision: number }) {
  const data = useAsync(() => source.history(name), [source, name, revision]);
  if (data.state !== "ready" || !data.data.enabled) return null;
  const history = data.data.scenarios.find((s) => s.name === name);
  return (
    <>
      <div className="section-head">
        <h2>History</h2>
      </div>
      {history ? (
        <ScenarioHistoryView history={history} />
      ) : (
        <p className="quiet">
          No drift checks recorded yet. Each <code>pytest --replex-mode=drift</code> run adds one here.
        </p>
      )}
    </>
  );
}

/** One line on the Overview: how the recent checks went. */
export function HistoryTrendLine({ source }: { source: DataSource }) {
  const data = useAsync(() => source.history(), [source]);
  if (data.state !== "ready") return null;
  const sentence = trendSentence(data.data.trend);
  return sentence ? <p className="history-trend">{sentence}</p> : null;
}
