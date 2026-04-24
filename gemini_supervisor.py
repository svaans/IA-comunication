"""
Cliente Gemini (REST) para supervisión técnica del bucle de mejora continua.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

GEMINI_RULE_QUOTE = (
    "No pidas validación humana para continuar. Si algo parece requerir validación humana, "
    "conviértelo en hipótesis verificable, test automático, simulación, auditoría o riesgo documentado. "
    "Continúa trabajando hasta que el usuario detenga el proceso. Solo el usuario puede declarar que el "
    "bot funciona en entorno real."
)

SYSTEM_INSTRUCTION = f"""Eres el supervisor técnico de un repositorio de software. Orquestas mejoras pequeñas y seguras.

Hechos sobre ESTE proyecto (no los contradigas ni inventes sustitutos):
- `gemini_supervisor.py` es un módulo basado en `requests` contra la API REST de Gemini (`generateContent`). No asumas LangChain, `ChatGoogleGenerativeAI`, `google.generativeai.GenerativeModel`, ni una clase `GeminiSupervisor` salvo que el extracto de código adjunto en el mensaje de usuario lo muestre explícitamente.
- El bucle vive en `auto_loop.py` y orquesta funciones importadas; no reescribas la arquitectura completa en una sola iteración.
- La CLI `cursor-agent` requiere autenticación en la máquina donde corre: `cursor-agent login` o variable de entorno `CURSOR_API_KEY` (no confundir con `GEMINI_API_KEY`). Si el error lo indica, no pidas credenciales en el chat: documenta el procedimiento o hipótesis de verificación sin bloquear el flujo.
- Si `last_cursor_result` menciona cuota agotada (`out of usage`, `increase your limit`), es un límite de la cuenta o plan de **Cursor**, no un defecto del código del repo; documenta el riesgo y propón trabajo verificable sin depender de Cursor hasta que haya cuota.
- Si `last_cursor_result` indica fallo al invocar `cursor-agent`, prioriza hipótesis de entorno (PATH, ruta del ejecutable, flags de CLI, login / API key de Cursor, cuotas de Cursor) y mejoras al orquestador o documentación, en lugar de inventar clases que no existen en el repo.

Reglas de gobernanza (obligatorias):
- NUNCA afirmes que el bot está terminado, listo para producción o validado en entorno real. El sistema no certifica producción.
- No pidas al operador que confirme si el bot "funciona en real"; eso solo lo decide el usuario probando el bot real.
- No modifiques ni solicites modificar claves API, secretos o `.env` reales.
- No solicites operar en trading real ni desactivar salvaguardas de riesgo sin dejar riesgos documentados explícitos.
- No solicites borrar archivos críticos del proyecto.
- No solicites commits automáticos a la rama principal; prefiere rama de trabajo o sandbox y un cambio pequeño por iteración.
- {GEMINI_RULE_QUOTE}

Formato obligatorio de tu respuesta (usa exactamente estos encabezados numerados en español):

1. Objetivo de la iteración
2. Archivos a revisar
3. Cambios exactos solicitados a Cursor
4. Tests o comandos a ejecutar
5. Riesgos a documentar
6. Criterio automático de avance

