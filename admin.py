# admin.py
"""Panel de supervisión del chatbot de WhatsApp de TALENTUM GROUP.

Router FastAPI con HTTP Basic Auth:
  - usuario: `admin`
  - contraseña: variable de entorno `ADMIN_PASSWORD`
  - si `ADMIN_PASSWORD` no está configurada, TODO se deniega (401).

Rutas:
  GET /admin/            -> lista de conversaciones (recientes primero)
  GET /admin/chat/{phone} -> historial completo de una conversación
  GET /admin/leads       -> tabla de leads/handoffs, filtrable por intención

HTML simple con identidad TALENTUM (navy #0A1F44 / dorado #C9A227).
Todo el contenido dinámico se escapa (html.escape) contra XSS.
"""

import html
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import db

log = logging.getLogger("talentum.admin")

ADMIN_USER = "admin"
EC_TZ = timezone(timedelta(hours=-5))  # America/Guayaquil

INTENT_LABELS = {
    "handoff_asesor": "Handoff asesor",
    "intencion_compra": "Intención de compra",
    "diagnostico_completado": "Diagnóstico",
}

security = HTTPBasic(auto_error=False)
router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_password():
    return os.environ.get("ADMIN_PASSWORD", "")


def _require_admin(creds: HTTPBasicCredentials = Depends(security)):
    """Dependencia de autenticación. Deniega todo si no hay contraseña."""
    password = _admin_password()
    if not password:
        log.warning("Intento de acceso a /admin sin ADMIN_PASSWORD configurada.")
        raise HTTPException(
            status_code=401,
            detail="Panel deshabilitado",
            headers={"WWW-Authenticate": "Basic"},
        )
    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Autenticación requerida",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = hmac.compare_digest(creds.username or "", ADMIN_USER)
    pw_ok = hmac.compare_digest(creds.password or "", password)
    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=401,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# ---------------------------------------------------------------------------
# Acceso a datos
# ---------------------------------------------------------------------------

def _safe_json(text):
    try:
        obj = json.loads(text or "{}")
    except (ValueError, TypeError):
        obj = {}
    return obj if isinstance(obj, dict) else {}


