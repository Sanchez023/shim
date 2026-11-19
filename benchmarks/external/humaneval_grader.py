"""Official HumanEval grader entry point (runs on Linux/WSL).

The official ``human_eval.execution`` module uses ``signal.setitimer``, which
does not exist on Windows.  On Windows the adapter therefore writes the
problem and the agent's solution to JSON and invokes this script through WSL,
so the *official* ``check_correctness`` implementation performs the grading.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from human_eval.evaluation import check_correctness

    result = check_correctness(
        payload["problem"],
        payload["completion"],
        timeout=float(payload.get("timeout", 10.0)),
    )
    print(
        json.dumps(
            {"passed": bool(result.get("passed")), "result": str(result.get("result"))},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
