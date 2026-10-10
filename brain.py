# brain.py
"""Cerebro LLM del chatbot de WhatsApp de TALENTUM GROUP.

Usa la API de Google Gemini vía REST directo (módulo `requests`, sin SDK
pesado). Lee GEMINI_API_KEY y GEMINI_MODEL del entorno en cada llamada
(para que los tests puedan inyectarlas sin reimportar).

Contrato:
    generate_reply(history, user_message) -> str | None
        history: lista de {"role": "user"|"model", "text": str}
        Devuelve el texto de respuesta, o None si la API falló / no hay key.
        NUNCA lanza excepciones: cualquier error se registra y devuelve None.

    extract_lead(history) -> dict
        Extrae datos estructurados del lead desde el historial.
        Devuelve dict (posiblemente vacío); nunca lanza excepciones.

Prueba local:
    GEMINI_API_KEY=falsa python3 -c "import brain; print(brain.generate_reply([], 'hola'))"
    # Debe imprimir None y loguear el error, sin traceback.
"""

import json
import logging
import os
import re

import requests

log = logging.getLogger("talentum.brain")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.0-flash"
TIMEOUT_SECS = 20

# ---------------------------------------------------------------------------
# Prompt del sistema (identidad, tono, tarifario, objetivos)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Eres el asistente de WhatsApp de TALENTUM GROUP, consultoría de ingeniería \
de perfiles profesionales. Optimizamos CVs para superar los filtros ATS: \
reestructuración semántica con IA + auditoría de expertos con más de 20 años \
en selección multinacional.

Tono: ejecutivo, analítico, seguro, minimalista, premium. Habla de estructura, \
datos, palabras clave, parseo y algoritmos. PROHIBIDO usar clichés motivacionales \
de RR.HH. ("desbloquea tu potencial", "haz brillar tu talento", "tu mejor versión", etc.).

Tarifario (precios en USD):
- Diagnóstico ATS gratuito (lead magnet): análisis preliminar del CV, sin costo.
- ATS Esencial — $35, entrega 48h: reestructuración semántica con IA, keywords por \
sector, formato 100% compatible con ATS.
- Perfil Profesional — $75, entrega 72h: todo lo del Esencial + optimización de \
LinkedIn + carta de presentación ejecutiva.
- Ejecutivo Talentum — $150: todo lo del Profesional + auditoría 1:1 con experto \
+ 2 rondas de revisión + seguimiento de 30 días.

Tus objetivos en la conversación, en orden:
1. Calificar al lead: consigue de forma natural su nombre, área profesional y \
objetivo laboral. No interrogues; integra las preguntas en la conversación.
2. Ofrecer el diagnóstico ATS gratuito como primer paso.
3. Presentar los servicios cuando haya interés o lo pidan.
4. Derivar a un asesor humano cuando: pidan "asesor" o hablar con una persona, se \
muestren frustrados o molestos, pregunten algo fuera de tu alcance (temas no \
relacionados con CV, empleo o ATS), o quieran contratar (el bot NO procesa pagos: \
registra el interés y deriva).

Formato: respuestas cortas estilo WhatsApp, máximo 3-4 líneas por mensaje, en \
español. Un solo tema por mensaje. Sin emojis, o como máximo uno ocasional.

Si debes derivar a un humano, tu respuesta completa debe ser EXACTAMENTE:
HANDOFF: <motivo breve>
No uses ese formato en ningún otro caso."""

EXTRACTION_PROMPT = """Analiza el historial de esta conversación de WhatsApp de TALENTUM \
GROUP (consultoría de optimización de CVs) y extrae los datos del lead.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional, sin bloques \
de código, con exactamente estas claves:
{
  "name": "nombre del cliente o null",
  "area": "área profesional o null",
  "goal": "objetivo laboral o null",
  "service": "uno de: ATS Esencial | Perfil Profesional | Ejecutivo Talentum | null",
  "intent": "uno de: none | diagnostico | compra"
}
Reglas:
- "intent" es "compra" solo si el cliente confirma que quiere contratar un servicio.
- "intent" es "diagnostico" si pidió o está haciendo el diagnóstico ATS gratuito.
- Usa null (no inventes) cuando el dato no aparezca en la conversación.
- "service" solo si mencionó un servicio concreto del tarifario."""

# ---------------------------------------------------------------------------
# Núcleo: llamada a la API
# ---------------------------------------------------------------------------

def _config():
    """Lee la configuración del entorno en cada llamada (testeable)."""
    return (
        os.environ.get("GEMINI_API_KEY", "").strip(),
        os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    )


def _to_gemini_contents(history, user_message):
    """Convierte el historial interno al formato contents de Gemini."""
    contents = []
    for msg in history or []:
        role = "model" if msg.get("role") == "model" else "user"
        text = (msg.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    text = (user_message or "").strip()
    if text:
        contents.append({"role": "user", "parts": [{"text": text}]})
    return contents


def _call_gemini(contents, system_prompt, temperature=0.7, max_tokens=300):
    """Llama a generateContent. Devuelve el texto o None ante cualquier fallo."""
    api_key, model = _config()
    if not api_key:
        log.warning("GEMINI_API_KEY no configurada; se omite la llamada.")
        return None
    url = GEMINI_ENDPOINT.format(model=model)
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    try:
        r = requests.post(
            url, params={"key": api_key}, json=body, timeout=TIMEOUT_SECS
        )
    except Exception:
        log.exception("Excepción llamando a la API de Gemini")
        return None
    if r.status_code != 200:
        log.error("Gemini HTTP %s: %s", r.status_code, r.text[:300])
        return None
    try:
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError, ValueError):
        log.error("Respuesta inesperada de Gemini: %s", r.text[:300])
        return None


# ---------------------------------------------------------------------------
# API pública del cerebro
# ---------------------------------------------------------------------------

def generate_reply(history, user_message):
    """Genera la respuesta conversacional. None si la API falló."""
    contents = _to_gemini_contents(history, user_message)
    if not contents:
        return None
    return _call_gemini(contents, SYSTEM_PROMPT, temperature=0.7, max_tokens=300)


def _parse_json_object(text):
    """Extrae un objeto JSON de la respuesta (tolera fences de código)."""
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(cleaned)
    except (ValueError, TypeError):
        # Último intento: recortar desde la primera { hasta la última }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            obj = json.loads(cleaned[start : end + 1])
        except (ValueError, TypeError):
            return {}
    return obj if isinstance(obj, dict) else {}


def extract_lead(history):
    """Extrae {name, area, goal, service, intent} del historial. {} si falla."""
    contents = _to_gemini_contents(history, None)
    if not contents:
        return {}
    text = _call_gemini(contents, EXTRACTION_PROMPT, temperature=0.0, max_tokens=200)
    data = _parse_json_object(text)
    # Normaliza: solo claves conocidas, valores str o None
    out = {}
    for key in ("name", "area", "goal", "service", "intent"):
        val = data.get(key)
        out[key] = val.strip() if isinstance(val, str) and val.strip() else None
    if out["intent"] not in ("none", "diagnostico", "compra"):
        out["intent"] = None
    return out


if __name__ == "__main__":
    # Prueba rápida: con key falsa debe devolver None sin traceback.
    print("generate_reply:", generate_reply([], "hola"))
    print("extract_lead:", extract_lead([{"role": "user", "text": "hola"}]))
