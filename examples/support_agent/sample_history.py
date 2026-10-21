"""`make sample` helper: build the console sample's drift history from real offline checks.

Runs the suite in drift mode twice against the local mock model, with the v1 prompt (every
scenario behaves as recorded) and then with the v2 prompt (the two refund flows change). Each
check appends its own line to the history file given as the only argument, exactly as it would
in a project. Git details are left out, so the sample does not carry this repository's state.

    python sample_history.py ../../src/replex/console/sample/history.jsonl
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The exit code pytest returns for each prompt version: v2 must drift.
EXPECTED = {"v1": 0, "v2": 1}


def main(out: Path) -> int:
    out = out.resolve()
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPLEX_") and k != "CI"}
    with tempfile.TemporaryDirectory() as candidates:
        env.update(
            REPLEX_HISTORY=str(out),
            REPLEX_CANDIDATE_DIR=candidates,
            REPLEX_GIT_SHA="",
            REPLEX_GIT_BRANCH="",
        )
        for version, expected in EXPECTED.items():
            code = subprocess.call(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--replex-mode=drift",
                    "-k",
                    "test_support_agent",
                ],
                cwd=HERE,
                env={**env, "PROMPT_VERSION": version},
                stdout=subprocess.DEVNULL,
            )
            if code != expected:
                print(f"drift check with prompt {version} exited {code}, expected {expected}")
                return 1
    out.with_name(out.name + ".lock").unlink(missing_ok=True)
    print(f"history: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
