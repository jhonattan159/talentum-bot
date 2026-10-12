# engine.py
"""Motor de conversación del chatbot de WhatsApp de TALENTUM GROUP.

El enrutamiento es conversacional: cada mensaje pasa por el cerebro LLM
(`brain.py`, Google Gemini) con el historial de la conversación. La máquina
de estados rígida se eliminó; se conserva:
  - persistencia de sesión e historial en SQLite (conversations.db),
  - registro de leads en SQLite (tabla leads),
  - derivación determinista a asesor humano ("asesor", frustración),
  - comandos globales: "reiniciar", "menú".

Si la API de Gemini falla, el bot responde con un fallback amable (nunca
se queda en silencio ni tumba el webhook).

Uso como módulo:
    from engine import process_message
    respuestas = process_message("+593997283424", "hola")  # -> list[str]

Prueba local (simulador de consola):
    DB_PATH=/tmp/test.db python3 engine.py
"""

import json
import logging
import os
import re
import sqlite3
import time

import requests

import brain

log = logging.getLogger("talentum.engine")

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.db"),
)

# --- Estados (simplificados; el flujo lo lleva el LLM) ---
INICIO = "inicio"
CHAT = "chat"
ASESOR = "asesor"  # derivado a humano: el bot queda en silencio

MAX_HISTORY = 20  # mensajes (10 intercambios) que se envían al LLM

# --- Envío de WhatsApp (Graph API). Vive aquí para que app.py y admin.py
# lo compartan sin importes circulares. ---
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")


def send_whatsapp_message(to, text):
    """Envía un mensaje de texto vía Graph API.

    Maneja errores sin romper el flujo: registra el fallo y devuelve False.
    WhatsApp limita el texto a ~4096 caracteres; se trunca a 4000 por seguridad.
    """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error("Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID en el entorno.")
        return False
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4000]},
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    except Exception:
        log.exception("Excepción al enviar mensaje de WhatsApp")
        return False
    if r.status_code >= 400:
        log.error("Error al enviar mensaje (%s): %s", r.status_code, r.text[:500])
        return False
    return True


# --- Video de bienvenida (primer contacto) ---
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://talentum-chatbot.onrender.com"
).rstrip("/")
WELCOME_VIDEO_URL = f"{PUBLIC_BASE_URL}/static/bienvenida-talentum.mp4"


def send_whatsapp_video(to, video_url, caption=None):
    """Envía un video vía Graph API usando un link público.

    Requiere MP4 (H.264 + AAC) de menos de 16 MB. Maneja errores sin
    romper el flujo: registra el fallo y devuelve False.
    """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error("Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID en el entorno.")
        return False
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    video = {"link": video_url}
    if caption:
        video["caption"] = caption[:1024]
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "video",
        "video": video,
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=30)
    except Exception:
        log.exception("Excepción al enviar video de WhatsApp")
        return False
    if r.status_code >= 400:
        log.error("Error al enviar video (%s): %s", r.status_code, r.text[:500])
        return False
    return True

# ---------------------------------------------------------------------------
# Catálogo de servicios (fuente única del tarifario)
# ---------------------------------------------------------------------------

SERVICES = {
    "esencial": {
        "nombre": "ATS Esencial",
        "precio": 35,
        "detalle": (
            "Reestructuración semántica del CV con IA, palabras clave por sector "
            "y formato 100% compatible con ATS. Entrega en 48 horas."
        ),
    },
    "profesional": {
        "nombre": "Perfil Profesional",
        "precio": 75,
        "detalle": (
            "Todo lo del Esencial + optimización del perfil de LinkedIn + carta "
            "de presentación ejecutiva. Entrega en 72 horas."
        ),
    },
    "ejecutivo": {
        "nombre": "Ejecutivo Talentum",
        "precio": 150,
        "detalle": (
            "Todo lo del Profesional + auditoría 1:1 con experto (más de 20 años "
            "en selección multinacional) + 2 rondas de revisión + seguimiento de 30 días."
        ),
    },
}

# ---------------------------------------------------------------------------
# Mensajes base
# ---------------------------------------------------------------------------

