# Chatbot de WhatsApp — TALENTUM GROUP

Motor de conversación para el WhatsApp Business de TALENTUM GROUP (consultoría que
optimiza CVs para superar filtros ATS). Tono ejecutivo y analítico; cero clichés
motivacionales de RR.HH.

## Estructura

```
chatbot/
├── app.py            # Webhook FastAPI (verificación + recepción + envío vía Graph API)
├── brain.py          # Cerebro LLM: integración con Google Gemini (conversación)
├── engine.py         # Enrutamiento conversacional + persistencia en SQLite
├── requirements.txt
├── Dockerfile
├── render.yaml       # Despliegue en Render (tier gratuito)
├── .env.example      # Plantilla de variables de entorno (sin secretos)
└── README.md
```

## Cómo conversa el bot (cerebro LLM con Gemini)

El bot ya no usa un menú rígido: cada mensaje pasa por `brain.py`, que consulta
a Google Gemini con el historial de la conversación y un system prompt con la
identidad de TALENTUM GROUP (ingeniería de perfiles, tono ejecutivo y analítico,
cero clichés motivacionales de RR.HH., tarifario completo y objetivos de
calificación de leads).

- **Conversación natural:** el LLM saluda, califica (nombre, área profesional,
  objetivo laboral), ofrece el diagnóstico ATS gratuito y presenta servicios
  según el interés detectado.
- **Derivación a humano:** ocurre cuando el cliente pide "asesor", muestra
  frustración (detectado por reglas + por el propio LLM con el marcador
  `HANDOFF:`), pregunta algo fuera de alcance o quiere contratar (el bot NO
  procesa pagos). Tras derivar, el bot queda en silencio para no interrumpir
  al asesor ("menú" lo reactiva).
- **Leads:** el LLM extrae nombre/área/objetivo/servicio/intención del historial
  y se guardan en la tabla `leads` de `conversations.db` (diagnósticos
  completados, intenciones de compra y handoffs).
- **Tolerancia a fallos:** si la API de Gemini no responde (sin key, error de
  red, cuota), el bot contesta con un mensaje de contingencia amable y el
  webhook nunca se cae.

Los leads quedan en la tabla `leads` de `conversations.db`.

## API key de Gemini (gratis)

1. Entra a **https://aistudio.google.com/** con una cuenta de Google.
2. **Get API Key** → **Create API key** (el tier gratuito es suficiente para
   empezar: ~15 peticiones/minuto en `gemini-2.0-flash`).
3. Copia la key y ponla en la variable `GEMINI_API_KEY` (Render → Environment,
   o tu `.env` local). Opcional: `GEMINI_MODEL` para cambiar de modelo
   (por defecto `gemini-2.0-flash`).

> La key es un secreto: nunca la pegues en chats ni la subas a git.

---

## 1. Configuración en Meta (lo hace el dueño, una sola vez)

### 1.1 Portafolio comercial
1. Entra a **business.facebook.com** → crea un portafolio comercial (antes "Business Manager")
   con los datos del negocio.
2. Completa la verificación de la empresa si Meta la pide (da acceso a límites mayores).

### 1.2 App en developers.facebook.com
1. Entra a **developers.facebook.com** → Mis apps → Crear app → tipo **Empresa**.
2. En el panel de la app: Agregar producto → **WhatsApp** → Configurar.
3. Ve a **WhatsApp → API Setup**.

### 1.3 Agregar y verificar el número
1. En API Setup, sección "Send and receive messages": **agrega el número** del negocio
   (el chip nuevo) con código país +593.
2. Meta lo **verifica por SMS o llamada**: llega un código de 6 dígitos al teléfono;
   ingrésalo en el panel. Desde ese momento el número queda vinculado a la Cloud API
   (sale de la app de WhatsApp: es lo esperado en la Ruta 1).
3. En la misma pantalla copia estos dos valores:
   - **Phone number ID** ("Identificador del número de teléfono") → `PHONE_NUMBER_ID`
   - **WhatsApp Business Account ID** (WABA_ID) → guárdalo como referencia.

### 1.4 Token de acceso permanente
1. El token que muestra API Setup es **temporal (24 h)** y sirve solo para pruebas.
2. Para producción: en el portafolio comercial crea un **System User**
   (Configuración del portafolio → Usuarios del sistema → Añadir),
   asígnale la app con permiso `whatsapp_business_messaging` y genera un **token
   permanente**. Ese valor va en `WHATSAPP_TOKEN`.
3. **VERIFY_TOKEN**: cadena secreta que inventas tú (ej: `talentum_verify_9f3kq7`);
   debe coincidir EXACTAMENTE entre tu `.env`/Render y el panel de Meta.

> Nunca pegues tokens en chats ni los subas a git. Solo viven en el `.env` local
> (no versionado) y en las variables de entorno de Render.

---

## 2. Despliegue en Render (paso a paso)