def _fmt_ts(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=EC_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "—"


def list_conversations():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT phone, state, data, updated_at FROM conversations "
            "ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        data = _safe_json(r["data"])
        hist = data.get("history")
        hist = hist if isinstance(hist, list) else []
        last = str(hist[-1].get("text", ""))[:90] if hist else ""
        out.append(
            {
                "phone": str(r["phone"]),
                "state": str(r["state"]),
                "count": len(hist),
                "last": last,
                "updated_at": _fmt_ts(r["updated_at"]),
                "handoff": r["state"] == "asesor",
            }
        )
    return out


def get_chat(phone):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT phone, state, data, updated_at FROM conversations WHERE phone = ?",
            (phone,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data = _safe_json(row["data"])
    hist = data.get("history")
    hist = hist if isinstance(hist, list) else []
    return {
        "phone": str(row["phone"]),
        "state": str(row["state"]),
        "updated_at": _fmt_ts(row["updated_at"]),
        "history": [
            {"role": h.get("role"), "text": str(h.get("text", ""))} for h in hist
        ],
    }


def list_leads(intent=""):
    conn = db.connect()
    try:
        if intent in INTENT_LABELS:
            rows = conn.execute(
                "SELECT phone, name, area, goal, service, intent, note, created_at "
                "FROM leads WHERE intent = ? ORDER BY created_at DESC",
                (intent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT phone, name, area, goal, service, intent, note, created_at "
                "FROM leads ORDER BY created_at DESC"
            ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "phone": str(r["phone"]),
                "name": str(r["name"] or ""),
                "area": str(r["area"] or ""),
                "goal": str(r["goal"] or ""),
                "service": str(r["service"] or ""),
                "intent": str(r["intent"] or ""),
                "intent_label": INTENT_LABELS.get(r["intent"], str(r["intent"] or "—")),
                "note": str(r["note"] or ""),
                "created_at": _fmt_ts(r["created_at"]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
background:#f4f5f7;color:#1a1a1a}
header{background:#0A1F44;color:#fff;padding:14px 24px;display:flex;
align-items:center;justify-content:space-between}
header h1{font-size:18px;margin:0;font-weight:600}
header h1 .gold{color:#C9A227}
nav a{color:#C9A227;text-decoration:none;margin-left:18px;font-size:14px}
main{max-width:1100px;margin:24px auto;padding:0 16px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}
th{background:#0A1F44;color:#fff;text-align:left;padding:10px 12px;font-size:13px}
td{padding:10px 12px;border-top:1px solid #eee;font-size:14px;vertical-align:top}
tr:hover td{background:#fafafa}
a{color:#0A1F44}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;
font-weight:600}
.badge.asesor{background:#C9A227;color:#0A1F44}
.badge.chat{background:#e3e9f2;color:#0A1F44}
.badge.inicio{background:#eee;color:#555}
.msg{max-width:70%;padding:10px 14px;border-radius:12px;margin:8px 0;font-size:14px;
line-height:1.45;white-space:pre-wrap;word-wrap:break-word}
.msg.user{background:#e8e8e8;margin-right:auto}
.msg.model{background:#0A1F44;color:#fff;margin-left:auto}
.meta{font-size:12px;color:#888;margin-bottom:16px}
.filters{margin-bottom:16px}
.filters a{display:inline-block;margin-right:8px;padding:6px 12px;border-radius:16px;
background:#fff;border:1px solid #ddd;font-size:13px;text-decoration:none;color:#0A1F44}
.filters a.active{background:#0A1F44;color:#fff;border-color:#0A1F44}
.empty{background:#fff;padding:32px;text-align:center;color:#888;border-radius:8px}
"""

_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — TALENTUM GROUP</title><style>{css}</style></head>
<body><header><h1>TALENTUM <span class="gold">GROUP</span> · Supervisión</h1>
<nav><a href="/admin/">Conversaciones</a><a href="/admin/leads">Leads</a></nav>
</header><main>{body}</main></body></html>"""


def _page(title, body):
    return _PAGE.format(title=html.escape(title), css=_CSS, body=body)


def _render_home(convs):
    if not convs:
        return _page("Conversaciones", '<div class="empty">Sin conversaciones aún.</div>')
    rows = []
    for c in convs:
        badge = (
            '<span class="badge asesor">ASESOR</span>'
            if c["handoff"]
            else f'<span class="badge {html.escape(c["state"])}">{html.escape(c["state"])}</span>'
        )
        rows.append(
            "<tr><td><a href=\"/admin/chat/{p}\">{p}</a></td>"
            "<td>{b}</td><td>{n}</td><td>{last}</td><td>{ts}</td></tr>".format(
                p=html.escape(c["phone"]),
                b=badge,
                n=c["count"],
                last=html.escape(c["last"]),
                ts=html.escape(c["updated_at"]),
            )
        )
    body = (
        "<table><tr><th>Teléfono</th><th>Estado</th><th>Msjs</th>"
        "<th>Último mensaje</th><th>Actualizado</th></tr>{}</table>".format("".join(rows))
    )
    return _page("Conversaciones", body)


def _render_chat(chat):
    if not chat:
        return _page("Chat", '<div class="empty">Conversación no encontrada.</div>')
    msgs = []
    for m in chat["history"]:
        cls = "user" if m["role"] == "user" else "model"
        quien = "Cliente" if m["role"] == "user" else "Bot"
        msgs.append(
            f'<div class="msg {cls}"><b>{quien}:</b><br>{html.escape(m["text"])}</div>'
        )
    body = (
        f'<div class="meta">Teléfono: <b>{html.escape(chat["phone"])}</b> · '
        f'Estado: <b>{html.escape(chat["state"])}</b> · '
        f'Actualizado: {html.escape(chat["updated_at"])}</div>'
        + ("".join(msgs) or '<div class="empty">Sin mensajes.</div>')
    )
    return _page(f'Chat {chat["phone"]}', body)


def _render_leads(leads, active):
    links = ['<a href="/admin/leads" class="{}">Todos</a>'.format(
        "active" if not active else "")]
    for key, label in INTENT_LABELS.items():
        links.append(
            '<a href="/admin/leads?intent={k}" class="{a}">{l}</a>'.format(
                k=key, a="active" if active == key else "", l=html.escape(label)
            )
        )
    body = f'<div class="filters">{"".join(links)}</div>'
    if not leads:
        return _page("Leads", body + '<div class="empty">Sin leads con este filtro.</div>')
    rows = []
    for ld in leads:
        rows.append(
            "<tr><td>{phone}</td><td>{name}</td><td>{area}</td><td>{goal}</td>"
            "<td>{service}</td><td>{intent}</td><td>{note}</td><td>{created_at}</td></tr>".format(
                **{k: html.escape(v) for k, v in ld.items() if k != "intent"},
                intent=html.escape(ld["intent_label"]),
            )
        )
    body += (
        "<table><tr><th>Teléfono</th><th>Nombre</th><th>Área</th><th>Objetivo</th>"
        "<th>Servicio</th><th>Intención</th><th>Nota / motivo</th><th>Fecha</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return _page("Leads", body)


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def admin_home(_: str = Depends(_require_admin)):
    """Lista de conversaciones, recientes primero."""
    return _render_home(list_conversations())


@router.get("/chat/{phone}", response_class=HTMLResponse)
def admin_chat(phone: str, _: str = Depends(_require_admin)):
    """Historial completo de una conversación."""
    return _render_chat(get_chat(phone))


@router.get("/leads", response_class=HTMLResponse)
def admin_leads(
    intent: str = Query(default=""), _: str = Depends(_require_admin)
):
    """Tabla de leads/handoffs con filtro por intención."""
    intent = intent if intent in INTENT_LABELS else ""
    return _render_leads(list_leads(intent), intent)
