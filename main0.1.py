# ============================================================
#  🤖 BOT FINANCIERO 
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import datetime
import requests
import os
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# ============================================================
#  🔑 ZONA DE CLAVES
# ============================================================
TOKEN_TELEGRAM = os.environ.get("TOKEN_TELEGRAM", "")
API_KEY_DEEPSEEK = os.environ.get("API_KEY_DEEPSEEK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# ============================================================
#  ⚙️ CONFIGURACIÓN Y ZONA HORARIA (-6 CENTRAL)
# ============================================================
TICKERS_VIGILAR = ["AAPL", "TSLA", "NVDA"]
UMBRAL_CAMBIO_PCT = 3.0

# 🚨 AQUÍ ESTÁ TU HORARIO: -6 (EE.UU. Central) 🚨
ZONA_HORARIA_OFFSET = -6 

def obtener_hora_local():
    """Devuelve la hora actual según el horario de -6"""
    tz = datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET))
    return datetime.datetime.now(tz)

CHAT_ID = None
job_vigilancia = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ============================================================
#  📊 FUNCIONES DE DATOS
# ============================================================
def finnhub_quote(ticker: str) -> dict:
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def finnhub_noticias(ticker: str, limite: int = 3) -> list:
    hoy = datetime.date.today()
    desde = hoy - datetime.timedelta(days=7)
    url = "https://finnhub.io/api/v1/company-news"
    params = {"symbol": ticker, "from": desde.isoformat(), "to": hoy.isoformat(), "token": FINNHUB_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        notis = r.json()[:limite]
        return [f"- {n['headline']} ({n['source']})" for n in notis] or ["Sin noticias recientes."]
    except Exception as e:
        logging.error(f"Noticias {ticker}: {e}")
        return ["No se pudieron cargar noticias."]

# ============================================================
#  🧠 FUNCIONES DE IA
# ============================================================
def consultar_deepseek(prompt: str, system_prompt: str = "Eres un analista financiero experto y amigable. Responde en español, de forma clara y profesional.") -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ============================================================
#  🧠 EL CEREBRO CENTRAL
# ============================================================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    if texto.startswith('/'):
        texto = texto.split(' ', 1)[1] if ' ' in texto else texto.replace('/', '')
    
    if not texto.strip():
        await update.message.reply_text("⚠️ No entendí tu mensaje.")
        return

    mensaje_espera = await update.message.reply_text("🤔 Analizando...")

    system_prompt_clasificador = (
        "Eres un clasificador de intenciones. El usuario te dará un texto y debes devolver ÚNICAMENTE un objeto JSON válido, sin texto adicional. "
        "El JSON debe tener exactamente estas claves: "
        '{"accion": "noticias" | "precio" | "hora" | "reporte" | "charla", "ticker": "AAPL" o "null", "hora_texto": "15:40" o "null"}\n'
        "- 'noticias': si pide información o novedades de una empresa.\n"
        "- 'precio': si pide precio o análisis técnico.\n"
        "- 'hora': si pregunta la hora actual.\n"
        "- 'reporte': si pide que le avises a una hora específica (extrae la hora en formato 24h HH:MM).\n"
        "- 'charla': cualquier otra cosa."
    )

    prompt_clasificador = f"Texto del usuario: '{texto}'"
    
    try:
        respuesta_json_str = consultar_deepseek(prompt_clasificador, system_prompt_clasificador)
        respuesta_json_str = respuesta_json_str.replace("```json", "").replace("```", "").strip()
        clasificacion = json.loads(respuesta_json_str)
        
        accion = clasificacion.get("accion")
        ticker = clasificacion.get("ticker")
        hora_texto = clasificacion.get("hora_texto")

        if accion == "hora":
            ahora = obtener_hora_local().strftime("%I:%M %p")
            await mensaje_espera.delete()
            await update.message.reply_text(f"🕒 La hora actual es: {ahora} (Hora del Centro de EE.UU.).")
            return

        elif accion == "reporte" and hora_texto:
            try:
                h, m = map(int, hora_texto.split(":"))
                ahora = obtener_hora_local()
                objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
                if objetivo < ahora: objetivo += datetime.timedelta(days=1)
                
                espera = (objetivo - ahora).total_seconds()
                context.job_queue.run_once(tarea_reporte_agendado, when=espera, chat_id=update.effective_chat.id)
                
                await mensaje_espera.delete()
                await update.message.reply_text(f"✅ ¡Entendido! Te mandaré un reporte a las {hora_texto} (tu hora local).")
            except ValueError:
                await mensaje_espera.edit_text("❌ No entendí la hora. Intenta decir 'Avísame a las 15:40'.")
            return

        elif accion == "noticias" and ticker and ticker != "null":
            notis = finnhub_noticias(ticker, 3)
            if notis and "No se pudieron cargar" not in notis[0]:
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker}:*\n\n" + "\n".join(notis), parse_mode="Markdown")
                return
            else:
                prompt_resumen = f"Dame un resumen breve de las noticias y situación actual de la empresa {ticker}."
                resumen = consultar_deepseek(prompt_resumen)
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker} (resumen IA):*\n\n{resumen}", parse_mode="Markdown")
                return

        elif accion == "precio" and ticker and ticker != "null":
            q = finnhub_quote(ticker)
            notis = finnhub_noticias(ticker)
            prompt = f"Analiza {ticker}. Precio: {q.get('c')}, Cambio: {q.get('dp')}%. Noticias: {', '.join(notis)}. Da un análisis breve y predicción."
            analisis = consultar_deepseek(prompt)
            await mensaje_espera.delete()
            await update.message.reply_text(f"📊 *Análisis de {ticker}:*\n\n{analisis}", parse_mode="Markdown")
            return

        respuesta = consultar_deepseek(f"Responde amigablemente a esto: '{texto}'")
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)

    except json.JSONDecodeError:
        respuesta = consultar_deepseek(f"Responde amigablemente a esto: '{texto}'")
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error en procesar_mensaje: {e}")
        await mensaje_espera.edit_text("❌ Lo siento, tuve un problema de conexión al pensar.")