1. Sube esta carpeta a un repositorio de GitHub (ej: `talentum-chatbot`).
2. Entra a **render.com** → New → **Web Service** → conecta el repositorio.
3. Configuración:
   - **Runtime:** Docker (usa el `Dockerfile` incluido; `render.yaml` ya lo declara)
   - **Plan:** Free
   - **Health check path:** `/health`
4. En **Environment**, agrega las variables (el `render.yaml` las declara; complétalas):
   - `VERIFY_TOKEN` = tu cadena secreta
   - `WHATSAPP_TOKEN` = token permanente del System User
   - `PHONE_NUMBER_ID` = el ID copiado en el paso 1.3
   - `GEMINI_API_KEY` = tu API key de Google AI Studio (ver sección "API key de Gemini")
   - `GEMINI_MODEL` = opcional (por defecto `gemini-2.0-flash`)
5. **Deploy** → Render te da una URL pública HTTPS, ej:
   `https://talentum-chatbot.onrender.com`
6. Verifica: abre `https://talentum-chatbot.onrender.com/health` → debe responder
   `{"status":"ok","service":"talentum-chatbot"}`.

> Nota del tier gratuito: el servicio "duerme" tras ~15 min sin tráfico (arranque en
> frío de ~30-60 s en la primera petición) y el disco es **efímero**: `conversations.db`
> se reinicia con cada despliegue o reinicio. Para producción, migrar la persistencia
> a PostgreSQL (Render ofrece Postgres gratuito con límite).

---

## 3. Registrar el webhook en Meta

1. En tu app de Meta ve a **WhatsApp → Configuration**.
2. En la sección **Webhook**: Callback URL =
   `https://talentum-chatbot.onrender.com/webhook`
   (tu URL de Render + `/webhook`), Verify token = tu `VERIFY_TOKEN`.
3. Clic en **Verify and save**: Meta hace un `GET /webhook?hub.mode=subscribe...`
   y el servicio debe devolver el `hub.challenge` (el código ya lo implementa).
4. En **Webhook fields** haz clic en **Manage** y suscríbete al campo **messages**.
5. Prueba: envía un mensaje de WhatsApp al número del negocio desde otro teléfono.
   Debes recibir la bienvenida del bot.

---

## 4. Probar en local

```bash
cd ~/workspace/talentum-brand/chatbot

# 4.1 Simulador de consola del motor (conversacional vía Gemini)
DB_PATH=/tmp/test-conversations.db python3 engine.py
# Con GEMINI_API_KEY configurada: conversación real con el LLM.
# Sin key: verás el fallback amable (el bot sigue respondiendo).
# Prueba: hola → cuéntale tu área → pide un asesor → verifica el handoff.

# 4.2 Servidor local (requiere dependencias)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # completa VERIFY_TOKEN (los demás pueden quedar vacíos para probar)
uvicorn app:app --reload --port 8000

# 4.3 Verificación del webhook (imita a Meta)
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=TU_TOKEN&hub.challenge=12345"
# Debe devolver: 12345

# 4.4 Simular un mensaje entrante (payload real de la Cloud API)
curl -X POST http://localhost:8000/webhook -H "Content-Type: application/json" -d '{
  "entry": [{"changes": [{"value": {"messages": [
    {"from": "593960984483", "type": "text", "text": {"body": "hola"}}
  ]}}]}]}'
# Sin WHATSAPP_TOKEN configurado, el envío se omite con un error en el log;
# el motor igual procesa el mensaje (verifícalo con el simulador del paso 4.1).
```

---

## 5. Revisar leads y conversaciones

Los datos viven en `conversations.db` (SQLite), junto al código.

```bash
# Con la CLI de sqlite3 (si está instalada):
sqlite3 conversations.db \
  "SELECT id, phone, name, area, goal, service, intent,
          datetime(created_at,'unixepoch','localtime') AS fecha
   FROM leads ORDER BY id DESC;"

# Sin sqlite3, con Python:
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect("conversations.db")
for r in c.execute("SELECT id, phone, name, area, goal, service, intent FROM leads ORDER BY id DESC"):
    print(r)
EOF
```

- Tabla `conversations`: estado actual de cada número (para depurar el flujo).
- Tabla `leads`: cada diagnóstico completado, intención de compra y derivación a
  asesor, con fecha. Es la lista de contacto manual del negocio.

---

## 6. Operativa diaria

- **El bot no cobra.** Toda intención de compra genera un lead con
  `intent='intencion_compra'` y deriva a humano para coordinar pago y entrega.
- **Tras derivar a asesor**, el bot no responde más a ese número (estado `asesor`).
  El cliente puede escribir `menú` para reactivarlo.
- **Respuestas rápidas sugeridas** (para el asesor humano al tomar un lead):
  usar el resumen guardado en `leads` (nombre, área, objetivo, servicio).
