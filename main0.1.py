# ============================================================
#  🤖 BOT FINANCIERO — PYDROID 3 / RENDER (VERSIÓN FINAL)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import datetime
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# ============================================================
#  🔑 ZONA DE CLAVES — RELLENA SOLO ESTAS 3 LÍNEAS
# ============================================================

# 👇 AQUÍ VA TU TOKEN DE TELEGRAM (lo da @BotFather)
TOKEN_TELEGRAM = ""

# 👇 AQUÍ VA TU API KEY DE DEEPSEEK (empieza por sk-)
API_KEY_DEEPSEEK = ""

# 👇 AQUÍ VA TU API KEY DE FINNHUB (la sacaste en finnhub.io)
FINNHUB_KEY = ""

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
#  📊 FUNCIONES DE DATOS (Finnhub)
# ============================================================
def finnhub_quote(ticker: str) -> dict:
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def finnhub_noticias(ticker: str, limite: int = 3) -> list:
    hoy = datetime.date.today()
    desde = hoy - datetime.timedelta(days=3)
    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": ticker,
        "from": desde.isoformat(),
        "to": hoy.isoformat(),
        "token": FINNHUB_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        notis = r.json()[:limite]
        return [f"- {n['headline']} ({n['source']})" for n in notis] or ["Sin noticias recientes."]
    except Exception as e:
        logging.error(f"Noticias {ticker}: {e}")
        return ["No se pudieron cargar noticias."]

# ============================================================
#  🧠 FUNCIONES DE IA (DeepSeek)
# ============================================================
def consultar_deepseek(prompt: str) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY_DEEPSEEK}",
        "Content-Type": "application/json",
    }
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

def generar_analisis(ticker: str, profundo: bool = False) -> str:
    q = finnhub_quote(ticker)
    precio = q.get("c", 0)
    cambio_pct = q.get("dp", 0)
    noticias = finnhub_noticias(ticker)
    contexto = "\n".join(noticias)

    prompt = (
        f"Ticker: {ticker}\n"
        f"Precio actual: {precio}\n"
        f"Cambio hoy: {cambio_pct:.2f}%\n"
        f"Máximo día: {q.get('h', 'N/A')}\n"
        f"Mínimo día: {q.get('l', 'N/A')}\n"
        f"Cierre anterior: {q.get('pc', 'N/A')}\n\n"
        f"Noticias recientes:\n{contexto}\n\n"
    )
    if profundo:
        prompt += "Haz una predicción razonada a corto plazo. ¿Comprar, vender o esperar? Explica por qué."
    else:
        prompt += "Dame un análisis breve y profesional en español."
    return consultar_deepseek(prompt)

# ============================================================
#  🤖 COMANDOS DE TELEGRAM
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 ¡Hola! Soy tu asistente financiero con IA.\n\n"
        "Puedes hablarme normal, por ejemplo:\n"
        "👉 _\"¿Cómo ves Tesla hoy?\"_\n"
        "👉 _\"Me conviene comprar Apple?\"_\n\n"
        "O usar los comandos rápidos:\n"
        "• /listo → Arranca la vigilancia automática cada hora.\n"
        "• /parar → Detiene la vigilancia.\n"
        "• /predecir TICKER → Análisis profundo (ej. /predecir AAPL).\n"
        "• /noticias TICKER → Últimas noticias.\n"
        "• /explica TICKER → ¿Qué está pasando con la empresa?",
        parse_mode="Markdown"
    )

def proxima_hora_en_punto() -> float:
    ahora = datetime.datetime.now()
    proxima = ahora.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    return (proxima - ahora).total_seconds()

async def listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID, job_vigilancia
    CHAT_ID = update.effective_chat.id
    if job_vigilancia is not None:
        await update.message.reply_text("✅ Ya estoy vigilando. Sigo con el ritmo por horas en punto (no reinicio).")
        return
    seg = proxima_hora_en_punto()
    job_vigilancia = context.job_queue.run_repeating(tarea_vigilancia, interval=3600, first=seg)
    proxima = datetime.datetime.now() + datetime.timedelta(seconds=seg)
    await update.message.reply_text(
        f"🚀 ¡Listo! Vigilando: {', '.join(TICKERS_VIGILAR)}\n"
        f"⏰ Próxima revisión: {proxima.strftime('%H:%M')} y luego cada hora en punto."
    )

