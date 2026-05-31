#!/usr/bin/env python3
"""Run every configured static-analysis gate and fail if any gate fails."""

from __future__ import annotations

import subprocess
import sys

COMMANDS = (
    ("ruff", ("python3", "-m", "ruff", "check", "--config", "pyproject.toml", "tools", "tests")),
    ("mypy", ("python3", "-m", "mypy", "--config-file", "pyproject.toml")),
    ("pyright", ("python3", "-m", "pyright")),
    ("bandit", ("python3", "-m", "bandit", "-c", "pyproject.toml", "-r", "tools", "tests")),
    ("vulture", ("python3", "-m", "vulture", "tools", "tests", "--min-confidence", "80")),
)


def main() -> int:
    failed: list[str] = []
    for name, command in COMMANDS:
        print(f"\n== {name} ==", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            failed.append(name)

    if failed:
        print(f"\nMaximum enforcement failed: {', '.join(failed)}", file=sys.stderr, flush=True)
        return 1

    print("\nMaximum enforcement passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
