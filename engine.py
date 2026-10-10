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
import os
import re
import sqlite3
import time

import brain

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
# Punto de entrada principal
# ---------------------------------------------------------------------------

def process_message(phone, text):
    """Procesa un mensaje entrante y devuelve la lista de respuestas a enviar."""
    text = (text or "").strip()
    if not text or not phone:
        return []
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

    # Ya derivado: el bot queda en silencio para no interrumpir al humano
    if state == ASESOR:
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
