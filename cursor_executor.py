"""
Ejecuta `cursor-agent` (CLI) con la instrucción del supervisor y captura salida.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _resolve_cursor_executable(bin_name: str) -> str | None:
    """
    Resuelve la ruta del ejecutable de Cursor Agent.
    En Windows, `shutil.which('cursor-agent')` a veces falla si solo existe `.cmd` / ruta no está en PATH.
    """
    raw = (bin_name or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return str(p.resolve())
    w = shutil.which(raw)
    if w:
        return w
    if os.name == "nt":
        for suffix in (".exe", ".cmd", ".bat", ".ps1"):
            w = shutil.which(raw + suffix)
            if w:
                return w
    return None


def is_cursor_auth_failure(result: dict[str, Any]) -> bool:
    """Detecta el mensaje típico de cursor-agent cuando falta login o CURSOR_API_KEY."""
    parts = [
        str(result.get("stderr") or ""),
        str(result.get("stdout") or ""),
        str(result.get("error") or ""),
        str(result.get("truncated_json") or ""),
    ]
    blob = " ".join(parts).lower()
    return (
        "authentication required" in blob
        or "agent login" in blob
        or "cursor_api_key" in blob
    )


def run_cursor_agent(
    instruction: str,
    cwd: Path,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """
    Invoca el binario configurado (por defecto `cursor-agent`).

    Variables de entorno:
    - CURSOR_AGENT_BIN: nombre o ruta del ejecutable (default: cursor-agent)
    - CURSOR_AGENT_EXTRA_ARGS: argumentos extra en una sola cadena (se parsea con shlex)
    - CURSOR_AGENT_USE_STDIN: si "1", envía la instrucción por stdin (default "0")
    - CURSOR_AGENT_USE_FILE: si "1", escribe la instrucción en un archivo temporal y pasa su ruta
    - CURSOR_AGENT_FILE_FLAG: flag antes de la ruta (default: --prompt-file)
    - CURSOR_TIMEOUT_SEC: timeout de la ejecución
    """
    timeout = timeout_sec or _env_int("CURSOR_TIMEOUT_SEC", 3600)
    bin_name = os.environ.get("CURSOR_AGENT_BIN", "cursor-agent").strip() or "cursor-agent"
    resolved = _resolve_cursor_executable(bin_name)
    if not resolved:
        msg = (
            f"No se encontró el ejecutable de Cursor Agent ({bin_name!r}). "
            "En Windows suele ser WinError 2 si no está en PATH. "
            "Configura CURSOR_AGENT_BIN con la ruta absoluta al ejecutable "
            "(por ejemplo donde instalaste la CLI de Cursor)."
        )
        logger.error(msg)
        return {
            "ok": False,
            "returncode": -2,
            "stdout": "",
            "stderr": "",
            "command": [bin_name],
            "error": msg,
        }

    extra = os.environ.get("CURSOR_AGENT_EXTRA_ARGS", "").strip()
    if extra:
        extra_list = shlex.split(extra, posix=os.name == "posix")
    else:
        extra_list = []

    use_file = os.environ.get("CURSOR_AGENT_USE_FILE", "").lower() in ("1", "true", "yes")
    use_stdin = os.environ.get("CURSOR_AGENT_USE_STDIN", "0").lower() in ("1", "true", "yes")

    cmd: list[str] = [resolved] + extra_list
    stdin_val: str | None = None
    tmp_path: Path | None = None

    try:
        if use_file:
            fd, tmp = tempfile.mkstemp(prefix="cursor_instr_", suffix=".txt", text=True)
            tmp_path = Path(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(instruction)
            flag = os.environ.get("CURSOR_AGENT_FILE_FLAG", "--prompt-file").strip() or "--prompt-file"
            cmd.extend([flag, str(tmp_path)])
        elif use_stdin:
            stdin_val = instruction
        else:
            cmd.append(instruction)

        logger.info("Ejecutando Cursor Agent: cwd=%s exe=%s", cwd, resolved)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            input=stdin_val,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        err_msg: str | None = None
        if proc.returncode != 0:
            err_msg = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            if not err_msg:
                err_msg = f"exit status {proc.returncode}"
            else:
                err_msg = err_msg[:8000]
            logger.warning(
                "Cursor Agent terminó con código %s. stderr (primeras 2k): %s",
                proc.returncode,
                (proc.stderr or "")[:2000],
            )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "command": cmd,
            "executable_resolved": resolved,
            "error": err_msg,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": -124,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "command": cmd,
            "executable_resolved": resolved,
            "error": f"timeout after {timeout}s",
        }
    except OSError as e:
        err = str(e)
        if "WinError 2" in err or isinstance(e, FileNotFoundError):
            err = (
                f"{err}. Comprueba CURSOR_AGENT_BIN / PATH. "
                "Ejecutable resuelto previamente; si el agente delega en otro binario ausente, "
                "revisa la instalación de la CLI de Cursor."
            )
        logger.error("No se pudo ejecutar Cursor Agent: %s", err)
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "command": cmd,
            "executable_resolved": resolved,
            "error": err,
        }
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
