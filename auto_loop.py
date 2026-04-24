#!/usr/bin/env python3
"""
Orquestador del bucle autónomo: Gemini (supervisor) + Cursor Agent (ejecutor) + tests + reportes.

Ejemplos:
  python auto_loop.py
  python auto_loop.py --max-iterations 5
  python auto_loop.py --infinite
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cursor_executor import is_cursor_auth_failure, run_cursor_agent
from gemini_supervisor import (
    build_user_prompt,
    call_gemini,
    compose_cursor_instruction,
    extract_risks_bullets,
)
from reporter import write_current_status, write_iteration_report
from state_manager import load_state, save_state
from test_runner import run_tests, truncate_text

LOG_DIR = Path("ai_logs")
DEFAULT_MAX_ITERATIONS = 10


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "auto_loop.log"
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _project_root() -> Path:
    load_dotenv()
    raw = os.environ.get("PROJECT_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _compact_for_state(name: str, data: dict[str, Any], limit: int = 12000) -> dict[str, Any]:
    try:
        s = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"name": name, "error": "unserializable"}
    if len(s) <= limit:
        return json.loads(s)
    return {"name": name, "truncated_json": truncate_text(s, max_chars=limit)}


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _summarize_gemini(parsed: dict[str, Any]) -> str:
    parts = [
        (parsed.get("objective") or "").strip(),
        (parsed.get("advance") or "").strip(),
    ]
    return "\n\n".join(p for p in parts if p) or "(sin resumen estructurado)"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bucle autónomo de mejora continua (Gemini + Cursor + tests).")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        metavar="N",
        help="Número máximo de iteraciones y salida (modo acotado).",
    )
    g.add_argument(
        "--infinite",
        action="store_true",
        help="Repite hasta interrupción manual (Ctrl+C).",
    )
    p.add_argument(
        "--skip-cursor",
        action="store_true",
        help="No invoca cursor-agent (solo planifica con Gemini y ejecuta tests).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    log = logging.getLogger("auto_loop")
    args = parse_args(argv)

    if args.infinite:
        max_iters: int | None = None
    elif args.max_iterations is not None:
        max_iters = max(1, int(args.max_iterations))
    else:
        max_iters = DEFAULT_MAX_ITERATIONS

    root = _project_root()
    os.chdir(root)
    log.info("Directorio de trabajo: %s", root)

    stop = {"flag": False}

    def _handle_sigint(_sig: int, _frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    state = load_state()
    iteration_done = 0

    try:
        n = 0
        while max_iters is None or n < max_iters:
            if stop["flag"]:
                log.info("Señal de parada recibida; saliendo.")
                state["status"] = "interrupted"
                save_state(state)
                break

            n += 1
            iteration = int(state.get("iteration", 0) or 0) + 1
            state["iteration"] = iteration
            state["status"] = "running"
            save_state(state)

            cursor_prev = state.get("last_cursor_result") or {}
            test_prev = state.get("last_test_result") or {}

            user_prompt = build_user_prompt(root, state, test_prev, cursor_prev)
            gemini_out = call_gemini(user_prompt)
            parsed = gemini_out.get("parsed") or {}
            raw_text = gemini_out.get("text") or ""

            if not gemini_out.get("ok"):
                state["status"] = "gemini_error"
                state["last_instruction"] = ""
                state["open_risks"] = state.get("open_risks") or []
                state["last_cursor_result"] = _compact_for_state(
                    "cursor_skipped",
                    {"skipped": True, "reason": "gemini_error"},
                )
                state["last_test_result"] = _compact_for_state(
                    "tests_after_gemini_error",
                    test_prev if isinstance(test_prev, dict) else {},
                )
                save_state(state)
                write_iteration_report(iteration, state, raw_text or json.dumps(gemini_out)[:20000])
                write_current_status(iteration, state, _summarize_gemini(parsed))
                log.error("Gemini falló: %s", gemini_out.get("error"))
                iteration_done = iteration
                continue

            instruction = compose_cursor_instruction(parsed)
            state["last_instruction"] = truncate_text(instruction, max_chars=16000)

            skip_after_auth = _env_truthy("AUTO_LOOP_SKIP_CURSOR_AFTER_AUTH_ERROR", "0")
            if args.skip_cursor:
                cursor_result = {
                    "ok": True,
                    "skipped": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "error": None,
                    "reason": "cli_flag_skip_cursor",
                }
            elif (
                skip_after_auth
                and isinstance(cursor_prev, dict)
                and is_cursor_auth_failure(cursor_prev)
            ):
                log.info(
                    "Omitiendo Cursor Agent: la iteración anterior falló por autenticación "
                    "(AUTO_LOOP_SKIP_CURSOR_AFTER_AUTH_ERROR=1). "
                    "Ejecuta en la máquina: `cursor-agent login` o exporta CURSOR_API_KEY."
                )
                cursor_result = {
                    "ok": True,
                    "skipped": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "error": None,
                    "reason": "cursor_agent_auth_required_previous_iteration",
                }
            else:
                cursor_result = run_cursor_agent(instruction, root)
                if not cursor_result.get("ok"):
                    log.warning(
                        "Cursor Agent falló: returncode=%s error=%s",
                        cursor_result.get("returncode"),
                        (cursor_result.get("error") or "")[:1500],
                    )

            state["last_cursor_result"] = _compact_for_state(
                "cursor",
                {
                    "ok": cursor_result.get("ok"),
                    "skipped": cursor_result.get("skipped"),
                    "reason": cursor_result.get("reason"),
                    "returncode": cursor_result.get("returncode"),
                    "stdout": truncate_text(cursor_result.get("stdout") or "", 8000),
                    "stderr": truncate_text(cursor_result.get("stderr") or "", 8000),
                    "error": cursor_result.get("error"),
                },
            )

            test_result = run_tests(root)
            state["last_test_result"] = _compact_for_state("tests", test_result)

            risks = extract_risks_bullets(str(parsed.get("risks") or ""))
            if risks:
                state["open_risks"] = risks

            if not cursor_result.get("ok"):
                state["status"] = "cursor_error"
            elif not test_result.get("overall_ok"):
                state["status"] = "tests_failed"
            elif cursor_result.get("skipped"):
                state["status"] = "iteration_ok_cursor_skipped"
            else:
                state["status"] = "iteration_ok"

            save_state(state)
            write_iteration_report(iteration, state, raw_text)
            write_current_status(iteration, state, _summarize_gemini(parsed))
            iteration_done = iteration
            log.info(
                "Iteración %s finalizada: status=%s tests_ok=%s",
                iteration,
                state.get("status"),
                test_result.get("overall_ok"),
            )

        if max_iters is not None and n >= max_iters and not stop["flag"]:
            state["status"] = "max_iterations_reached"
            save_state(state)
            log.info("Se alcanzó el máximo de iteraciones (%s).", max_iters)

    except KeyboardInterrupt:
        state["status"] = "interrupted"
        save_state(state)
        log.info("Interrupción por teclado.")
        return 130

    log.info("Bucle terminado (última iteración registrada: %s).", iteration_done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
