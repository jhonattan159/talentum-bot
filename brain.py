# brain.py
"""Cerebro LLM del chatbot de WhatsApp de TALENTUM GROUP.

Usa la API de Google Gemini vía REST directo (módulo `requests`, sin SDK
pesado). Lee las API keys del entorno en cada llamada (para que los tests
puedan inyectarlas sin reimportar).

Rotación de keys: la variable GEMINI_API_KEYS contiene las claves separadas
por comas (se acepta también la singular GEMINI_API_KEY como respaldo).
El pool reparte las llamadas en round-robin y pone en pausa la key que falle:
  - 429 / 5xx / excepción de red  -> pausa de 60 s (cupo agotado o fallo temporal)
  - 400 / 401 / 403              -> pausa de 1 h (clave inválida o sin permiso)
Nunca se registra el valor de las claves, solo su índice.

Contrato:
    generate_reply(history, user_message) -> str | None
        history: lista de {"role": "user"|"model", "text": str}
        Devuelve el texto de respuesta, o None si la API falló / no hay key.
        NUNCA lanza excepciones: cualquier error se registra y devuelve None.

    extract_lead(history) -> dict
        Extrae datos estructurados del lead desde el historial.
        Devuelve dict (posiblemente vacío); nunca lanza excepciones.

    _call_gemini(contents, system_prompt, temperature, max_tokens,
                 operation="chat") -> (str|None, dict|None)
        Uso interno (también lo usa ats_semantic). Devuelve (texto, uso)
        donde `uso` es {"key_index", "model", "prompt_tokens",
        "candidates_tokens", "total_tokens"} o None si la API no lo reportó.
        Cada llamada exitosa registra el consumo en la tabla `gemini_usage`
        y lo deja en el log a nivel INFO (solo índice de key, nunca su valor).

Prueba local:
    GEMINI_API_KEY=falsa python3 -c "import brain; print(brain.generate_reply([], 'hola'))"
    # Debe imprimir None y loguear el error, sin traceback.
"""

import json
import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger("talentum.brain")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-3.6-flash"
TIMEOUT_SECS = 60

# Pausas del pool de rotación (segundos)
COOLDOWN_QUOTA = 60      # 429 / 5xx / error de red: cupo agotado o fallo temporal
COOLDOWN_INVALID = 3600  # 401 / 403: clave inválida o sin permiso

# ---------------------------------------------------------------------------
# Prompt del sistema (identidad, tono, tarifario, objetivos)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Eres el asistente de WhatsApp de TALENTUM GROUP, empresa construida con \
ingeniería en perfiles, con años de experiencia en recursos humanos de \
multinacionales. Optimizamos CVs para superar los filtros ATS: \
reestructuración semántica con IA + auditoría de expertos.

Tono: ejecutivo, analítico, seguro, minimalista, premium. Habla de estructura, \
datos, palabras clave, parseo y algoritmos. PROHIBIDO usar clichés motivacionales \
de RR.HH. ("desbloquea tu potencial", "haz brillar tu talento", "tu mejor versión", etc.).

Nuestro servicio incluye: un CV que supera los sistemas ATS, 6 fotos \
profesionales y un análisis de tu perfil.

Tarifario (precios en USD):
- Diagnóstico ATS gratuito (lead magnet): análisis preliminar del CV, sin costo.
- ATS Esencial — $20: reestructuración semántica con IA, keywords por sector, \
formato 100% compatible con ATS.
- Perfil Profesional — $50: todo lo del Esencial + optimización de LinkedIn + \
carta de presentación ejecutiva.
- Ejecutivo Talentum — $100: todo lo del Profesional + auditoría 1:1 con experto \
+ 2 rondas de revisión + seguimiento de 30 días.
- PROMO ANIVERSARIO (tiempo muy limitado): el plan Esencial al 90% de descuento: \
queda en $2, con carta de presentación incluida y entrega en 12 horas.

