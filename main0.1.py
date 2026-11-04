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
#  ⚙️ CONFIGURACIÓN
# ============================================================
TICKERS_VIGILAR = ["AAPL", "TSLA", "NVDA"]
UMBRAL_CAMBIO_PCT = 3.0

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
def consultar_deepseek(prompt: str) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Eres un analista financiero experto y amigable. Responde en español, de forma clara y profesional."},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ============================================================
#  🧠 EL CEREBRO CENTRAL (Decide qué hacer con CUALQUIER mensaje)
# ============================================================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Agarramos el texto del usuario (si es un comando, quitamos la barra)
    texto = update.message.text
    if texto.startswith('/'):
        texto = texto.split(' ', 1)[1] if ' ' in texto else texto.replace('/', '')
    
    if not texto.strip():
        await update.message.reply_text("⚠️ No entendí tu mensaje. ¿Puedes repetirlo?")
        return

    mensaje_espera = await update.message.reply_text("🤔 Analizando...")

    # 1. La IA clasifica qué quiere el usuario
    prompt_clasificador = (
        f"Analiza este mensaje: '{texto}'\n\n"
        "Responde SOLO con un formato JSON con las claves:\n"
        '{"accion": "noticias" | "precio" | "charla", "ticker": "AAPL" o "null"}\n'
        "- Si pide noticias, novedades, qué pasa con una empresa -> accion: 'noticias'\n"
        "- Si pide precio, cómo va la acción, análisis -> accion: 'precio'\n"
        "- Si es un saludo o charla general -> accion: 'charla'\n"
        "Identifica el ticker en base a nombres (Apple -> AAPL, Tesla -> TSLA, etc)."
    )
    
    try:
        respuesta_json_str = consultar_deepseek(prompt_clasificador)
        respuesta_json_str = respuesta_json_str.replace("```json", "").replace("```", "").strip()
        clasificacion = json.loads(respuesta_json_str)
        
        accion = clasificacion.get("accion")
        ticker = clasificacion.get("ticker")

        # 2. ACTUAR SEGÚN LO QUE DIJO LA IA
        if accion == "noticias" and ticker and ticker != "null":
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

        # 3. Si es charla normal o no detectó nada específico
        respuesta = consultar_deepseek(f"Responde amigablemente a esto: '{texto}'")
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)

    except Exception as e:
        logging.error(f"Error en procesar_mensaje: {e}")
        await mensaje_espera.edit_text("❌ Lo siento, tuve un problema de conexión al pensar. Intenta de nuevo.")

# ============================================================
#  🤖 COMANDOS ESPECÍFICOS (Para reportes y configuración)
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 ¡Hola! Soy tu asistente financiero.\n\n"
        "Háblame como quieras, entiendo todo:\n"
        "👉 _\"Dame noticias de Apple\"_\n"
        "👉 _\"¿Cómo va Tesla?\"_\n"
        "👉 _\"Hola, ¿qué tal?\"_\n\n"
        "Comandos de control:\n"
        "• /reporte HH:MM (24h) → Reporte programado.\n"
        "• /listo → Vigilancia automática.\n"
        "• /parar → Detener vigilancia.",
        parse_mode="Markdown"
    )

async def agendar_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/reporte 15:40` (24h).")
        return
    try:
        h, m = map(int, context.args[0].split(":"))
        ahora = datetime.datetime.now(datetime.timezone.utc)
        objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
        if objetivo < ahora: objetivo += datetime.timedelta(days=1)
        espera = (objetivo - ahora).total_seconds()
        context.job_queue.run_once(tarea_reporte_agendado, when=espera, chat_id=update.effective_chat.id)
        await update.message.reply_text(f"✅ Reporte agendado para las {context.args[0]} (UTC).")
    except:
        await update.message.reply_text("❌ Usa `/reporte 15:40`.")

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
    ahora = datetime.datetime.now()
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

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Activa" if job_vigilancia else "💤 Detenida")

# ============================================================
#  ⏰ TAREA AUTOMÁTICA
# ============================================================
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
    
    # Comandos de configuración (no pasan por el cerebro, son directos)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("listo", listo))
    app.add_handler(CommandHandler("parar", parar))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("reporte", agendar_reporte))
    
    # ¡La magia! TODO lo demás pasa por el cerebro procesador
    app.add_handler(MessageHandler(filters.TEXT, procesar_mensaje))
    
    app.run_polling()