WELCOME_MSG = (
    "Hola, bienvenido a TALENTUM GROUP. Soy el asistente de ingeniería de "
    "perfiles: optimizamos tu CV para superar los filtros ATS con IA + "
    "auditoría experta.\n\n"
    "¿En qué te ayudo hoy? Puedo hacerte un diagnóstico ATS gratuito o "
    "contarte de nuestros servicios."
)

MENU_MSG = (
    "Puedo ayudarte con:\n"
    "1. Diagnóstico ATS gratuito\n"
    "2. Ver servicios (Esencial $35 · Profesional $75 · Ejecutivo $150)\n"
    "3. Hablar con un asesor\n\n"
    "¿Qué necesitas?"
)

# Fallback amable cuando la API de Gemini no responde: el bot sigue operando.
FALLBACK_MSG = (
    "Gracias por escribir a TALENTUM GROUP. Estoy pasando por un inconveniente "
    "técnico momentáneo, pero puedo orientarte:\n\n" + MENU_MSG
)

# ---------------------------------------------------------------------------
# Detección determinista (no depende del LLM)
# ---------------------------------------------------------------------------

RE_ASESOR = re.compile(r"\basesor\b|\bhumano\b|persona real|\bagente\b", re.I)
RE_FRUSTRACION = re.compile(
    r"\bestafa\w*|\bladron\w*|\bin[úu]til\w*|\bp[ée]simo\w*|\bhorrible\w*|"
    r"\bdemanda\w*|\babogado\w*|\breembolso\w*|\bqueja\w*|\breclamo\w*",
    re.I,
)
RE_HANDOFF_MARKER = re.compile(r"^\s*HANDOFF\s*:\s*(.+)$", re.I | re.S)

# ---------------------------------------------------------------------------
# Persistencia (SQLite)
# ---------------------------------------------------------------------------

def _connect():
    """Abre una conexión nueva en cada operación (seguro con hilos)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            fails INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            name TEXT,
            area TEXT,
            goal TEXT,
            service TEXT,
            intent TEXT,
            note TEXT,
            created_at REAL NOT NULL
        );
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
    )
    conn.commit()
    conn.close()


_init_db()


def get_conversation(phone):
    """Recupera la conversación; si no existe, parte en INICIO con data vacía."""
    conn = _connect()
    row = conn.execute(
        "SELECT state, data, fails FROM conversations WHERE phone = ?", (phone,)
    ).fetchone()
    conn.close()
    if row is None:
        return {"state": INICIO, "data": {}, "fails": 0}
    try:
        data = json.loads(row["data"])
    except (ValueError, TypeError):
        data = {}
    state = row["state"]
    # Compatibilidad: estados de la máquina anterior caen al modo conversacional
    if state not in (INICIO, CHAT, ASESOR):
        state = CHAT
    return {"state": state, "data": data, "fails": row["fails"]}


def save_conversation(phone, state, data, fails=0):
    conn = _connect()
    conn.execute(
        """INSERT INTO conversations (phone, state, data, fails, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(phone) DO UPDATE SET
             state=excluded.state, data=excluded.data,
             fails=excluded.fails, updated_at=excluded.updated_at""",
        (phone, state, json.dumps(data or {}), fails, time.time()),
    )
    conn.commit()
    conn.close()


def register_lead(phone, data, intent="", note=""):
    """Guarda el lead para contacto manual posterior."""
    conn = _connect()
    conn.execute(
        """INSERT INTO leads (phone, name, area, goal, service, intent, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            phone,
            (data or {}).get("name"),
            (data or {}).get("area"),
            (data or {}).get("goal"),
            (data or {}).get("service"),
            intent,
            note,
            time.time(),
        ),
    )
    conn.commit()
    conn.close()


def build_summary(data):
    """Arma un resumen legible del lead para el asesor humano."""
    data = data or {}
    lineas = []
    if data.get("name"):
        lineas.append(f"Nombre: {data['name']}")
    if data.get("area"):
        lineas.append(f"Área: {data['area']}")
    if data.get("goal"):
        lineas.append(f"Objetivo: {data['goal']}")
    if data.get("service"):
        lineas.append(f"Servicio de interés: {data['service']}")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

def _get_history(data):
    hist = (data or {}).get("history")
    return hist if isinstance(hist, list) else []


