"""
Persistencia de estado del bucle de mejora continua (ai_state/state.json).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path("ai_state") / "state.json"


def _default_state() -> dict[str, Any]:
    return {
        "iteration": 0,
        "status": "initialized",
        "last_instruction": "",
        "last_cursor_result": {},
        "last_test_result": {},
        "open_risks": [],
        "user_declared_functional": False,
    }


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_STATE_PATH
    if not p.exists():
        return _default_state()
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()
    base = _default_state()
    if isinstance(data, dict):
        base.update(data)
    if "user_declared_functional" not in data:
        base["user_declared_functional"] = False
    return base


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    fd, tmp = tempfile.mkstemp(
        prefix="state_",
        suffix=".json",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
