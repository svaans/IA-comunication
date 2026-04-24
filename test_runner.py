"""
Ejecución de pruebas y comandos de verificación con subprocess.run y timeouts.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _run_one(
    cmd: list[str],
    cwd: Path,
    timeout_sec: int,
    name: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "command": cmd,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timeout_sec": timeout_sec,
        "skipped": False,
        "error": None,
    }
    exe = cmd[0] if cmd else ""
    if not exe:
        result["skipped"] = True
        result["error"] = "empty command"
        return result
    if shutil.which(exe) is None and exe not in ("python", "py"):
        result["skipped"] = True
        result["error"] = f"executable not found in PATH: {exe}"
        return result
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout or ""
        result["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        result["returncode"] = -124
        result["stdout"] = (e.stdout or "") if isinstance(e.stdout, str) else ""
        result["stderr"] = (e.stderr or "") if isinstance(e.stderr, str) else ""
        result["error"] = f"timeout after {timeout_sec}s"
    except OSError as e:
        result["returncode"] = -1
        result["error"] = str(e)
    return result


def _python_cmd() -> list[str]:
    return [os.environ.get("PYTHON_EXE", "python")]


def _truthy_env(name: str, default: str = "0") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _pytest_module_available(root: Path) -> bool:
    """Comprueba si `import pytest` funciona con el mismo intérprete que usará el bucle."""
    proc = subprocess.run(
        _python_cmd() + ["-c", "import pytest"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    return proc.returncode == 0


def _output_has_missing_pytest(step: dict[str, Any]) -> bool:
    blob = ((step.get("stdout") or "") + (step.get("stderr") or "")).lower()
    return "no module named pytest" in blob


def _syntax_module_files(root: Path) -> list[str]:
    names = [
        "auto_loop.py",
        "gemini_supervisor.py",
        "cursor_executor.py",
        "test_runner.py",
        "state_manager.py",
        "reporter.py",
    ]
    return [n for n in names if (root / n).is_file()]


def run_tests(project_root: Path | None = None) -> dict[str, Any]:
    """
    Ejecuta pytest, ruff (opcional) y script de simulación (opcional).
    Variables de entorno:
    - AUTO_LOOP_PYTEST: "1" (default) o "0" para omitir
    - AUTO_LOOP_REQUIRE_PYTEST: "1" para exigir pytest instalado (falla si falta el módulo)
    - AUTO_LOOP_PYTEST_ALLOW_NO_TESTS: "1" (default) trata pytest código 5 (sin tests) como éxito
    - AUTO_LOOP_SYNTAX_CHECK: "1" (default) ejecuta `python -m py_compile` sobre los módulos del bucle
    - AUTO_LOOP_RUFF: "1" para forzar, "0" para omitir (default omit si no hay ruff)
    - AUTO_LOOP_SIM_SCRIPT: ruta relativa a script opcional
    - AUTO_LOOP_TIMEOUT_PYTEST, AUTO_LOOP_TIMEOUT_RUFF, AUTO_LOOP_TIMEOUT_SIM
    """
    root = project_root or Path.cwd()
    out: dict[str, Any] = {"cwd": str(root.resolve()), "steps": []}

    def timeout(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            return max(1, int(raw))
        except ValueError:
            return default

    t_pytest = timeout("AUTO_LOOP_TIMEOUT_PYTEST", 600)
    t_ruff = timeout("AUTO_LOOP_TIMEOUT_RUFF", 300)
    t_sim = timeout("AUTO_LOOP_TIMEOUT_SIM", 600)
    t_syntax = timeout("AUTO_LOOP_TIMEOUT_SYNTAX", 120)

    if os.environ.get("AUTO_LOOP_PYTEST", "1").strip() not in ("0", "false", "False"):
        require_pytest = _truthy_env("AUTO_LOOP_REQUIRE_PYTEST", "0")
        step: dict[str, Any]
        if not require_pytest and not _pytest_module_available(root):
            logger.info(
                "test_runner: pytest omitido (módulo no instalado en %r). "
                "Instala con: pip install pytest. O AUTO_LOOP_REQUIRE_PYTEST=1 para fallar aquí.",
                _python_cmd()[0],
            )
            step = {
                "name": "pytest",
                "skipped": True,
                "note": (
                    "pytest no instalado en este intérprete; omitido. "
                    "`pip install pytest` en el venv o AUTO_LOOP_PYTEST=0."
                ),
            }
        else:
            cmd = _python_cmd() + ["-m", "pytest"]
            step = _run_one(cmd, root, t_pytest, "pytest")
            if (
                not require_pytest
                and not step.get("skipped")
                and not step.get("error")
                and step.get("returncode") == 1
                and _output_has_missing_pytest(step)
            ):
                logger.info(
                    "test_runner: `python -m pytest` falló por pytest ausente; marcando paso como omitido."
                )
                step = {
                    "name": "pytest",
                    "skipped": True,
                    "note": "pytest no disponible (salida: No module named pytest)",
                    "command": cmd,
                }
            else:
                rc = step.get("returncode")
                allow_no = _truthy_env("AUTO_LOOP_PYTEST_ALLOW_NO_TESTS", "1")
                if (
                    allow_no
                    and not step.get("skipped")
                    and not step.get("error")
                    and rc == 5
                ):
                    # pytest: 5 = no tests collected (proyecto sin suite aún)
                    step["note"] = "pytest exit 5 (sin tests recolectados) tratado como éxito"
                    step["returncode_effective"] = 0
        out["steps"].append(step)

    if _truthy_env("AUTO_LOOP_SYNTAX_CHECK", "1"):
        files = _syntax_module_files(root)
        if files:
            cmd = _python_cmd() + ["-m", "py_compile"] + files
            out["steps"].append(_run_one(cmd, root, t_syntax, "py_compile"))

    want_ruff = os.environ.get("AUTO_LOOP_RUFF", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not want_ruff:
        if (root / "ruff.toml").is_file() or (root / "pyproject.toml").is_file():
            # Si existe pyproject, intentar ruff solo si el binario existe
            want_ruff = shutil.which("ruff") is not None
    if want_ruff and shutil.which("ruff"):
        step = _run_one(["ruff", "check", "."], root, t_ruff, "ruff")
        out["steps"].append(step)

    sim = os.environ.get("AUTO_LOOP_SIM_SCRIPT", "").strip()
    if sim:
        script_path = (root / sim).resolve()
        if script_path.is_file():
            cmd = _python_cmd() + [str(script_path)]
            out["steps"].append(_run_one(cmd, root, t_sim, "simulation"))
        else:
            out["steps"].append(
                {
                    "name": "simulation",
                    "skipped": True,
                    "error": f"script not found: {sim}",
                }
            )

    overall_ok = True
    for s in out["steps"]:
        if s.get("skipped"):
            continue
        if s.get("error"):
            overall_ok = False
            break
        rc = s.get("returncode_effective", s.get("returncode"))
        if rc is None or int(rc) != 0:
            overall_ok = False
            break
    out["overall_ok"] = overall_ok
    logger.info(
        "test_runner: overall_ok=%s steps=%s",
        overall_ok,
        [s.get("name") for s in out["steps"]],
    )
    if not overall_ok:
        for s in out["steps"]:
            if s.get("skipped"):
                continue
            rc = s.get("returncode_effective", s.get("returncode"))
            if s.get("error") or rc not in (0, None):
                tail = ((s.get("stderr") or "") + (s.get("stdout") or ""))[-600:]
                logger.warning(
                    "test_runner paso en fallo: name=%s rc=%s err=%s tail=%r",
                    s.get("name"),
                    rc,
                    (s.get("error") or "")[:500],
                    tail,
                )
    return out


def truncate_text(s: str, max_chars: int = 24000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 80] + "\n\n... [truncado por longitud] ...\n"
