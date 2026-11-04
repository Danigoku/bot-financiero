# ============================================================
#  🤖 BOT FINANCIERO — VERSIÓN DEFINITIVA (MAGIA + REPORTES)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import datetime
import requests
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# ============================================================
#  🔑 ZONA DE CLAVES (Se leen de Render)
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
#  📊 FUNCIONES DE DATOS (Finnhub)
# ============================================================
def finnhub_quote(ticker: str) -> dict:
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def finnhub_noticias(ticker: str, limite: int = 3) -> list:
    hoy = datetime.date.today()
    desde = hoy - datetime.timedelta(days=7) # Ampliado a 7 días
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
        "👉 _\"Dame las noticias de Apple\"_\n"
        "👉 _\"¿Cómo va Tesla hoy?\"_\n\n"
        "Comandos útiles:\n"
        "• /listo → Vigilancia automática cada hora.\n"
        "• /reporte HH:MM → Enviar reporte a hora exacta (ej. /reporte 15:40).\n"
        "• /predecir TICKER → Análisis profundo.",
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
        await update.message.reply_text("✅ Ya estoy vigilando.")
        return
    seg = proxima_hora_en_punto()
    job_vigilancia = context.job_queue.run_repeating(tarea_vigilancia, interval=3600, first=seg)
    proxima = datetime.datetime.now() + datetime.timedelta(seconds=seg)
    await update.message.reply_text(f"🚀 ¡Listo! Próxima revisión: {proxima.strftime('%H:%M')}")

async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_vigilancia
    if job_vigilancia is not None:
        job_vigilancia.schedule_removal()
        job_vigilancia = None
    await update.message.reply_text("🛑 Vigilancia detenida.")

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Activa" if job_vigilancia else "💤 Detenida")

# --- NUEVO COMANDO DE REPORTE ---
async def agendar_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso correcto: `/reporte 15:40` (formato 24h).")
        return
    hora_texto = context.args[0]
    try:
        partes = hora_texto.split(":")
        hora = int(partes[0])
        minuto = int(partes[1])
        
        # Usa hora UTC del servidor
        ahora = datetime.datetime.now(datetime.timezone.utc)
        objetivo = ahora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
        
        if objetivo < ahora:
            objetivo += datetime.timedelta(days=1)
            
        espera = (objetivo - ahora).total_seconds()
        context.job_queue.run_once(tarea_reporte_agendado, when=espera, chat_id=update.effective_chat.id)
        
        await update.message.reply_text(f"✅ ¡Entendido! Te mandaré un reporte a las {hora_texto} (hora del servidor).")
    except Exception as e:
        await update.message.reply_text("❌ Formato incorrecto. Usa `/reporte 15:40`.")

async def tarea_reporte_agendado(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id=chat_id, text="📊 *Aquí está tu reporte programado:*", parse_mode="Markdown")
    for t in TICKERS_VIGILAR:
        try:
            q = finnhub_quote(t)
            precio = q.get("c", 0)
            cambio = q.get("dp", 0)
            await context.bot.send_message(chat_id=chat_id, text=f"📈 {t}: ${precio} ({cambio:+.2f}%)")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Error con {t}")

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
        await update.message.reply_text("❌ No pude analizar ese ticker.")

async def noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /noticias AAPL")
        return
    t = context.args[0].upper()
    await update.message.reply_text(f"📰 Buscando noticias de {t}...")
    try:
        notis = finnhub_noticias(t, 5)
        if notis and "No se pudieron cargar" not in notis[0]:
            mensaje = f"📰 *Noticias {t}:*\n\n" + "\n".join(notis)
        else:
            prompt = f"Dame un resumen breve de las noticias y situación actual de la empresa {t}."
            respuesta_ia = consultar_deepseek(prompt)
            mensaje = f"📰 *Noticias {t} (resumen IA):*\n\n{respuesta_ia}"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text("❌ Error buscando noticias.")

# ============================================================
#  🗣️ CHAT LIBRE INTELIGENTE (Sin comandos)
# ============================================================
async def chat_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text
    mensaje_espera = await update.message.reply_text("🤔 Déjame revisar...")
    
    empresas = {
        "apple": "AAPL", "manzana": "AAPL", "tesla": "TSLA", "nvidia": "NVDA",
        "microsoft": "MSFT", "google": "GOOGL", "amazon": "AMZN",
        "meta": "META", "facebook": "META", "netflix": "NFLX"
    }
    
    texto_min = texto_usuario.lower()
    ticker_encontrado = None
    
    for nombre, ticker in empresas.items():
        if nombre in texto_min:
            ticker_encontrado = ticker
            break
            
    if ticker_encontrado and ("noticia" in texto_min or "pasa" in texto_min or "nueva" in texto_min):
        try:
            notis = finnhub_noticias(ticker_encontrado, 3)
            if notis and "No se pudieron cargar" not in notis[0]:
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 Aquí tienes las noticias recientes de {ticker_encontrado}:\n\n" + "\n".join(notis))
                return
        except:
            pass

    prompt = (
        f"El usuario me dice: '{texto_usuario}'\n\n"
        "Respóndele como un asistente financiero amigable y experto en español. Sé conciso."
    )
    
    try:
        respuesta = consultar_deepseek(prompt)
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error en chat libre: {e}")
        await mensaje_espera.edit_text("❌ Tuve un problema de conexión al pensar.")

# ============================================================
#  ⏰ TAREA AUTOMÁTICA (Cada hora)
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
            pass
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
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("noticias", noticias))
    app.add_handler(CommandHandler("reporte", agendar_reporte))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_libre))
    
    app.run_polling()