Tus objetivos en la conversación, en orden:
1. Bienvenida (primer contacto, justo después del video): saluda con labia y \
claridad, como una persona real — nada de prosa acartonada de IA: evita el \
abuso de rayas (—) y las frases simétricas de folleto. Guion: "Hola, bienvenido \
a *TALENTUM GROUP*. No solo escribimos CVs: los *ingeniamos*. Analizamos cómo \
los sistemas ATS leen tu hoja de vida y la reconstruimos con datos y palabras \
clave precisas, con el respaldo de años de experiencia en recursos humanos de \
multinacionales." Luego pregunta: ¿Cuál es tu nombre?
2. Cuando te dé su nombre, salúdalo por su nombre y pregúntale qué quiere, SIN \
venderle todavía — nada de qué incluye el servicio ni precios aún: "¿Te \
gustaría recibir un *diagnóstico gratuito* de tu CV para ver cómo está frente \
a los filtros ATS? ¿O prefieres que *elaboremos tu CV* directamente?"
3. Si elige que le elaboremos el CV, sigue este guion con labia:
   a. Valor: "¡Buena decisión, {nombre}! Incluye: CV reconstruido con ingeniería \
de perfiles que supera los filtros ATS, 6 fotos profesionales y análisis \
completo de tu perfil."
   b. Califica: "Para dejarlo a tu medida: ¿para qué área o puesto lo necesitas?"
   c. Planes: "Te cuento las opciones: *Esencial $20* · *Profesional $50* · \
*Ejecutivo $100*."
   d. Promo (tono REAL y tranquilo, cero griterío de infomercial): "Un dato: por \
nuestro aniversario, el *Esencial* está en *$2* en vez de $20. Incluye carta de \
presentación y entrega en 12 horas. La promo es solo por estos días — ¿te \
anotas?" Nada de "¡no te lo pierdas!", nada de repetir el precio con asombro: \
la oferta se dice una vez, con calma.
   e. Si elige un plan: deriva al asesor para coordinar pago y entrega (el bot \
NO procesa pagos: registra el interés y deriva).
4. Calificar al lead: consigue de forma natural su área profesional y objetivo \
laboral. No interrogues; integra las preguntas en la conversación.
5. Presentar los servicios o precios solo cuando haya interés o lo pidan.
6. Derivar a un asesor humano cuando: pidan "asesor" o hablar con una persona, se \
muestren frustrados o molestos, pregunten algo fuera de tu alcance (temas no \
relacionados con CV, empleo o ATS), o quieran contratar (el bot NO procesa pagos: \
registra el interés y deriva).

Formato: respuestas cortas estilo WhatsApp, máximo 3-4 líneas por mensaje, en \
español. Un solo tema por mensaje. Usa *negritas* de WhatsApp para lo importante \
(nombres del servicio, beneficios clave). Sin emojis, o como máximo uno ocasional.

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
# Pool de rotación de API keys
# ---------------------------------------------------------------------------

