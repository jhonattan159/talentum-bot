# alerts.py
"""Alertas instantáneas al dueño por WhatsApp cuando hay un handoff.

`notify_handoff(phone, motivo, resumen)` envía la plantilla `alerta_handoff`
al número personal del dueño. Los mensajes iniciados por la empresa fuera de
la ventana de 24 h REQUIEREN plantilla aprobada: por eso se usa plantilla y
no texto libre.

Si la plantilla aún no existe o no está aprobada, se registra un mensaje
claro indicando que hay que crearla en WhatsApp Manager y NO se lanza
excepción: las alertas nunca deben romper el webhook.

Toda la configuración sale de variables de entorno; nada va hardcodeado.
"""

import logging
import os

import requests

log = logging.getLogger("talentum.alerts")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "593984705397")

TEMPLATE_NAME = "alerta_handoff"
TEMPLATE_LANG = "es"
TIMEOUT_SECS = 15


def _post(payload):
    """POST a la Graph API. Devuelve el response o None ante fallos."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error("Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID; alerta no enviada.")
        return None
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    try:
        return requests.post(
            url,
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT_SECS,
        )
    except Exception:
        log.exception("Excepción enviando alerta de handoff")
        return None


def _is_template_missing(resp):
    """Detecta si el error es por plantilla inexistente o no aprobada."""
    try:
        err = resp.json().get("error", {})
    except (ValueError, AttributeError):
        return False
    code = err.get("code")
    msg = str(err.get("message", "")).lower()
    template_mention = "template" in msg and (
        "exist" in msg or "missing" in msg or "not found" in msg or "not approved" in msg
    )
    return code == 132000 or template_mention


def notify_handoff(phone, motivo, resumen=""):
    """Envía la alerta de handoff al dueño vía plantilla de WhatsApp.

    Devuelve True si se envió, False en caso contrario.
    NUNCA lanza excepciones.
    """
    motivo = (motivo or "handoff").strip()[:200]
    phone = (phone or "").strip()
    payload = {
        "messaging_product": "whatsapp",
        "to": OWNER_PHONE,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": phone[:40]},
                        {"type": "text", "text": motivo},
                    ],
                }
            ],
        },
    }
    try:
        resp = _post(payload)
    except Exception:
        log.exception("Fallo inesperado en notify_handoff")
        return False
    if resp is None:
        return False
    if resp.status_code < 400:
        try:
            mid = resp.json().get("messages", [{}])[0].get("id", "")
        except (ValueError, KeyError, IndexError, TypeError):
            mid = ""
        log.info("Alerta de handoff enviada para %s (id %s).", phone, mid)
        return True
    if _is_template_missing(resp):
        log.error(
            "Plantilla '%s' inexistente o no aprobada (HTTP %s). "
            "Créala en WhatsApp Manager (categoría UTILITY, idioma es, "
            "cuerpo con 2 parámetros de texto) y se reintentará en el próximo "
            "handoff. Detalle: %s",
            TEMPLATE_NAME,
            resp.status_code,
            resp.text[:200],
        )
        return False
    log.error(
        "No se pudo enviar la alerta de handoff (HTTP %s): %s",
        resp.status_code,
        resp.text[:300],
    )
    return False