def _push_history(data, role, text):
    hist = _get_history(data)
    hist.append({"role": role, "text": text})
    data["history"] = hist[-MAX_HISTORY:]
    return data


# ---------------------------------------------------------------------------
# Derivación a humano
# ---------------------------------------------------------------------------

def handoff(phone, data, motivo="", intent="handoff_asesor"):
    """Deriva a humano: registra el lead y deja al bot en silencio."""
    register_lead(phone, data, intent=intent, note=motivo)
    try:
        from alerts import notify_handoff

        notify_handoff(phone, motivo or intent, build_summary(data))
    except Exception:
        log.exception("Fallo la alerta de handoff (no afecta al webhook)")
    save_conversation(phone, ASESOR, data, 0)
    msg = (
        "Te derivo con un asesor de TALENTUM GROUP. Te contactará por este mismo "
        "número a la brevedad."
    )
    resumen = build_summary(data)
    if resumen:
        msg += f"\n\nResumen registrado:\n{resumen}"
    msg += "\n\nSi quieres, adelanta en qué horario prefieres que te escribamos."
    return [msg]


def handoff_compra(phone, data):
    """El bot no procesa pagos: registra la intención y deriva al asesor."""
    servicio = (data or {}).get("service") or "no especificado"
    register_lead(
        phone, data, intent="intencion_compra", note=f"Servicio: {servicio}"
    )
    try:
        from alerts import notify_handoff

        notify_handoff(phone, f"intención de compra ({servicio})", build_summary(data))
    except Exception:
        log.exception("Fallo la alerta de handoff (no afecta al webhook)")
    save_conversation(phone, ASESOR, data, 0)
    resumen = build_summary(data)
    msg = (
        "Perfecto, registro tu interés. Un asesor de TALENTUM GROUP te contactará "
        "por este número para coordinar el pago y la entrega del servicio."
    )
    if resumen:
        msg += f"\n\nResumen:\n{resumen}"
    return [msg]


# ---------------------------------------------------------------------------
# Extracción de datos del lead (vía LLM, tolerante a fallos)
# ---------------------------------------------------------------------------

def _merge_lead_data(data, extracted):
    """Fusiona los datos extraídos sin sobrescribir lo ya conocido."""
    for key in ("name", "area", "goal", "service"):
        val = (extracted or {}).get(key)
        if val and not data.get(key):
            data[key] = val
    return data


def _maybe_register_diagnostico(phone, data, extracted):
    """Registra el lead una sola vez cuando el diagnóstico avanza con datos."""
    if data.get("lead_registered"):
        return
    if (
        (extracted or {}).get("intent") == "diagnostico"
        and data.get("name")
        and data.get("area")
    ):
        register_lead(phone, data, intent="diagnostico_completado")
        data["lead_registered"] = True


# ---------------------------------------------------------------------------
# Comandos del dueño vía WhatsApp (supervisión sin navegador)
# ---------------------------------------------------------------------------
# Solo responden al número del dueño (OWNER_PHONE). Cualquier otro número
# que escriba estas palabras recibe el flujo NORMAL del bot: los comandos
# se ignoran en silencio y nunca se revela que existen.

OWNER_PHONE_DEFAULT = "593984705397"


def _owner_phone():
    """Número del dueño normalizado (solo dígitos)."""
    raw = os.environ.get("OWNER_PHONE", OWNER_PHONE_DEFAULT)
    return re.sub(r"\D", "", str(raw))


def _is_owner(phone):
    """True solo si el remitente es el número del dueño."""
    p = re.sub(r"\D", "", str(phone or ""))
    o = _owner_phone()
    return bool(p and o) and p == o


# Buzón de salida: mensajes del dueño hacia clientes.
# engine no importa app (sería circular); app.py lo vacía con drain_outbox()
# después de procesar cada mensaje y los envía con send_whatsapp_message.
_OUTBOX = []


def drain_outbox():
    """Devuelve y vacía [(teléfono destino, texto)] encolados por el dueño."""
    global _OUTBOX
    out = _OUTBOX
    _OUTBOX = []
    return out


