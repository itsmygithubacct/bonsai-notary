#!/usr/bin/env python3
"""Pending producer / signed verifier handoff entry point."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
engine = Path(os.environ.get("BONSAI_ENGINE_DIR", ROOT / "engine"))
engine_python = engine / "bonsai" / ".venv" / "bin" / "python"
if os.environ.get("BONSAI_HANDOFF_ENGINE_PY") != "1" and engine_python.is_file():
    environment = os.environ.copy()
    environment["BONSAI_HANDOFF_ENGINE_PY"] = "1"
    os.execve(
        str(engine_python),
        [str(engine_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

sys.path.insert(0, str(ROOT))

from notary_tools.handoff import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
