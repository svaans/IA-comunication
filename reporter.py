"""
Reportes por iteración y estado actual en ai_reports/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPORTS_DIR = Path("ai_reports")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_iteration_report(
    iteration: int,
    state: dict[str, Any],
    gemini_raw: str,
    path: Path | None = None,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = path or (REPORTS_DIR / f"iteration_{iteration:03d}.md")
    lines = [
        f"# Iteración {iteration}",
        "",
        f"- Generado (UTC): `{_utc_now_iso()}`",
        f"- Estado: `{state.get('status', '')}`",
        "",
        "## Estado resumido",
        "",
        "```json",
        _safe_json_snippet(state),
        "```",
        "",
        "## Salida del supervisor (Gemini)",
        "",
        "```text",
        (gemini_raw or "").strip() or "(vacío)",
        "```",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_current_status(
    iteration: int,
    state: dict[str, Any],
    gemini_summary: str,
    path: Path | None = None,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = path or (REPORTS_DIR / "current_status.md")
    body = [
        "# Estado actual del bucle autónomo",
        "",
        f"- Última actualización (UTC): `{_utc_now_iso()}`",
        f"- Iteración: **{iteration}**",
        f"- Estado: **{state.get('status', '')}**",
        "",
        "## Instrucción en curso / última",
        "",
        "```text",
        (state.get("last_instruction") or "").strip() or "(ninguna)",
        "```",
        "",
        "## Resumen del supervisor",
        "",
        gemini_summary.strip() or "(sin resumen)",
        "",
        "## Riesgos abiertos",
        "",
    ]
    risks = state.get("open_risks") or []
    if isinstance(risks, list) and risks:
        for r in risks:
            body.append(f"- {r}")
    else:
        body.append("- (ninguno registrado)")
    body.extend(
        [
            "",
            "## Nota de gobernanza",
            "",
            "Solo el usuario puede declarar que el bot funciona en entorno real tras pruebas manuales.",
            "Este sistema no afirma finalización ni idoneidad para producción.",
            "",
        ]
    )
    out.write_text("\n".join(body), encoding="utf-8")
    return out


def _safe_json_snippet(state: dict[str, Any]) -> str:
    import json

    try:
        return json.dumps(state, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return "{}"
