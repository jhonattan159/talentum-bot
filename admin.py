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
  GET /admin/uso         -> consumo de tokens de Gemini (medición real)

HTML simple con identidad TALENTUM (navy #0A1F44 / dorado #C9A227).
Todo el contenido dinámico se escapa (html.escape) contra XSS.
"""

import html
import hmac
import hashlib
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import db

log = logging.getLogger("talentum.admin")

ADMIN_USER = "admin"
EC_TZ = timezone(timedelta(hours=-5))  # America/Guayaquil

# Sesión del formulario: cookie firmada con la propia ADMIN_PASSWORD.
# Cambiar la contraseña invalida las sesiones anteriores.
SESSION_COOKIE = "talentum_admin"
SESSION_MSG = b"talentum-admin-ok"


def _session_value():
    """Valor esperado de la cookie de sesión (hex HMAC-SHA256)."""
    pw = _admin_password()
    if not pw:
        return ""
    return hmac.new(pw.encode("utf-8"), SESSION_MSG, hashlib.sha256).hexdigest()


def _has_session(request):
    """True si la request trae una cookie de sesión válida."""
    expected = _session_value()
    if not expected:
        return False
    try:
        got = (request.cookies or {}).get(SESSION_COOKIE, "")
    except (AttributeError, TypeError):
        return False
    return bool(got) and hmac.compare_digest(got, expected)


def _check_login(username, password):
    """Valida credenciales del formulario. Nunca revela cuál falló."""
    expected = _admin_password()
    if not expected:
        return False
    user_ok = hmac.compare_digest(username or "", ADMIN_USER)
    pw_ok = hmac.compare_digest(password or "", expected)
    return bool(user_ok and pw_ok)

INTENT_LABELS = {
    "handoff_asesor": "Handoff asesor",
    "intencion_compra": "Intención de compra",
    "diagnostico_completado": "Diagnóstico",
}

security = HTTPBasic(auto_error=False)
router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_password():
    return os.environ.get("ADMIN_PASSWORD", "")


def _require_admin(
    request: Request, creds: HTTPBasicCredentials = Depends(security)
):
    """Dependencia de autenticación.

    Acepta (en orden): cookie de sesión del formulario, o el header
    HTTP Basic clásico como respaldo. Sin ADMIN_PASSWORD configurada,
    todo se deniega con 401. Sin credenciales, redirige al formulario
    (el diálogo nativo de Basic es hostil en móvil).
    """
    password = _admin_password()
    if not password:
        log.warning("Intento de acceso a /admin sin ADMIN_PASSWORD configurada.")
        raise HTTPException(
            status_code=401,
            detail="Panel deshabilitado",
            headers={"WWW-Authenticate": "Basic"},
        )
    if _has_session(request):
        return ADMIN_USER
    if creds is not None:
        # Respaldo: autenticación Basic clásica
        user_ok = hmac.compare_digest(creds.username or "", ADMIN_USER)
        pw_ok = hmac.compare_digest(creds.password or "", password)
        if user_ok and pw_ok:
            return ADMIN_USER
        raise HTTPException(
            status_code=401,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    # Sin sesión ni credenciales: ir al formulario de acceso
    raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


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
# Uso de tokens de Gemini
# ---------------------------------------------------------------------------

def _today_start_ts():
    """Unix timestamp de la medianoche de hoy (hora Ecuador)."""
    now = datetime.now(EC_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def usage_today():
    """Totales de hoy, uso por key y llamadas recientes.

    Devuelve dict con totales/por_key/recientes. Si la tabla no existe
    aún (sin llamadas registradas), devuelve estructura vacía.
    """
    empty = {"totals": {"calls": 0, "prompt": 0, "candidates": 0, "total": 0},
             "per_key": [], "recent": []}
    try:
        conn = db.connect()
    except Exception:
        log.exception("No se pudo abrir la DB para uso de tokens")
        return empty
    try:
        start = _today_start_ts()
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(prompt_tokens),0) AS p, "
            "COALESCE(SUM(candidates_tokens),0) AS c, "
            "COALESCE(SUM(total_tokens),0) AS t "
            "FROM gemini_usage WHERE ts >= ?",
            (start,),
        ).fetchone()
        per_key = conn.execute(
            "SELECT key_index, COUNT(*) AS n, "
            "COALESCE(SUM(total_tokens),0) AS t "
            "FROM gemini_usage WHERE ts >= ? "
            "GROUP BY key_index ORDER BY key_index",
            (start,),
        ).fetchall()
        recent = conn.execute(
            "SELECT ts, operation, model, key_index, prompt_tokens, "
            "candidates_tokens, total_tokens FROM gemini_usage "
            "ORDER BY ts DESC LIMIT 50"
        ).fetchall()
    except Exception:
        # Tabla inexistente (aún sin llamadas) u otro fallo: vacío.
        return empty
    finally:
        conn.close()
    return {
        "totals": {
            "calls": int(totals["n"] or 0),
            "prompt": int(totals["p"] or 0),
            "candidates": int(totals["c"] or 0),
            "total": int(totals["t"] or 0),
        },
        "per_key": [
            {"key_index": int(r["key_index"]), "calls": int(r["n"]),
             "tokens": int(r["t"] or 0)}
            for r in per_key
        ],
        "recent": [
            {"ts": _fmt_ts(r["ts"]), "operation": str(r["operation"] or ""),
             "model": str(r["model"] or ""), "key_index": int(r["key_index"]),
             "prompt": int(r["prompt_tokens"] or 0),
             "candidates": int(r["candidates_tokens"] or 0),
             "total": int(r["total_tokens"] or 0)}
            for r in recent
        ],
    }


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
<nav><a href="/admin/">Conversaciones</a><a href="/admin/leads">Leads</a><a href="/admin/uso">Uso de tokens</a></nav>
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


def _render_uso(stats):
    t = stats["totals"]
    cards = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;">'
        '<div style="background:#fff;border-radius:8px;padding:16px 20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:140px;">'
        '<div style="font-size:12px;color:#888;">Llamadas hoy</div>'
        '<div style="font-size:28px;font-weight:700;color:#0A1F44;">{calls}</div></div>'
        '<div style="background:#fff;border-radius:8px;padding:16px 20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:140px;">'
        '<div style="font-size:12px;color:#888;">Tokens de entrada</div>'
        '<div style="font-size:28px;font-weight:700;color:#0A1F44;">{prompt}</div></div>'
        '<div style="background:#fff;border-radius:8px;padding:16px 20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:140px;">'
        '<div style="font-size:12px;color:#888;">Tokens de salida</div>'
        '<div style="font-size:28px;font-weight:700;color:#0A1F44;">{cands}</div></div>'
        '<div style="background:#fff;border-radius:8px;padding:16px 20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:140px;">'
        '<div style="font-size:12px;color:#888;">Tokens totales</div>'
        '<div style="font-size:28px;font-weight:700;color:#C9A227;">{total}</div></div>'
        "</div>"
    ).format(calls=t["calls"], prompt=t["prompt"],
             cands=t["candidates"], total=t["total"])

    if stats["per_key"]:
        krows = "".join(
            "<tr><td>#{k}</td><td>{n}</td><td>{tok}</td></tr>".format(
                k=html.escape(str(k["key_index"])),
                n=k["calls"], tok=k["tokens"])
            for k in stats["per_key"]
        )
        key_table = (
            "<h3>Uso por clave (hoy)</h3>"
            "<table><tr><th>Clave</th><th>Llamadas</th><th>Tokens</th></tr>"
            + krows + "</table>"
        )
    else:
        key_table = '<div class="empty">Sin llamadas registradas hoy.</div>'

    if stats["recent"]:
        rrows = "".join(
            "<tr><td>{ts}</td><td>{op}</td><td>{model}</td><td>#{k}</td>"
            "<td>{p}</td><td>{c}</td><td>{t}</td></tr>".format(
                ts=html.escape(r["ts"]), op=html.escape(r["operation"]),
                model=html.escape(r["model"]),
                k=html.escape(str(r["key_index"])),
                p=r["prompt"], c=r["candidates"], t=r["total"])
            for r in stats["recent"]
        )
        recent_table = (
            "<h3>Llamadas recientes</h3>"
            "<table><tr><th>Hora</th><th>Operación</th><th>Modelo</th>"
            "<th>Clave</th><th>Entrada</th><th>Salida</th><th>Total</th></tr>"
            + rrows + "</table>"
        )
    else:
        recent_table = ""

    note = (
        "<p style=\"font-size:13px;color:#666;background:#fff;padding:14px 18px;"
        "border-radius:8px;border-left:4px solid #C9A227;\">"
        "<b>Nota:</b> los límites gratuitos de la API de Gemini se aplican por "
        "clave y se reinician cada día. Este panel muestra el consumo real "
        "medido llamada por llamada: úsalo para decidir cuándo conviene "
        "activar la facturación por uso (pay-as-you-go).</p>"
    )
    body = ("<h2>Uso de tokens — hoy</h2>" + cards + note + key_table
            + recent_table)
    return _page("Uso de tokens", body)


# ---------------------------------------------------------------------------
# Formulario de acceso (móvil-amigable; reemplaza el diálogo nativo Basic)
# ---------------------------------------------------------------------------

_LOGIN_CSS = """
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
background:#0A1F44;color:#1a1a1a;min-height:100vh;display:flex;
align-items:center;justify-content:center;padding:16px}
.card{background:#fff;border-radius:12px;padding:32px 28px;width:100%;
max-width:380px;box-shadow:0 8px 32px rgba(0,0,0,.3)}
.brand{font-size:13px;letter-spacing:3px;font-weight:700;color:#0A1F44;
margin-bottom:4px}
.brand span{color:#C9A227}
h1{font-size:20px;margin:0 0 20px;color:#0A1F44}
label{display:block;font-size:14px;font-weight:600;color:#0A1F44;margin:0 0 12px}
input{width:100%;box-sizing:border-box;margin-top:6px;padding:12px;
font-size:16px;border:1px solid #ccc;border-radius:8px}
button{width:100%;padding:14px;margin-top:8px;font-size:16px;font-weight:700;
color:#fff;background:#0A1F44;border:none;border-radius:8px;cursor:pointer}
button:active{background:#132c5e}
.error{background:#fdecea;color:#B42318;border:1px solid #f5c6c2;
border-radius:8px;padding:10px 12px;font-size:14px;margin-bottom:16px}
.hint{font-size:12px;color:#888;margin-top:16px;text-align:center}
"""

_LOGIN_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso — TALENTUM GROUP</title><style>{css}</style></head>
<body><div class="card">
<div class="brand">TALENTUM <span>GROUP</span></div>
<h1>Panel de supervisión</h1>
{error}
<form method="post" action="/admin/login">
<label>Usuario
<input name="username" autocomplete="username" autocapitalize="none"></label>
<label>Contraseña
<input type="password" name="password" autocomplete="current-password"></label>
<button type="submit">Entrar</button>
</form>
<div class="hint">Acceso restringido al administrador.</div>
</div></body></html>"""


def _login_form_html(error=""):
    err = (
        '<div class="error">{}</div>'.format(html.escape(error)) if error else ""
    )
    return _LOGIN_PAGE.format(css=_LOGIN_CSS, error=err)


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


@router.get("/uso", response_class=HTMLResponse)
def admin_uso(_: str = Depends(_require_admin)):
    """Consumo de tokens de Gemini: totales de hoy, por clave y recientes."""
    return _render_uso(usage_today())


@router.get("/login", response_class=HTMLResponse)
def admin_login(request: Request):
    """Formulario de acceso (no requiere autenticación previa)."""
    if not _admin_password():
        raise HTTPException(
            status_code=401,
            detail="Panel deshabilitado",
            headers={"WWW-Authenticate": "Basic"},
        )
    if _has_session(request):
        return RedirectResponse(url="/admin/", status_code=303)
    return _login_form_html()


@router.post("/login")
async def admin_login_post(request: Request):
    """Valida el formulario; en éxito fija la cookie de sesión."""
    if not _admin_password():
        raise HTTPException(
            status_code=401,
            detail="Panel deshabilitado",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        body = (await request.body()).decode("utf-8", "replace")
        params = urllib.parse.parse_qs(body)
        username = (params.get("username", [""])[0] or "").strip()
        password = params.get("password", [""])[0] or ""
    except Exception:
        username, password = "", ""
    if _check_login(username, password):
        resp = RedirectResponse(url="/admin/", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE,
            _session_value(),
            httponly=True,
            samesite="lax",
            secure=True,
            path="/",
        )
        return resp
    return HTMLResponse(
        _login_form_html("Usuario o contraseña inválidos."), status_code=401
    )