# ============================================================
#  🤖 COMANDOS Y TAREAS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 ¡Hola! Soy tu asistente financiero.\n\n"
        "Háblame como quieras, entiendo todo:\n"
        "👉 _\"Dame noticias de Apple\"_\n"
        "👉 _\"¿Qué hora es?\"_\n"
        "👉 _\"Avísame a las 15:40\"_\n\n"
        "Comandos de control:\n"
        "• /listo → Vigilancia automática.\n"
        "• /parar → Detener vigilancia.",
        parse_mode="Markdown"
    )

async def tarea_reporte_agendado(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id=chat_id, text="📊 *Reporte programado:*", parse_mode="Markdown")
    for t in TICKERS_VIGILAR:
        try:
            q = finnhub_quote(t)
            await context.bot.send_message(chat_id=chat_id, text=f"📈 {t}: ${q.get('c')} ({q.get('dp'):+.2f}%)")
        except: pass

async def listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID, job_vigilancia
    CHAT_ID = update.effective_chat.id
    if job_vigilancia is not None:
        await update.message.reply_text("✅ Ya estoy vigilando.")
        return
    ahora = obtener_hora_local()
    proxima = ahora.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    seg = (proxima - ahora).total_seconds()
    job_vigilancia = context.job_queue.run_repeating(tarea_vigilancia, interval=3600, first=seg)
    await update.message.reply_text(f"🚀 ¡Listo! Próxima revisión: {proxima.strftime('%H:%M')}")

async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_vigilancia
    if job_vigilancia is not None:
        job_vigilancia.schedule_removal()
        job_vigilancia = None
    await update.message.reply_text("🛑 Vigilancia detenida.")

async def tarea_vigilancia(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID is None: return
    alertas = []
    for t in TICKERS_VIGILAR:
        try:
            q = finnhub_quote(t)
            cambio = q.get("dp", 0)
            if cambio >= UMBRAL_CAMBIO_PCT: alertas.append(f"🔴 *VENDER* {t} — subió {cambio:.2f}%")
            elif cambio <= -UMBRAL_CAMBIO_PCT: alertas.append(f"🟢 *COMPRAR* {t} — cayó {cambio:.2f}%")
        except: pass
    msg = ("📢 *Señales:*\n\n" + "\n".join(alertas)) if alertas else "✅ Sin señales fuertes."
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
#  ▶️ ARRANQUE
# ============================================================
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot vivo")

def iniciar_servidor():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

if __name__ == "__main__":
    iniciar_servidor()
    print("🤖 Bot encendido en Render.")
    
    req = HTTPXRequest(read_timeout=30.0, connect_timeout=15.0)
    app = Application.builder().token(TOKEN_TELEGRAM).request(req).get_updates_request(req).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("listo", listo))
    app.add_handler(CommandHandler("parar", parar))
    
    app.add_handler(MessageHandler(filters.TEXT, procesar_mensaje))
    
    app.run_polling()