async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_vigilancia
    if job_vigilancia is None:
        await update.message.reply_text("ℹ️ Ya estaba detenida.")
        return
    job_vigilancia.schedule_removal()
    job_vigilancia = None
    await update.message.reply_text("🛑 Vigilancia detenida.")

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_vigilancia is None:
        await update.message.reply_text("💤 Detenida. Usa /listo para arrancar.")
    else:
        await update.message.reply_text("✅ Activa, revisando cada hora en punto.")

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /predecir AAPL")
        return
    t = context.args[0].upper()
    await update.message.reply_text(f"🔮 Analizando {t}...")
    try:
        texto = generar_analisis(t, profundo=True)
        await update.message.reply_text(f"🔮 *Predicción {t}*\n\n{texto}", parse_mode="Markdown")
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ No pude analizar ese ticker.")

async def noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /noticias AAPL")
        return
    t = context.args[0].upper()
    await update.message.reply_text(f"📰 *Noticias {t}:*\n\n" + "\n".join(finnhub_noticias(t, 5)), parse_mode="Markdown")

async def explica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /explica AAPL")
        return
    t = context.args[0].upper()
    await update.message.reply_text(f"🧠 Analizando {t}...")
    try:
        texto = generar_analisis(t, profundo=True)
        await update.message.reply_text(f"🧠 *¿Qué pasa con {t}?*\n\n{texto}", parse_mode="Markdown")
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ No pude explicar ese ticker.")

# ============================================================
#  🗣️ CHAT LIBRE (Entiende lenguaje humano)
# ============================================================
async def chat_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a cualquier mensaje que no sea un comando."""
    texto_usuario = update.message.text
    mensaje_espera = await update.message.reply_text("🤔 Pensando...")
    
    prompt = (
        f"El usuario me dice: '{texto_usuario}'\n\n"
        "Respóndele como un asistente financiero amigable y experto en español. "
        "Si te pregunta por una acción o empresa, dale tu opinión o consejo brevemente. "
        "No uses formato Markdown complicado, responde como si fuera una conversación normal."
    )
    
    try:
        respuesta = consultar_deepseek(prompt)
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error en chat libre: {e}")
        await mensaje_espera.edit_text("❌ Lo siento, tuve un problema de conexión al pensar. Intenta de nuevo.")

# ============================================================
#  ⏰ TAREA AUTOMÁTICA CADA HORA EN PUNTO
# ============================================================
async def tarea_vigilancia(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID is None:
        return
    alertas = []
    for t in TICKERS_VIGILAR:
        try:
            q = finnhub_quote(t)
            cambio = q.get("dp", 0)
            if cambio >= UMBRAL_CAMBIO_PCT:
                alertas.append(f"🔴 *VENDER* {t} — subió {cambio:.2f}%")
            elif cambio <= -UMBRAL_CAMBIO_PCT:
                alertas.append(f"🟢 *COMPRAR* {t} — cayó {cambio:.2f}%")
        except Exception as e:
            logging.error(f"Error {t}: {e}")
    msg = ("📢 *Señales de la hora:*\n\n" + "\n".join(alertas)) if alertas else "✅ Sin señales fuertes esta hora."
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
#  ▶️ ARRANQUE (Única función main)
# ============================================================
# --- SERVIDOR WEB PARA RENDER (Despertador) ---
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot vivo")

def iniciar_servidor():
    import os 
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- ARRANQUE DEL BOT ---
if __name__ == "__main__":
    iniciar_servidor()
    print("🤖 Bot encendido en Render.")
    
    req = HTTPXRequest(read_timeout=30.0, connect_timeout=15.0)
    app = Application.builder().token(TOKEN_TELEGRAM).request(req).get_updates_request(req).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("listo", listo))
    app.add_handler(CommandHandler("parar", parar))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("noticias", noticias))
    app.add_handler(CommandHandler("explica", explica))
    
    # ¡El toque mágico para que hable como humano!
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_libre))
    
    app.run_polling()