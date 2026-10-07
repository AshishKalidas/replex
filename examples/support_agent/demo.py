"""`make demo`: the whole RepleX workflow in under a minute, offline, with no API key.

1. Replay the committed cassettes (no network, no cost, no tool side effects).
2. Change the prompt (v1 -> v2) and run the suite in drift mode against the local mock model.
3. Write the single-file HTML report (and a Markdown summary) and print where it is.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / ".replex"
BOLD, DIM, GREEN, RED, RESET = (
    ("\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m") if sys.stdout.isatty() else ("",) * 5
)


def step(n: int, title: str) -> None:
    print(f"\n{BOLD}[{n}/3] {title}{RESET}", flush=True)


def run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, float]:
    print(f"{DIM}$ {' '.join(cmd)}{RESET}", flush=True)
    start = time.perf_counter()
    code = subprocess.call(cmd, cwd=HERE, env={**os.environ, **(env or {})})
    return code, time.perf_counter() - start


def main() -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPLEX_")}
    env.pop("CI", None)
    os.environ.clear()
    os.environ.update(env)
    shutil.rmtree(OUT, ignore_errors=True)
    py = sys.executable

    step(1, "Replay the committed cassettes (offline, no API key)")
    code, seconds = run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    if code != 0:
        print(f"{RED}replay failed: the committed cassettes no longer match the agent{RESET}")
        return code
    print(
        f"{GREEN}✓ replay passed in {seconds:.1f}s: "
        f"zero network calls, zero tool side effects{RESET}"
    )

    step(2, "Change the system prompt (v1 -> v2) and check for behavioural drift")
    code, _ = run(
        [
            py,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=line",
            "--replex-mode=drift",
            "-k",
            "test_support_agent",
        ],
        env={"PROMPT_VERSION": "v2"},
    )
    if code == 0:
        print(f"{RED}expected the v2 prompt to drift, but every check passed{RESET}")
        return 1
    print(
        f"{GREEN}✓ drift detected, as expected: the v2 prompt skips the refund-policy check{RESET}"
    )

    step(3, "Write the reports")
    code, _ = run(
        [
            py,
            "-m",
            "replex.cli",
            "report",
            "cassettes",
            ".replex/candidates",
            "--html",
            ".replex/replex-report.html",
            "--markdown",
            ".replex/replex-report.md",
        ]
    )
    if code != 0:
        return code
    report = OUT / "replex-report.html"
    print(f"\n{BOLD}HTML report:{RESET} {report}")
    print(f"{DIM}Open it in a browser (works from the file system, no server needed).")
    print("Explore interactively:  replex console cassettes --candidates .replex/candidates")
    print(f"Accept the new behaviour: replex approve refund_request{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