Sé concreto: rutas de archivo, comportamiento esperado, y límites del cambio (una mejora acotada por iteración).
"""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _project_snapshot(root: Path, max_files: int = 120) -> str:
    skip = {
        ".git",
        "venv",
        ".venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
    lines: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root)
        if str(rel) == ".":
            rel_s = "."
        else:
            rel_s = str(rel).replace("\\", "/")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in (".pyc", ".pyo", ".dll", ".exe", ".zip"):
                continue
            lines.append(f"{rel_s}/{name}".replace("./", ""))
            count += 1
            if count >= max_files:
                lines.append("... [lista truncada]")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(sin archivos listados)"


def _read_file_head(path: Path, max_lines: int = 55) -> str:
    if not path.is_file():
        return f"(no existe: {path.name})"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(no se pudo leer {path.name}: {e})"
    head = lines[:max_lines]
    body = "\n".join(head)
    if len(lines) > max_lines:
        body += f"\n... [{len(lines) - max_lines} líneas omitidas]"
    return body


def build_code_context(project_root: Path) -> str:
    """Extractos reales para reducir alucinaciones del modelo sobre la estructura del código."""
    files = [
        "gemini_supervisor.py",
        "auto_loop.py",
        "cursor_executor.py",
        "test_runner.py",
    ]
    chunks: list[str] = []
    for name in files:
        p = project_root / name
        chunks.append(f"### Extracto: `{name}` (primeras líneas)\n```python\n{_read_file_head(p)}\n```")
    return "\n\n".join(chunks)


def build_user_prompt(
    project_root: Path,
    state: dict[str, Any],
    test_result: dict[str, Any],
    cursor_result: dict[str, Any],
) -> str:
    snap = _project_snapshot(project_root)
    code_ctx = build_code_context(project_root)
    parts = [
        "## Contexto del repositorio",
        f"Raíz del proyecto: `{project_root.resolve()}`",
        "",
        "### Listado parcial de archivos",
        "```text",
        snap,
        "```",
        "",
        "## Extractos de código (lee antes de proponer cambios; no inventes APIs que no aparezcan aquí)",
        code_ctx,
        "",
        "## Estado persistido (JSON)",
        "```json",
        json.dumps(state, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Último resultado de Cursor Agent",
        "```json",
        json.dumps(cursor_result, ensure_ascii=False, indent=2)[:48000],
        "```",
        "",
        "## Último resultado de tests / comandos",
        "```json",
        json.dumps(test_result, ensure_ascii=False, indent=2)[:48000],
        "```",
        "",
        "Propon la siguiente iteración siguiendo el formato obligatorio.",
    ]
    return "\n".join(parts)


def parse_supervisor_response(text: str) -> dict[str, Any]:
    """Extrae secciones numeradas 1–6 del texto del modelo."""
    out: dict[str, Any] = {
        "objective": "",
        "files": "",
        "cursor_changes": "",
        "tests": "",
        "risks": "",
        "advance": "",
        "raw": text or "",
    }
    if not text:
        return out
    pattern = re.compile(r"^\s*(\d+)\.\s*", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        out["cursor_changes"] = text.strip()
        return out
    for i, m in enumerate(matches):
        section_num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        key = {
            1: "objective",
            2: "files",
            3: "cursor_changes",
            4: "tests",
            5: "risks",
            6: "advance",
        }.get(section_num)
        if key:
            out[key] = chunk
    return out


def extract_risks_bullets(risks_text: str) -> list[str]:
    lines = []
    for line in (risks_text or "").splitlines():
        s = line.strip()
        if s.startswith(("-", "*")):
            lines.append(s.lstrip("-* ").strip())
        elif re.match(r"^\d+\.", s):
            lines.append(re.sub(r"^\d+\.\s*", "", s).strip())
    return [x for x in lines if x][:50]


def _gemini_http_error_summary(resp: Any) -> tuple[str, str]:
    """
    Devuelve (mensaje_corto, cuerpo_detalle) a partir de una respuesta HTTP de error.
    No incluye secretos; solo texto devuelto por la API.
    """
    code = getattr(resp, "status_code", "?")
    raw = (getattr(resp, "text", None) or "")[:8000]
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            msg = (err.get("message") or "").strip()
            status = (err.get("status") or "").strip()
            if msg:
                bits = [f"HTTP {code}"]
                if status:
                    bits.append(status)
                bits.append(msg)
                return (": ".join(bits), raw)
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
        pass
    return (f"HTTP {code}", raw)


def call_gemini(
    user_prompt: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    load_dotenv()
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("Falta GEMINI_API_KEY en el entorno o en .env")
    mdl = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    timeout = timeout_sec or _env_int("GEMINI_TIMEOUT_SEC", 120)
    base = os.environ.get(
        "GEMINI_API_BASE",
        "https://generativelanguage.googleapis.com",
    ).rstrip("/")
    url = f"{base}/v1beta/models/{mdl}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
    }
    logger.info("Llamando a Gemini model=%s timeout=%ss", mdl, timeout)
    try:
        resp = requests.post(
            url,
            params={"key": key},
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        logger.exception("Error de red al llamar a Gemini")
        return {"ok": False, "error": str(e), "text": "", "parsed": parse_supervisor_response("")}
    if resp.status_code != 200:
        summary, detail = _gemini_http_error_summary(resp)
        logger.error("Gemini %s | cuerpo: %s", summary, resp.text[:2000])
        return {
            "ok": False,
            "error": summary,
            "detail": detail,
            "text": "",
            "parsed": parse_supervisor_response(""),
        }
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return {"ok": False, "error": "respuesta JSON inválida", "text": "", "parsed": {}}
    text = ""
    try:
        candidates = data.get("candidates") or []
        if candidates:
            parts = (
                (candidates[0].get("content") or {}).get("parts") or []
            )
            if parts and isinstance(parts[0], dict):
                text = parts[0].get("text") or ""
    except (IndexError, TypeError, AttributeError):
        text = ""
    parsed = parse_supervisor_response(text)
    parsed["raw"] = text
    return {"ok": True, "text": text, "parsed": parsed, "raw_response": data}


def compose_cursor_instruction(parsed: dict[str, Any]) -> str:
    """Instrucción única enviada a Cursor Agent."""
    blocks = [
        "### Objetivo",
        (parsed.get("objective") or "").strip() or "(sin objetivo explícito)",
        "",
        "### Archivos a revisar",
        (parsed.get("files") or "").strip() or "(no especificado)",
        "",
        "### Cambios exactos (aplicar en el repo, un cambio pequeño)",
        (parsed.get("cursor_changes") or "").strip() or "(sin detalle)",
        "",
        "### Restricciones",
        "- No tocar claves API ni `.env` con secretos.",
        "- No operar en trading real ni relajar riesgo sin documentar riesgos.",
        "- No borrar archivos críticos.",
        "- No hacer commit a main; preferir rama sandbox si aplica.",
        "- No declarar el bot listo para producción.",
    ]
    return "\n".join(blocks).strip()
