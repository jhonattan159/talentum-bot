# ats_semantic.py
"""Capa semántica LLM + formateadores de reporte del motor híbrido de
diagnóstico ATS de TALENTUM GROUP.

1. `semantic_review(cv_text, job_posting_text=None)` — revisión cualitativa
   vía Gemini (reutiliza `brain._call_gemini`). Nunca inventa datos del CV,
   nunca emite un "puntaje ATS" universal y nunca promete predecir el
   puntaje de un ATS específico. Devuelve dict o None; nunca lanza.
2. `format_whatsapp(report)` — mensaje corto de WhatsApp desde el reporte
   final. Tono ejecutivo/minimalista/premium, sin clichés motivacionales.
3. `render_html(report)` — reporte HTML de una página con CSS inline,
   paleta navy #0A1F44 / dorado #C9A227. Sin assets externos.

Convenciones: salidas en español, stdlib + requests (vía brain),
sin registro de secretos, las funciones nunca lanzan a sus llamadores.
"""

import html
import json
import logging
import re
from datetime import date

from brain import _call_gemini

log = logging.getLogger("talentum.ats_semantic")

# ---------------------------------------------------------------------------
# Límites de entrada para la capa semántica
# ---------------------------------------------------------------------------

CV_MAX_CHARS = 6000
POSTING_MAX_CHARS = 2000

# Topes de salida de la revisión semántica
MAX_HALLAZGOS = 6
MAX_FORTALEZAS = 5
MAX_ACCIONES = 5

# ---------------------------------------------------------------------------
# Prompt del revisor semántico
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_SEMANTIC = """Actúas como un reclutador técnico senior con más de 20 años en
selección multinacional. Analizas un CV frente a un aviso laboral (si se
incluye uno) para una auditoría de legibilidad ATS y calidad de contenido.

Dimensiones a analizar:
1. Encaje de seniority: ¿el perfil se lee junior / mid / senior frente al
   aviso (si se da)? No inventes años de experiencia que no estén escritos.
2. Calidad de los logros: ¿los bullets orientan a resultados medibles o son
   listas de tareas? ¿Hay métricas, alcance, contexto?
3. Keywords en contexto: ¿los términos técnicos aparecen de forma natural,
   están ausentes frente al aviso, o están forzados/keyword stuffing?
4. Claridad y estructura: ¿orden lógico, fechas consistentes, legibilidad?

Reglas de honestidad, obligatorias y no negociables:
- NUNCA inventes datos del CV: si un dato no está escrito, no lo afirmes.
- Marca explícitamente lo que "no se pudo determinar".
- NUNCA emitas un puntaje ATS universal ni afirmes predecir el puntaje de
  un ATS específico (Workday, Taleo, Greenhouse, iCIMS, etc.).
- No des consejos genéricos: cada acción debe atacar un hallazgo concreto.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional y sin
bloques de código, con EXACTAMENTE estas claves:
{
  "encaje_seniority": "texto breve",
  "calidad_logros": "texto breve",
  "keywords_contexto": {
    "presentes": ["keyword en contexto natural", "..."],
    "faltantes": ["keyword del aviso ausente en el CV", "..."],
    "nota": "texto breve sobre uso natural vs forzado"
  },
  "claridad": "texto breve",
  "hallazgos": [
    {
      "criterio": "criterio afectado",
      "severidad": "critico|medio|leve",
      "hallazgo": "qué se encontró, con evidencia concreta del CV",
      "como_arreglarlo": "corrección concreta y accionable"
    }
  ],
  "fortalezas": ["fortaleza concreta y verificable", "..."],
  "acciones": ["acción concreta, ordenada por impacto", "..."]
}"""


def _build_user_contents(cv_text, job_posting_text):
    cv = (cv_text or "")[:CV_MAX_CHARS]
    parts = ["CV a analizar:\n---\n" + cv + "\n---"]
    if job_posting_text and job_posting_text.strip():
        posting = job_posting_text.strip()[:POSTING_MAX_CHARS]
        parts.append("Aviso laboral de referencia:\n---\n" + posting + "\n---")
    else:
        parts.append("No se proporcionó aviso laboral: evalúa el CV en forma autónoma.")
    return [{"role": "user", "parts": [{"text": "\n\n".join(parts)}]}]


