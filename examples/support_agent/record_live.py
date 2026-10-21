"""Record REAL cassettes from a real model, or check one for drift (owner-only, needs API keys).

    python record_live.py claude            # ANTHROPIC_API_KEY
    python record_live.py mimo              # MIMO_API_KEY, MIMO_BASE_URL, MIMO_MODEL
    python record_live.py openrouter        # OPENROUTER_API_KEY
    python record_live.py openrouter --model anthropic/claude-haiku-5.5
    python record_live.py claude --overwrite --stream

Recordings go to cassettes/live/<name>/, where <name> is the target (claude, mimo) or, through
OpenRouter, "openrouter-" plus the model's name (openrouter-claude-sonnet-5.5). Secrets are
redacted before anything touches disk.

The model id comes from --model, else CLAUDE_MODEL, MIMO_MODEL or OPENROUTER_MODEL. Claude
defaults to claude-sonnet-5-5 and OpenRouter to anthropic/claude-sonnet-5.5; MiMo has no
default, because its ids change between releases and endpoints.

With --against NAME the scenarios are not recorded: they run live and are compared with the
recordings in cassettes/live/NAME/ under replex.policy.yaml, and each latest run is kept as a
candidate in cassettes/live/NAME/candidates/<name>-<prompt>/. For example, a prompt change and
a model swap, both checked against Sonnet's v1 recordings:

    python record_live.py openrouter --prompt v2 --against openrouter-claude-sonnet-5.5
    python record_live.py openrouter --model anthropic/claude-haiku-5.5 \\
        --against openrouter-claude-sonnet-5.5
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from support_agent import AgentConfig, run_agent
from support_agent import tools as agent_tools
from support_agent.prompts import PROMPTS
from support_agent.scenarios import SCENARIOS

import replex

HERE = Path(__file__).resolve().parent
LIVE = HERE / "cassettes" / "live"
TARGETS = ("claude", "mimo", "openrouter")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"


def config_for(
    target: str, stream: bool, model: str | None = None, prompt_version: str = "v1"
) -> AgentConfig:
    """The agent configuration for a target, from the flags and the environment."""
    env = os.environ
    if target == "claude":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit(
                "ANTHROPIC_API_KEY is not set: export it, then run `make record-claude` again."
            )
        return AgentConfig(
            provider="anthropic",
            base_url=env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            api_key=key,
            model=model or env.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            prompt_version=prompt_version,
            stream=stream,
        )
    if target == "openrouter":
        key = env.get("OPENROUTER_API_KEY")
        if not key:
            sys.exit(
                "OPENROUTER_API_KEY is not set: export it, then run `make record-openrouter` again."
            )
        return AgentConfig(
            provider="openai",
            base_url=env.get("OPENROUTER_BASE_URL", OPENROUTER_URL),
            api_key=key,
            model=model or env.get("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL),
            prompt_version=prompt_version,
            stream=stream,
        )
    needed = ["MIMO_API_KEY", "MIMO_BASE_URL"] + ([] if model else ["MIMO_MODEL"])
    missing = [v for v in needed if not env.get(v)]
    if missing:
        sys.exit(
            f"set {', '.join(missing)} to record MiMo cassettes (the model id is never hard-coded)."
        )
    return AgentConfig(
        provider="openai",
        base_url=env["MIMO_BASE_URL"],
        api_key=env["MIMO_API_KEY"],
        model=model or env["MIMO_MODEL"],
        prompt_version=prompt_version,
        stream=stream,
    )


def folder_name(target: str, model: str) -> str:
    """The folder under cassettes/live/ for a target and model.

    Direct targets keep their own name. Through OpenRouter the model is what differs between
    recordings, so it names the folder: anthropic/claude-sonnet-5.5 -> openrouter-claude-sonnet-5.5.
    """
    if target != "openrouter":
        return target
    return f"openrouter-{model.rsplit('/', 1)[-1].replace(':', '-')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--model", help="model id (default: from the environment, see above)")
    parser.add_argument(
        "--prompt",
        choices=sorted(PROMPTS),
        default=os.environ.get("PROMPT_VERSION", "v1"),
        help="system prompt version (default: PROMPT_VERSION or v1)",
    )
    parser.add_argument(
        "--against",
        metavar="NAME",
        help="run a drift check against the recordings in cassettes/live/NAME/ instead",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace existing live cassettes")
    parser.add_argument("--stream", action="store_true", help="use streaming responses")
    args = parser.parse_args(argv)

    cfg = config_for(args.target, args.stream, args.model, args.prompt)
    host = urlsplit(cfg.base_url).hostname or ""
    name = folder_name(args.target, cfg.model)
    if args.against:
        baselines = LIVE / args.against
        out = baselines / "candidates" / f"{name}-{cfg.prompt_version}"
        if not baselines.is_dir():
            sys.exit(f"no recordings to compare with in {baselines}")
        options: dict[str, Any] = {
            "mode": "drift",
            "cassette_dir": baselines,
            "candidate_dir": out,
            "policy": HERE / "replex.policy.yaml",
            # The example's tools are an in-memory store, so a drift run may execute them all;
            # a real model's free-text arguments rarely match a recording exactly.
            "tools": "live",
        }
        print(f"Checking {cfg.model} (prompt {cfg.prompt_version}) against {baselines}")
    else:
        out = LIVE / name
        options = {"mode": "record", "cassette_dir": out, "overwrite": args.overwrite}
        print(f"Recording {len(SCENARIOS)} scenarios from {cfg.model} at {host} into {out}")

    drifted = 0
    for scenario in SCENARIOS:
        tags = [
            f"scenario={scenario.name}",
            f"wire={cfg.provider}",
            f"stream={'true' if cfg.stream else 'false'}",
            f"prompt={cfg.prompt_version}",
            f"host={host}",
            f"base_url={cfg.base_url}",
            f"model={cfg.model}",
            "source=live",
        ]
        agent_tools.reset()
        with replex.session(scenario.name, intercept_hosts=[host], tags=tags, **options) as s:
            result = run_agent(scenario.question, cfg)
        t = s.trace
        print(
            f"  {scenario.name:<24} {' -> '.join(t.tool_names) or '(no tools)':<60} "
            f"{t.usage.total:>6} tokens  {s.written_path}"
        )
        print(f"    answer: {result.answer[:160]}")
        report = s.drift_report
        if report is not None:
            drifted += not report.passed
            verdict = "behaves as recorded" if report.passed else "BEHAVIOUR CHANGED"
            print(f"    drift: {verdict}")
            for finding in report.failures + report.warnings:
                print(f"      {finding.severity:<4}  {finding.check}: {finding.message}")
    print("Done. Check them with `replex redact --check cassettes/live` before committing.")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
