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

from engine import process_message

# --- Configuración desde variables de entorno (sin secretos en el código) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("talentum")

PRIVACY_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de Privacidad — TALENTUM GROUP</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:720px;margin:0 auto;padding:32px 20px;color:#1a1a1a;line-height:1.6}h1{color:#0A1F44}h2{color:#0A1F44;margin-top:28px}.small{color:#666;font-size:14px}</style>
</head>
<body>
<h1>Política de Privacidad — TALENTUM GROUP</h1>
<p class="small">Última actualización: 10 de octubre de 2026</p>
<p>TALENTUM GROUP opera un asistente conversacional por WhatsApp que orienta sobre optimización de hojas de vida y servicios de consultoría de talento.</p>
<h2>Datos que recopilamos</h2>
<ul><li>Tu número de teléfono de WhatsApp.</li><li>El contenido de los mensajes que nos envías.</li><li>Registros técnicos básicos (fecha y hora) para calidad del servicio.</li></ul>
<h2>Uso de los datos</h2>
<p>Solo para responder tus consultas, dar seguimiento a solicitudes y mejorar el asistente. No vendemos ni alquilamos tus datos.</p>
<h2>Compartición</h2>
<p>Los mensajes se transmiten vía la plataforma WhatsApp Business de Meta para su entrega; no compartimos datos con terceros salvo obligación legal.</p>
<h2>Tus derechos</h2>
<p>Puedes pedir la eliminación de tu historial escribiendo «eliminar mis datos» en el chat o a talentumgroup.ec@gmail.com.</p>
<h2>Contacto</h2>
<p>TALENTUM GROUP — Quito, Ecuador<br>talentumgroup.ec@gmail.com</p>
</body>
</html>"""

app = FastAPI(title="TALENTUM GROUP — Chatbot WhatsApp")


@app.get("/health")
def health():
    """Chequeo de vida para Render y monitoreo."""
    return {"status": "ok", "service": "talentum-chatbot"}




@app.get("/privacy")
def privacy_policy():
    """Política de privacidad (requerida por Meta para publicar la app)."""
    return Response(content=PRIVACY_HTML, media_type="text/html")


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

    return {"status": "ok"}


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