def _parse_json_object(text):
    """Extrae un objeto JSON del texto, tolerando fences de código.

    Devuelve {} si no hay un objeto JSON válido.
    """
    if not text:
        return {}
    cleaned = text.strip()
    # Quita fences ```json ... ``` o ``` ... ```
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except (ValueError, TypeError):
        # Recorta desde la primera { hasta la última }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            obj = json.loads(cleaned[start : end + 1])
        except (ValueError, TypeError):
            return {}
    return obj if isinstance(obj, dict) else {}


def _sanitize_semantic(data):
    """Valida y recorta la salida del LLM. Devuelve dict o None."""
    if not isinstance(data, dict):
        return None

    def _str(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _str_list(v, cap):
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            s = _str(item)
            if s:
                out.append(s)
            if len(out) >= cap:
                break
        return out

    encaje = _str(data.get("encaje_seniority"))
    calidad = _str(data.get("calidad_logros"))
    claridad = _str(data.get("claridad"))
    if not (encaje and calidad and claridad):
        log.warning("Revisión semántica incompleta: faltan campos clave.")
        return None

    kw = data.get("keywords_contexto")
    if not isinstance(kw, dict):
        log.warning("Revisión semántica sin keywords_contexto.")
        return None
    kw_out = {
        "presentes": _str_list(kw.get("presentes"), 8),
        "faltantes": _str_list(kw.get("faltantes"), 8),
        "nota": _str(kw.get("nota")) or "no se pudo determinar",
    }

    hallazgos = []
    raw_h = data.get("hallazgos")
    if isinstance(raw_h, list):
        for item in raw_h:
            if not isinstance(item, dict):
                continue
            sev = item.get("severidad")
            if sev not in ("critico", "medio", "leve"):
                continue
            criterio = _str(item.get("criterio"))
            hallazgo = _str(item.get("hallazgo"))
            fix = _str(item.get("como_arreglarlo"))
            if not (criterio and hallazgo and fix):
                continue
            hallazgos.append(
                {
                    "criterio": criterio,
                    "severidad": sev,
                    "hallazgo": hallazgo,
                    "como_arreglarlo": fix,
                }
            )
            if len(hallazgos) >= MAX_HALLAZGOS:
                break

    out = {
        "encaje_seniority": encaje,
        "calidad_logros": calidad,
        "keywords_contexto": kw_out,
        "claridad": claridad,
        "hallazgos": hallazgos,
        "fortalezas": _str_list(data.get("fortalezas"), MAX_FORTALEZAS),
        "acciones": _str_list(data.get("acciones"), MAX_ACCIONES),
    }
    return out


def semantic_review(cv_text, job_posting_text=None):
    """Revisión semántica LLM del CV (opcionalmente contra un aviso).

    Devuelve dict con la estructura del prompt, o None si el LLM falló,
    la respuesta no es JSON válido o la sanitización la rechaza.
    Nunca lanza excepciones.
    """
    try:
        if not cv_text or not cv_text.strip():
            return None
        contents = _build_user_contents(cv_text, job_posting_text)
        text = _call_gemini(
            contents, SYSTEM_PROMPT_SEMANTIC, temperature=0.3, max_tokens=1200
        )
        if not text:
            return None
        data = _parse_json_object(text)
        if not data:
            log.warning("La respuesta semántica no contenía JSON válido.")
            return None
        return _sanitize_semantic(data)
    except Exception:
        log.exception("Fallo inesperado en semantic_review")
        return None


# ---------------------------------------------------------------------------
# Formateador WhatsApp
# ---------------------------------------------------------------------------

BANNED_CLICHES = (
    "desbloquea tu potencial",
    "haz brillar",
    "tu mejor versión",
    "descubre tu talento",
    "alcanza tus sueños",
    "el trabajo de tus sueños",
    "impulsa tu carrera",
    "sé la mejor versión",
)

METHODOLOGY_SHORT = "rúbrica interna TALENTUM, no es puntaje de ningún ATS"


def _verdict(indice):
    if indice is None:
        return "Diagnóstico listo."
    if indice >= 75:
        return "Tu CV está en forma: pequeños ajustes lo dejan listo."
    if indice >= 50:
        return "Tu CV tiene base sólida y brechas claras que corregir."
    return "Tu CV necesita una reestructuración a fondo para pasar filtros ATS."


def format_whatsapp(report):
    """Formatea el reporte final como mensaje de WhatsApp (~1300 chars máx)."""
    try:
        if not isinstance(report, dict):
            return ""

        indice = report.get("indice_diagnostico")
        try:
            indice = int(indice) if indice is not None else None
        except (TypeError, ValueError):
            indice = None

        acciones = report.get("acciones") or []
        acciones = [a for a in acciones if isinstance(a, str) and a.strip()][:3]
        fortalezas = report.get("fortalezas") or []
        fortaleza = next(
            (f for f in fortalezas if isinstance(f, str) and f.strip()), None
        )

        lines = []
        lines.append("📋 *Diagnóstico ATS — TALENTUM GROUP*")
        lines.append(_verdict(indice))
        if indice is not None:
            lines.append(
                "Índice TALENTUM: *{}/100* ({}).".format(indice, METHODOLOGY_SHORT)
            )
        else:
            lines.append("Índice no disponible en esta revisión.")

        if acciones:
            lines.append("")
            lines.append("*Top 3 acciones:*")
            for i, acc in enumerate(acciones, 1):
                acc = acc.strip()
                if len(acc) > 280:
                    acc = acc[:277].rstrip() + "…"
                lines.append("{}. {}".format(i, acc))

        if fortaleza:
            f = fortaleza.strip()
            if len(f) > 220:
                f = f[:217].rstrip() + "…"
            lines.append("")
            lines.append("✓ *Fortaleza:* " + f)

        lines.append("")
        lines.append(
            "Un asesor puede aplicar esto por ti: responde *ASESOR*."
        )

        msg = "\n".join(lines)
        for cliche in BANNED_CLICHES:
            if cliche.lower() in msg.lower():
                log.warning("Cliché prohibido detectado en mensaje WhatsApp: %r", cliche)
        if len(msg) > 1300:
            msg = msg[:1297].rstrip() + "…"
        return msg
    except Exception:
        log.exception("Fallo inesperado en format_whatsapp")
        return ""


# ---------------------------------------------------------------------------
# Reporte HTML
# ---------------------------------------------------------------------------

NAVY = "#0A1F44"
GOLD = "#C9A227"
SEVERITY_COLORS = {
    "critico": "#B42318",  # rojo oscuro
    "medio": "#B54708",  # ámbar oscuro
    "leve": "#067647",  # verde oscuro
}
SEVERITY_LABELS = {
    "critico": "Crítico",
    "medio": "Medio",
    "leve": "Leve",
}

METHODOLOGY_NOTE = (
    "Metodología: rúbrica interna TALENTUM (análisis automático de estructura y "
    "parseo + revisión semántica de experto). Este índice no es el puntaje de "
    "ningún ATS ni predice el puntaje de un ATS específico."
)


def _esc(v):
    return html.escape(str(v), quote=True)


def render_html(report):
    """Genera el reporte de diagnóstico como HTML autocontenido (CSS inline)."""
    try:
        if not isinstance(report, dict):
            report = {}

        resumen = report.get("resumen") or ""
        indice = report.get("indice_diagnostico")
        criterios = [
            c for c in (report.get("criterios") or []) if isinstance(c, dict)
        ]
        fortalezas = [
            f for f in (report.get("fortalezas") or [])
            if isinstance(f, str) and f.strip()
        ]
        acciones = [
            a for a in (report.get("acciones") or [])
            if isinstance(a, str) and a.strip()
        ]
        sem_disp = report.get("revision_semantica_disponible", False)
        nota = report.get("nota_metodologia") or METHODOLOGY_NOTE

        indice_html = _esc(indice) if indice is not None else "—"

        # Filas de la tabla de criterios
        rows = []
        for c in criterios:
            sev = c.get("severidad")
            color = SEVERITY_COLORS.get(sev, "#555555")
            label = SEVERITY_LABELS.get(sev, _esc(sev or "—"))
            rows.append(
                "<tr>"
                "<td>{}</td>"
                '<td><span style="display:inline-block;padding:2px 10px;'
                'border-radius:999px;color:#ffffff;background-color:{};'
                'font-size:12px;font-weight:600;">{}</span></td>'
                "<td>{}</td>"
                "<td>{}</td>"
                "</tr>".format(
                    _esc(c.get("criterio", "—")),
                    color,
                    label,
                    _esc(c.get("hallazgo", "—")),
                    _esc(c.get("como_arreglarlo", "—")),
                )
            )
        if not rows:
            rows.append(
                '<tr><td colspan="4" style="color:#666;">'
                "Sin criterios registrados en esta revisión.</td></tr>"
            )
        criterios_html = "\n".join(rows)

        fortalezas_html = "".join(
            "<li>{}</li>".format(_esc(f)) for f in fortalezas
        ) or '<li style="color:#666;">Sin fortalezas registradas.</li>'
        acciones_html = "".join(
            "<li>{}</li>".format(_esc(a)) for a in acciones
        ) or '<li style="color:#666;">Sin acciones registradas.</li>'

        sem_note = (
            "Revisión semántica por experto: incluida."
            if sem_disp
            else "Revisión semántica por experto: no disponible en esta revisión "
            "(se aplicó solo el análisis automático)."
        )

        today = date.today().strftime("%d/%m/%Y")

        return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diagnóstico ATS — TALENTUM GROUP</title>
</head>
<body style="margin:0;padding:0;background-color:#F5F6F8;
font-family:Arial,Helvetica,sans-serif;color:#1A1A1A;">
<div style="max-width:760px;margin:24px auto;background:#FFFFFF;
border-top:6px solid {navy};">
  <div style="background-color:{navy};color:#FFFFFF;padding:28px 32px;">
    <div style="font-size:12px;letter-spacing:3px;color:{gold};
    font-weight:600;margin-bottom:8px;">TALENTUM GROUP</div>
    <h1 style="margin:0;font-size:24px;font-weight:700;">Diagnóstico ATS</h1>
    <p style="margin:8px 0 0;font-size:14px;color:#D7DDE8;">{resumen}</p>
  </div>

  <div style="padding:28px 32px;">
    <h2 style="font-size:16px;color:{navy};margin:0 0 12px;
    text-transform:uppercase;letter-spacing:1px;">Índice del diagnóstico</h2>
    <div style="display:flex;align-items:center;gap:24px;margin-bottom:8px;">
      <div style="font-size:56px;font-weight:800;color:{navy};
      line-height:1;">{indice}<span style="font-size:24px;color:#888;">/100</span></div>
      <div style="flex:1;">
        <div style="height:10px;background:#E8EAF0;border-radius:999px;">
          <div style="height:10px;background:{gold};border-radius:999px;
          width:{pct}%;"></div>
        </div>
        <p style="font-size:12px;color:#666;margin:8px 0 0;">{nota}</p>
      </div>
    </div>
    <p style="font-size:12px;color:#888;margin:4px 0 0;">{sem_note}</p>

    <h2 style="font-size:16px;color:{navy};margin:28px 0 12px;
    text-transform:uppercase;letter-spacing:1px;">Criterios evaluados</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background-color:{navy};color:#FFFFFF;">
          <th style="text-align:left;padding:10px 12px;">Criterio</th>
          <th style="text-align:left;padding:10px 12px;">Severidad</th>
          <th style="text-align:left;padding:10px 12px;">Hallazgo</th>
          <th style="text-align:left;padding:10px 12px;">Cómo arreglarlo</th>
        </tr>
      </thead>
      <tbody>
        {criterios}
      </tbody>
    </table>

    <h2 style="font-size:16px;color:{navy};margin:28px 0 12px;
    text-transform:uppercase;letter-spacing:1px;">Fortalezas</h2>
    <ul style="font-size:13px;line-height:1.6;margin:0;padding-left:20px;">
      {fortalezas}
    </ul>

    <h2 style="font-size:16px;color:{navy};margin:28px 0 12px;
    text-transform:uppercase;letter-spacing:1px;">Acciones priorizadas</h2>
    <ol style="font-size:13px;line-height:1.6;margin:0;padding-left:20px;">
      {acciones}
    </ol>
  </div>

  <div style="background-color:#F0F1F5;padding:16px 32px;font-size:11px;
  color:#888;border-top:1px solid #E2E4EA;">
    <div>TALENTUM GROUP — Consultoría de optimización de CVs · {today}</div>
    <div style="margin-top:4px;">{nota2}</div>
  </div>
</div>
</body>
</html>""".format(
            navy=NAVY,
            gold=GOLD,
            resumen=_esc(resumen),
            indice=indice_html,
            pct=int(indice) if isinstance(indice, (int, float)) else 0,
            nota=_esc(nota),
            sem_note=_esc(sem_note),
            criterios=criterios_html,
            fortalezas=fortalezas_html,
            acciones=acciones_html,
            today=today,
            nota2=_esc(nota),
        )
    except Exception:
        log.exception("Fallo inesperado en render_html")
        return ""
