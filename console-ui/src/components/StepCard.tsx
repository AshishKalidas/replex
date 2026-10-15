import { formatMs, lastMessageText, stepSubtitle, stepTitle } from "../format";
import type { Step } from "../types";
import { Collapsible, JsonBlock, Status, StepIcon } from "./common";

interface Props {
  step: Step;
  active?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  onFocus?: () => void;
  navIndex?: number;
  tone?: string;
}

/** One step of a run: compact header, details on expand. */
export function StepCard({ step, active, expanded, onToggle, onFocus, navIndex, tone }: Props) {
  const isTool = step.kind === "tool";
  return (
    <article
      className={`step step-${step.kind}${active ? " step-active" : ""}${tone ? ` step-${tone}` : ""}`}
      data-nav-index={navIndex}
      aria-current={active ? "step" : undefined}
    >
      <button
        type="button"
        className="step-head"
        onClick={() => {
          onFocus?.();
          onToggle?.();
        }}
        aria-expanded={expanded}
      >
        <span className="step-index">{step.index}</span>
        <StepIcon kind={step.kind} />
        <span className="step-main">
          <span className="step-title">
            {isTool ? <code>{stepTitle(step)}</code> : stepTitle(step)}
            {step.error && <Status tone="fail">Error</Status>}
          </span>
          <span className="step-sub">{stepSubtitle(step)}</span>
        </span>
        <span className="step-meta">
          {step.kind === "model" && (
            <span title="input and output tokens">
              {step.usage.input_tokens} in, {step.usage.output_tokens} out
            </span>
          )}
          <span>{formatMs(step.latency_ms)}</span>
        </span>
      </button>
      {expanded && <StepDetails step={step} />}
    </article>
  );
}

export function StepDetails({ step }: { step: Step }) {
  if (step.kind === "tool") {
    return (
      <div className="step-body">
        <JsonBlock label="Arguments" value={step.args} />
        {step.error ? <JsonBlock label="Error" value={step.error} /> : <JsonBlock label="Result" value={step.result} />}
      </div>
    );
  }
  const last = lastMessageText(step.input_messages);
  return (
    <div className="step-body">
      <div className="step-facts">
        <span>
          Model <code>{step.model}</code>
        </span>
        <span>
          Stop reason <code>{step.stop_reason}</code>
        </span>
        <span>{step.streamed ? "Streamed" : "Not streamed"}</span>
        <span>
          Wire format <code>{step.wire_format}</code>
        </span>
        <span title="hash of the messages sent">
          Input <code>{step.input_messages_digest.replace("sha256:", "").slice(0, 10)}</code>
        </span>
      </div>
      {step.error && <JsonBlock label="Error" value={step.error} />}
      {last && <JsonBlock label="Last message sent" value={last} lines={6} />}
      {step.text && <JsonBlock label="Reply" value={step.text} lines={12} />}
      {step.tool_requests.length > 0 && (
        <div className="block">
          <div className="block-label">Requested tools</div>
          {step.tool_requests.map((r, i) => (
            <JsonBlock key={r.id ?? i} label={r.name} value={r.args} lines={6} />
          ))}
        </div>
      )}
      {step.system && (
        <Collapsible title="System prompt">
          <JsonBlock value={step.system} lines={12} />
        </Collapsible>
      )}
      <Collapsible title={`All messages sent (${step.input_messages.length})`}>
        <JsonBlock value={step.input_messages} lines={16} />
      </Collapsible>
    </div>
  );
}