def _rel_time(ts):
    """Tiempo relativo en español: 'hace 5 min', 'ayer', etc."""
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "—"
    if delta < 0:
        return "ahora mismo"
    if delta < 60:
        return "ahora mismo"
    if delta < 3600:
        m = int(delta // 60)
        return "hace 1 min" if m == 1 else "hace {} min".format(m)
    if delta < 86400:
        h = int(delta // 3600)
        return "hace 1 h" if h == 1 else "hace {} h".format(h)
    d = int(delta // 86400)
    if d == 1:
        return "ayer"
    return "hace {} días".format(d)


def _last_snippet(data, max_chars=50):
    """Último mensaje con texto del historial, recortado."""
    try:
        hist = (data or {}).get("history") or []
    except (AttributeError, TypeError):
        return ""
    for m in reversed(hist):
        t = str((m or {}).get("text") or "").strip().replace("\n", " ")
        if t:
            return t[:max_chars] + ("…" if len(t) > max_chars else "")
    return ""


def _owner_chats():
    """Lista las últimas 10 conversaciones."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT phone, data, updated_at FROM conversations "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        log.exception("Fallo listando conversaciones para el dueño")
        return "No pude leer las conversaciones en este momento."
    if not rows:
        return "Sin conversaciones registradas."
    lines = ["*Últimas conversaciones:*"]
    for i, r in enumerate(rows, 1):
        try:
            data = json.loads(r["data"] or "{}")
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        snippet = _last_snippet(data)
        lines.append(
            "{}. {} — {} ({})".format(
                i, r["phone"], snippet or "sin mensajes", _rel_time(r["updated_at"])
            )
        )
    return "\n".join(lines)


def _owner_chat_cmd(arg):
    """Últimos 10 mensajes de una conversación."""
    digits = re.sub(r"\D", "", str(arg or ""))
    if not digits:
        return "Uso: chat <número>. Ejemplo: chat 593991234567"
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT phone, data FROM conversations WHERE phone = ?",
                (digits,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT phone, data FROM conversations WHERE phone LIKE ?",
                    ("%{}%".format(digits),),
                ).fetchone()
        finally:
            conn.close()
    except Exception:
        log.exception("Fallo leyendo chat para el dueño")
        return "No pude leer esa conversación en este momento."
    if row is None:
        return "No encontré conversación con ese número."
    try:
        data = json.loads(row["data"] or "{}")
    except (ValueError, TypeError):
        data = {}
    hist = (data or {}).get("history") or []
    hist = [m for m in hist if isinstance(m, dict) and str(m.get("text") or "").strip()]
    if not hist:
        return "Esa conversación no tiene mensajes."
    lines = ["*Chat {}:*".format(row["phone"])]
    for m in hist[-10:]:
        quien = "Cliente" if m.get("role") == "user" else "Bot"
        t = str(m.get("text") or "").strip().replace("\n", " ")
        if len(t) > 160:
            t = t[:157].rstrip() + "…"
        lines.append("{}: {}".format(quien, t))
    return "\n".join(lines)


def _owner_help():
    return (
        "*Comandos del dueño:*\n"
        "• chats — últimas 10 conversaciones\n"
        "• chat <número> — últimos 10 mensajes de ese chat\n"
        "• responder <número> <mensaje> — escribirle a un cliente desde el número de TALENTUM\n"
        "• ayuda — esta lista"
    )


def _owner_responder(raw_phone, message):
    """Encola un mensaje del dueño para un cliente.

    El mensaje sale del número de TALENTUM (no del personal del dueño) y
    queda registrado en el historial del chat como mensaje del asesor.
    """
    digits = re.sub(r"\D", "", raw_phone or "")
    message = (message or "").strip()
    if not digits or not message:
        return (
            "Uso: responder <número> <mensaje>\n"
            "Ej: responder 593991234567 Hola, soy Jhonattan de TALENTUM."
        )
    _OUTBOX.append((digits, message))
    try:
        conv = get_conversation(digits)
        data = conv["data"]
        _push_history(data, "model", "🧑‍💼 Asesor: " + message)
        save_conversation(digits, conv["state"], data, conv["fails"])
    except Exception:
        log.exception("No se pudo registrar el mensaje del asesor en el historial")
    return f"✓ Mensaje para +{digits} encolado. Saldrá del número de TALENTUM."


def _owner_command(text):
    """Interpreta un posible comando del dueño.

    Devuelve la respuesta (str) si `text` es un comando, o None si no lo es
    (el flujo normal del bot continúa).
    """
    raw = (text or "").strip()
    low = raw.lower()
    if low == "chats":
        return _owner_chats()
    if low == "ayuda":
        return _owner_help()
    m = re.match(r"^chat\s+(\+?[\d][\d\s]*)$", low)
    if m:
        return _owner_chat_cmd(m.group(1))
    m = re.match(r"^responder\s+(\+?[\d][\d\s]*)\s+(.+)$", raw, re.DOTALL)
    if m:
        return _owner_responder(m.group(1), m.group(2))
    return None


# ---------------------------------------------------------------------------
# Punto de entrada principal
# ---------------------------------------------------------------------------

def process_message(phone, text):
    """Procesa un mensaje entrante y devuelve la lista de respuestas a enviar."""
    text = (text or "").strip()
    if not text or not phone:
        return []
    # Comandos del dueño: solo desde su número, antes del flujo normal.
    # Otros números que escriban estas palabras reciben el flujo normal
    # (los comandos se ignoran en silencio).
    if _is_owner(phone):
        owner_reply = _owner_command(text)
        if owner_reply is not None:
            return [owner_reply]
        # El dueño escribiendo como "cliente" (probando): sin auto-aviso.
        notify_owner = False
    else:
        notify_owner = True
    # Aviso instantáneo al dueño por cada mensaje de cliente (sin abrir el panel).
    if notify_owner:
        _OUTBOX.append((_owner_phone(), f"💬 +{phone}:\n{text[:200]}"))
    conv = get_conversation(phone)
    state, data = conv["state"], conv["data"]
    low = text.lower()

    # Comando global: reiniciar la conversación
    if re.search(r"\breiniciar\b", low):
        save_conversation(phone, CHAT, {}, 0)
        return [WELCOME_MSG]

    # Comando global: mostrar opciones (también reactiva tras derivación)
    if re.search(r"\bmen[uú]\b", low):
        _push_history(data, "user", text)
        _push_history(data, "model", MENU_MSG)
        save_conversation(phone, CHAT, data, 0)
        return [MENU_MSG]

    # Ya derivado: el bot queda en silencio para no interrumpir al humano,
    # pero el mensaje SÍ se guarda en el historial (el aviso al dueño ya se
    # encoló arriba para todos los mensajes).
    if state == ASESOR:
        _push_history(data, "user", text)
        save_conversation(phone, ASESOR, data, 0)
        return []

    # Derivación explícita a humano (determinista, no depende del LLM)
    if RE_ASESOR.search(low):
        _push_history(data, "user", text)
        save_conversation(phone, CHAT, data, 0)
        return handoff(phone, data, motivo="pidió hablar con un asesor")

    # Frustración evidente: derivar sin discutir
    if RE_FRUSTRACION.search(low):
        _push_history(data, "user", text)
        save_conversation(phone, CHAT, data, 0)
        return handoff(phone, data, motivo="frustración detectada")

    # Primer contacto: video de bienvenida antes del saludo del asistente.
    # Si el video falla, el saludo de texto igual se envía (no bloquea).
    if state == INICIO:
        send_whatsapp_video(phone, WELCOME_VIDEO_URL)

    # --- Ruta principal: cerebro LLM ---
    history = _get_history(data)
    reply = brain.generate_reply(history, text)

    if reply is None:
        # La API falló: fallback amable, el bot sigue respondiendo
        _push_history(data, "user", text)
        _push_history(data, "model", FALLBACK_MSG)
        save_conversation(phone, CHAT, data, 0)
        return [FALLBACK_MSG]

    # El LLM pide derivar a humano con el marcador HANDOFF:
    m = RE_HANDOFF_MARKER.match(reply)
    if m:
        motivo = m.group(1).strip() or "derivado por el asistente"
        _push_history(data, "user", text)
        save_conversation(phone, CHAT, data, 0)
        return handoff(phone, data, motivo=motivo)

    _push_history(data, "user", text)
    _push_history(data, "model", reply)

    # Extracción de datos del lead (tolerante a fallos)
    try:
        extracted = brain.extract_lead(_get_history(data))
    except Exception:
        extracted = {}
    _merge_lead_data(data, extracted)

    # Intención de compra: el bot no cobra → deriva al asesor
    if (extracted or {}).get("intent") == "compra":
        save_conversation(phone, CHAT, data, 0)
        return handoff_compra(phone, data)

    _maybe_register_diagnostico(phone, data, extracted)
    save_conversation(phone, CHAT, data, 0)
    return [reply]


# ---------------------------------------------------------------------------
# Diagnóstico ATS (documentos PDF recibidos por WhatsApp)
# ---------------------------------------------------------------------------

MAX_PDF_BYTES = 5 * 1024 * 1024


def _accion_text(a):
    """Normaliza una acción (dict {"accion","porque"} o str) a texto."""
    if isinstance(a, dict):
        return str(a.get("accion") or "").strip()
    return str(a or "").strip()


def _diagnostic_fallback_text(report):
    """Texto plano del diagnóstico si el formateador semántico no existe."""
    try:
        idx = report.get("indice_diagnostico")
        acciones = (report.get("acciones") or [])[:3]
        lineas = [
            "Diagnóstico ATS — TALENTUM GROUP",
            f"Índice TALENTUM: {idx}/100 "
            "(rúbrica interna, no es puntaje de ningún ATS).",
        ]
        for i, a in enumerate(acciones, 1):
            t = _accion_text(a)
            if t:
                lineas.append(f"{i}. {t}")
        lineas.append("Un asesor puede aplicar esto por ti: responde ASESOR.")
        return "\n".join(lineas)
    except Exception:
        return FALLBACK_MSG


def process_document(phone, pdf_bytes, filename="cv.pdf"):
    """Procesa un PDF entrante: diagnóstico ATS híbrido y respuesta.

    Usa ats_diagnostic.diagnose con la revisión semántica de ats_semantic
    (si está disponible). Registra el lead del diagnóstico. Nunca lanza
    excepciones: ante cualquier fallo devuelve un mensaje amable.
    """
    if not phone or not pdf_bytes:
        return []
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return ["El archivo supera los 5 MB. Envíalo de nuevo en un PDF más liviano."]
    try:
        from ats_diagnostic import diagnose
    except Exception:
        return [
            "No pude analizar el documento en este momento. "
            "Escribe ASESOR y un asesor te ayuda."
        ]
    try:
        try:
            from ats_semantic import semantic_review, format_whatsapp
        except Exception:
            semantic_review, format_whatsapp = None, None
        report = diagnose(pdf_bytes, semantic_fn=semantic_review)
        if format_whatsapp:
            reply = format_whatsapp(report)
        else:
            reply = ""
        if not reply:
            reply = _diagnostic_fallback_text(report)
        # Lead: se registra el diagnóstico con su índice
        try:
            conv = get_conversation(phone)
            data = conv["data"]
            _push_history(data, "user", f"[documento PDF: {filename}]")
            _push_history(data, "model", reply)
            register_lead(
                phone,
                data,
                intent="diagnostico_completado",
                note=f"Diagnóstico ATS: índice {report.get('indice_diagnostico')}/100",
            )
            save_conversation(phone, CHAT, data, 0)
        except Exception:
            pass
        return [reply]
    except Exception:
        return [
            "No pude procesar el PDF. Verifica que sea un PDF válido "
            "o escribe ASESOR."
        ]


# ---------------------------------------------------------------------------
# Simulador de consola para pruebas locales: python engine.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Simulador local del chatbot TALENTUM GROUP (cerebro Gemini).")
    print("Sin GEMINI_API_KEY verás el fallback amable.")
    print("Escribe 'salir' para terminar, 'reiniciar' para empezar de nuevo.\n")
    telefono = "test-local"
    while True:
        try:
            entrada = input("Tú: ")
        except (EOFError, KeyboardInterrupt):
            break
        if entrada.strip().lower() in ("salir", "exit", "quit"):
            break
        for respuesta in process_message(telefono, entrada):
            print(f"Bot: {respuesta}\n")