def _load_keys():
    """Lee las claves del entorno. Acepta lista (GEMINI_API_KEYS) o singular."""
    raw = os.environ.get("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        single = os.environ.get("GEMINI_API_KEY", "").strip()
        if single:
            keys = [single]
    # Deduplica preservando orden
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


class _KeyPool:
    """Round-robin de API keys con backoff por fallos. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._keys = []
        self._cooldown_until = {}  # idx -> timestamp unix
        self._pos = 0

    def _refresh(self):
        keys = _load_keys()
        with self._lock:
            if keys != self._keys:
                self._keys = keys
                self._cooldown_until = {}
                self._pos = 0

    def acquire(self):
        """Devuelve (idx, key) disponible, o (None, None) si no hay."""
        self._refresh()
        with self._lock:
            n = len(self._keys)
            if n == 0:
                return None, None
            now = time.time()
            for _ in range(n):
                idx = self._pos % n
                self._pos += 1
                if self._cooldown_until.get(idx, 0) <= now:
                    return idx, self._keys[idx]
            return None, None

    def report(self, idx, ok, status=None):
        """Registra el resultado del uso de una key (nunca su valor)."""
        with self._lock:
            if ok:
                self._cooldown_until.pop(idx, None)
                return
            now = time.time()
            if status in (401, 403):
                self._cooldown_until[idx] = now + COOLDOWN_INVALID
                log.warning(
                    "Gemini key #%d rechazada (HTTP %s); en pausa 1h.", idx, status
                )
            else:
                self._cooldown_until[idx] = now + COOLDOWN_QUOTA
                log.warning(
                    "Gemini key #%d sin cupo o con fallo (HTTP %s); en pausa 60s.",
                    idx, status,
                )


_POOL = _KeyPool()


def _config():
    """Lee el modelo del entorno en cada llamada (testeable)."""
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Núcleo: llamada a la API con rotación
# ---------------------------------------------------------------------------

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
    # La API rechaza requests que terminan con turno del modelo (HTTP 400):
    # se recortan los turnos finales del modelo para no envenenar las keys.
    while contents and contents[-1]["role"] == "model":
        contents.pop()
    return contents


def _extract_text(response):
    """Extrae el texto de una respuesta 200 de generateContent."""
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _extract_usage(response):
    """Extrae usageMetadata de la respuesta. Dict o None si ausente.

    Nunca lanza: ante cualquier forma inesperada devuelve None.
    """
    try:
        data = response.json()
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    um = data.get("usageMetadata")
    if not isinstance(um, dict):
        return None

    def _i(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 0
        return n if n >= 0 else 0

    return {
        "prompt_tokens": _i(um.get("promptTokenCount")),
        "candidates_tokens": _i(um.get("candidatesTokenCount")),
        "total_tokens": _i(um.get("totalTokenCount")),
    }


# DDL portable de la tabla de consumo (la crea el registrador si no existe;
# también está en db._ddl() y engine._init_db() para documentación).
_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS gemini_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_index INTEGER NOT NULL,
    model TEXT NOT NULL,
    operation TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    candidates_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
);
"""


def _record_usage(operation, key_index, model, usage):
    """Registra el consumo de una llamada: log INFO + tabla gemini_usage.

    Nunca lanza excepciones: un fallo de registro no afecta la respuesta.
    Solo se registra el índice de la key, nunca su valor.
    """
    try:
        u = usage if isinstance(usage, dict) else {}
        p = u.get("prompt_tokens", 0)
        c = u.get("candidates_tokens", 0)
        t = u.get("total_tokens", 0)
        log.info(
            "Gemini uso: op=%s key=#%d model=%s prompt=%d candidatos=%d total=%d",
            operation, key_index, model, p, c, t,
        )
        import db  # importación diferida: brain no depende de db al importarse

        conn = db.connect()
        try:
            conn.execute(_USAGE_DDL)
            conn.execute(
                "INSERT INTO gemini_usage "
                "(ts, key_index, model, operation, prompt_tokens, "
                "candidates_tokens, total_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), key_index, model, operation, p, c, t),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("No se pudo registrar el uso de tokens (no afecta la respuesta)")


def _call_gemini(contents, system_prompt, temperature=0.7, max_tokens=300,
                 operation="chat"):
    """Llama a generateContent rotando keys. Devuelve (texto, uso).

    Prueba cada key disponible; ante 429/5xx la pone en pausa y sigue con la
    siguiente. Si ninguna responde, devuelve (None, None) (el motor usa el
    fallback). `uso` es None cuando la API no reportó usageMetadata.
    """
    model = _config()
    url = GEMINI_ENDPOINT.format(model=model)
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    while True:
        idx, api_key = _POOL.acquire()
        if api_key is None:
            log.error("Sin API keys de Gemini disponibles; se omite la llamada.")
            return None, None
        try:
            r = requests.post(
                url, params={"key": api_key}, json=body, timeout=TIMEOUT_SECS
            )
        except Exception:
            log.exception("Excepción llamando a la API de Gemini")
            _POOL.report(idx, False, status="exception")
            continue
        if r.status_code == 200:
            _POOL.report(idx, True)
            try:
                text = _extract_text(r)
            except (KeyError, IndexError, TypeError, ValueError):
                log.error("Respuesta inesperada de Gemini: %s", r.text[:300])
                return None, None
            usage = _extract_usage(r)
            if usage is not None:
                usage["key_index"] = idx
                usage["model"] = model
            _record_usage(operation, idx, model, usage)
            return text, usage
        if r.status_code == 400:
            # Request malformado (error nuestro, no de la key): reintentar con
            # otras keys es inútil y castigarlas 1h envenena el pool.
            log.error(
                "Gemini HTTP 400 (request inválido, no se reintenta): %s",
                r.text[:300],
            )
            return None, None
        # Fallo: se registra, la key entra en pausa y se prueba la siguiente
        log.error("Gemini HTTP %s: %s", r.status_code, r.text[:300])
        _POOL.report(idx, False, status=r.status_code)


# ---------------------------------------------------------------------------
# API pública del cerebro
# ---------------------------------------------------------------------------

def generate_reply(history, user_message):
    """Genera la respuesta conversacional. None si la API falló."""
    contents = _to_gemini_contents(history, user_message)
    if not contents:
        return None
    text, _usage = _call_gemini(
        contents, SYSTEM_PROMPT, temperature=0.7, max_tokens=1000,
        operation="chat",
    )
    return text


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
    text, _usage = _call_gemini(
        contents, EXTRACTION_PROMPT, temperature=0.0, max_tokens=200,
        operation="extract_lead",
    )
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
