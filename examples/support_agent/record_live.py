"""Record REAL cassettes from Claude Sonnet 5.5 or MiMo 2.6 (owner-only, needs API keys).

    python record_live.py claude            # ANTHROPIC_API_KEY
    python record_live.py mimo              # MIMO_API_KEY, MIMO_BASE_URL, MIMO_MODEL
    python record_live.py claude --overwrite --stream

Cassettes are written to cassettes/live/<provider>/ with secrets redacted before anything
touches disk. The model id for MiMo always comes from MIMO_MODEL; it is never hard-coded.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from support_agent import AgentConfig, run_agent
from support_agent.scenarios import SCENARIOS

import replex

HERE = Path(__file__).resolve().parent


def config_for(provider: str, stream: bool) -> AgentConfig:
    if provider == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit(
                "ANTHROPIC_API_KEY is not set: export it, then run `make record-claude` again."
            )
        return AgentConfig(
            provider="anthropic",
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            api_key=key,
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            stream=stream,
        )
    missing = [v for v in ("MIMO_API_KEY", "MIMO_BASE_URL", "MIMO_MODEL") if not os.environ.get(v)]
    if missing:
        sys.exit(
            f"set {', '.join(missing)} to record MiMo cassettes (the model id is never hard-coded)."
        )
    return AgentConfig(
        provider="openai",
        base_url=os.environ["MIMO_BASE_URL"],
        api_key=os.environ["MIMO_API_KEY"],
        model=os.environ["MIMO_MODEL"],
        stream=stream,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("provider", choices=["claude", "mimo"])
    parser.add_argument("--overwrite", action="store_true", help="replace existing live cassettes")
    parser.add_argument("--stream", action="store_true", help="use streaming responses")
    args = parser.parse_args()

    cfg = config_for(args.provider, args.stream)
    host = urlsplit(cfg.base_url).hostname or ""
    out = HERE / "cassettes" / "live" / args.provider
    print(f"Recording {len(SCENARIOS)} scenarios from {cfg.model} at {host} into {out}")
    for scenario in SCENARIOS:
        tags = [
            f"scenario={scenario.name}",
            f"wire={cfg.provider}",
            f"stream={'true' if cfg.stream else 'false'}",
            f"prompt={cfg.prompt_version}",
            f"host={host}",
            f"base_url={cfg.base_url}",
            "source=live",
        ]
        with replex.session(
            scenario.name,
            mode="record",
            cassette_dir=out,
            intercept_hosts=[host],
            overwrite=args.overwrite,
            tags=tags,
        ) as s:
            result = run_agent(scenario.question, cfg)
        t = s.trace
        print(
            f"  {scenario.name:<20} {' -> '.join(t.tool_names) or '(no tools)':<60} "
            f"{t.usage.total:>6} tokens  {s.written_path}"
        )
        print(f"    answer: {result.answer[:160]}")
    print("Done. Check them with `replex redact --check cassettes/live` before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
