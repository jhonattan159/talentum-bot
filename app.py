# app.py
"""Webhook FastAPI del chatbot de WhatsApp de TALENTUM GROUP.

Endpoints:
    GET  /health    -> estado del servicio
    GET  /webhook   -> verificación del webhook (Meta envía hub.challenge)
    POST /webhook   -> recibe eventos de la Cloud API de WhatsApp

Las credenciales se leen del entorno (ver .env.example). Nunca van
hardcodeadas en el código.
"""

import logging
import os

import requests
from fastapi import FastAPI, Request, Response

from engine import process_message, process_document, drain_outbox

# --- Configuración desde variables de entorno (sin secretos en el código) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("talentum")

app = FastAPI(title="TALENTUM GROUP — Chatbot WhatsApp")


# --- Panel de administración (supervisión de conversaciones y leads) ---
try:
    from admin import router as admin_router

    app.include_router(admin_router)
except Exception:
    log.exception("No se pudo montar el panel /admin")


@app.get("/health")
def health():
    """Chequeo de vida para Render y monitoreo."""
    return {"status": "ok", "service": "talentum-chatbot"}


@app.get("/webhook")
def verify_webhook(request: Request):
    """Verificación que hace Meta al registrar el webhook.

    Meta envía: hub.mode=subscribe, hub.verify_token y hub.challenge.
    Si el token coincide con VERIFY_TOKEN, se devuelve el challenge.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta.")
        return Response(content=challenge or "", media_type="text/plain")
    log.warning("Intento de verificación con token inválido.")
    return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Recibe eventos de la Cloud API: los pasa por el motor y responde.

    Siempre responde 200 para que Meta no reintente el envío.
    """
    try:
        payload = await request.json()
    except Exception:
        log.warning("Payload no JSON; se ignora.")
        return {"status": "ignored"}

    for phone, text in extract_messages(payload):
        try:
            replies = process_message(phone, text)
        except Exception:
            # El motor nunca debe tumbar el webhook: respuesta de contingencia
            log.exception("Error en el motor de conversación")
            replies = [
                "Tuvimos un inconveniente técnico. Un asesor te contactará "
                "por este número a la brevedad."
            ]
        for reply in replies:
            send_whatsapp_message(phone, reply)
        # Mensajes del dueño hacia clientes (comando "responder"): salen del
        # número de TALENTUM. Si falla el envío se le avisa al dueño.
        for to_phone, out_text in drain_outbox():
            if send_whatsapp_message(to_phone, out_text):
                log.info("Mensaje del dueño enviado a +%s", to_phone)
            else:
                log.error("No se pudo enviar el mensaje del dueño a +%s", to_phone)
                send_whatsapp_message(
                    phone,
                    f"⚠ No se pudo enviar el mensaje a +{to_phone} "
                    "(quizá pasaron más de 24h sin que te escriba).",
                )

    for doc in extract_documents(payload):
        try:
            if not _is_pdf(doc):
                replies = [
                    "Solo puedo analizar archivos PDF. Envíame tu CV en PDF "
                    "y te hago el diagnóstico ATS gratuito."
                ]
            else:
                pdf_bytes = download_whatsapp_media(doc["media_id"])
                if pdf_bytes:
                    replies = process_document(
                        doc["phone"], pdf_bytes, doc.get("filename") or "cv.pdf"
                    )
                else:
                    replies = [
                        "No pude descargar el documento. Intenta enviarlo de nuevo."
                    ]
        except Exception:
            log.exception("Error procesando documento de WhatsApp")
            replies = [
                "No pude procesar el documento. Intenta de nuevo o escribe ASESOR."
            ]
        for reply in replies:
            send_whatsapp_message(doc["phone"], reply)

    return {"status": "ok"}


def _is_pdf(doc):
    """Acepta solo PDFs (por MIME o por extensión del nombre)."""
    mime = (doc.get("mime_type") or "").lower()
    name = (doc.get("filename") or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf")


def download_whatsapp_media(media_id):
    """Descarga un archivo de la Cloud API. Devuelve bytes o None.

    Flujo: GET /{media-id} -> URL temporal -> GET URL con el Bearer token.
    Nunca lanza excepciones.
    """
    if not WHATSAPP_TOKEN or not media_id:
        log.error("Faltan WHATSAPP_TOKEN o media_id para descargar el archivo.")
        return None
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        meta = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}",
            headers=headers,
            timeout=15,
        )
        if meta.status_code != 200:
            log.error("Error obteniendo URL de media (%s).", meta.status_code)
            return None
        url = (meta.json() or {}).get("url")
        if not url:
            log.error("La API no devolvió URL para el media %s.", media_id)
            return None
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            log.error("Error descargando media (%s).", r.status_code)
            return None
        data = r.content or b""
        if len(data) > 6 * 1024 * 1024:
            log.warning("Media demasiado pesado (%d bytes); se rechaza.", len(data))
            return None
        return data
    except Exception:
        log.exception("Excepción descargando media de WhatsApp")
        return None


def extract_messages(payload):
    """Extrae pares (teléfono, texto) de los mensajes entrantes.

    Acepta mensajes de texto y respuestas de botones interactivos.
    Ignora estados de entrega/lectura y cualquier otro evento.
    """
    out = []
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    phone = msg.get("from")
                    text = ""
                    if msg.get("type") == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    elif msg.get("type") == "interactive":
                        inter = msg.get("interactive", {})
                        br = inter.get("button_reply") or inter.get("list_reply") or {}
                        text = br.get("title") or br.get("id") or ""
                    if phone and text:
                        out.append((phone, text))
    except Exception:
        log.exception("No se pudo parsear el payload del webhook")
    return out


def extract_documents(payload):
    """Extrae documentos entrantes del payload de la Cloud API.

    Devuelve lista de dicts: {phone, media_id, mime_type, filename, caption}.
    Ignora cualquier otro tipo de mensaje.
    """
    out = []
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") != "document":
                        continue
                    doc = msg.get("document") or {}
                    phone = msg.get("from")
                    media_id = doc.get("id")
                    if phone and media_id:
                        out.append(
                            {
                                "phone": phone,
                                "media_id": media_id,
                                "mime_type": doc.get("mime_type") or "",
                                "filename": doc.get("filename") or "documento.pdf",
                                "caption": (doc.get("caption") or "").strip(),
                            }
                        )
    except Exception:
        log.exception("No se pudo parsear documentos del payload")
    return out


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
