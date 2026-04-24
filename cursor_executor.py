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


def _env_truthy(name: str, default: str = "0") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _extra_has_trust_or_yolo(extra: list[str]) -> bool:
    blob = " ".join(extra).lower()
    return "--trust" in blob or "--yolo" in blob


def _argv_has_explicit_model(argv: list[str]) -> bool:
    for tok in argv:
        if tok == "--model" or tok.startswith("--model="):
            return True
    return False


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


def is_cursor_usage_exhausted(result: dict[str, Any]) -> bool:
    """
    Detecta respuesta de Cursor por cuota / límite de uso de la cuenta (no es un bug del repo).
    """
    parts = [
        str(result.get("stderr") or ""),
        str(result.get("stdout") or ""),
        str(result.get("error") or ""),
        str(result.get("truncated_json") or ""),
    ]
    blob = " ".join(parts).lower()
    return (
        "out of usage" in blob
        or "you're out of usage" in blob
        or "increase your limit" in blob
        or "ask your admin to increase" in blob
        or "switch to auto" in blob
    )


def _result_from_proc(
    proc: Any,
    cmd: list[str],
    resolved: str,
) -> dict[str, Any]:
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
    - CURSOR_AGENT_TRUST_WORKSPACE: si "1" (default), añade flags de confianza del workspace para modo no interactivo
    - CURSOR_AGENT_TRUST_FLAGS: argumentos de confianza (default: --trust). Solo se añaden si no vienen ya en EXTRA_ARGS
    - CURSOR_AGENT_AUTO_MODEL_FALLBACK: si "1" (default), ante cuota agotada / "Switch to Auto" reintenta con `--model <nombre>`
    - CURSOR_AGENT_AUTO_MODEL_NAME: modelo de respaldo (default: auto)
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

    trust_prefix: list[str] = []
    if _env_truthy("CURSOR_AGENT_TRUST_WORKSPACE", "1") and not _extra_has_trust_or_yolo(extra_list):
        trust_raw = os.environ.get("CURSOR_AGENT_TRUST_FLAGS", "--trust").strip()
        if trust_raw:
            try:
                trust_prefix.extend(shlex.split(trust_raw, posix=os.name == "posix"))
            except ValueError:
                logger.warning("CURSOR_AGENT_TRUST_FLAGS no se pudo parsear; omitiendo inyección de confianza")

    stdin_val: str | None = None
    tmp_path: Path | None = None
    tail: list[str] = []
    last_cmd: list[str] = [resolved]

    try:
        if use_file:
            fd, tmp = tempfile.mkstemp(prefix="cursor_instr_", suffix=".txt", text=True)
            tmp_path = Path(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(instruction)
            flag = os.environ.get("CURSOR_AGENT_FILE_FLAG", "--prompt-file").strip() or "--prompt-file"
            tail = [flag, str(tmp_path)]
        elif use_stdin:
            stdin_val = instruction
        else:
            tail = [instruction]

        def build_cmd(model_args: list[str]) -> list[str]:
            return [resolved] + trust_prefix + model_args + extra_list + tail

        logger.info("Ejecutando Cursor Agent: cwd=%s exe=%s", cwd, resolved)
        cmd0 = build_cmd([])
        last_cmd = cmd0
        proc0 = subprocess.run(
            cmd0,
            cwd=str(cwd),
            input=stdin_val,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        res0 = _result_from_proc(proc0, cmd0, resolved)
        if res0.get("ok"):
            return res0

        want_fb = _env_truthy("CURSOR_AGENT_AUTO_MODEL_FALLBACK", "1")
        head_for_model_check = [resolved] + trust_prefix + extra_list
        if (
            want_fb
            and is_cursor_usage_exhausted(res0)
            and not _argv_has_explicit_model(head_for_model_check)
        ):
            auto_name = os.environ.get("CURSOR_AGENT_AUTO_MODEL_NAME", "auto").strip() or "auto"
            model_args = ["--model", auto_name]
            cmd1 = build_cmd(model_args)
            last_cmd = cmd1
            logger.info(
                "Reintentando Cursor Agent con --model %s (cuota del modelo actual / mensaje Switch to Auto).",
                auto_name,
            )
            proc1 = subprocess.run(
                cmd1,
                cwd=str(cwd),
                input=stdin_val,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            res1 = _result_from_proc(proc1, cmd1, resolved)
            if res1.get("ok"):
                res1["model_fallback"] = auto_name
                res1["note"] = (
                    f"Se aplicó automáticamente `--model {auto_name}` tras límite de uso del modelo por defecto."
                )
                logger.info(
                    "Cursor Agent OK tras fallback de modelo (--model %s).",
                    auto_name,
                )
            return res1

        return res0
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": -124,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "command": last_cmd,
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
            "command": last_cmd,
            "executable_resolved": resolved,
            "error": err,
        }
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
