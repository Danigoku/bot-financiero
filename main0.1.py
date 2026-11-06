# ============================================================
#  🤖 BOT FINANCIERO — VERSIÓN FINAL (RADAR GEMINI + DEEPSEEK)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import datetime
import requests
import os
import json
import math
import yfinance as yf
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

# ============================================================
#  🔑 ZONA DE CLAVES
# ============================================================
TOKEN_TELEGRAM = os.environ.get("TOKEN_TELEGRAM", "")
API_KEY_DEEPSEEK = os.environ.get("API_KEY_DEEPSEEK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
API_KEY_GEMINI = os.environ.get("GEMINI_API_KEY", "")

# Configurar cliente de Gemini
client_gemini = genai.Client(api_key=API_KEY_GEMINI)

# ============================================================
#  ⚙️ CONFIGURACIÓN Y DICCIONARIO
# ============================================================
ZONA_HORARIA_OFFSET = -6 
CHAT_ID = None
job_vigilancia = None

# Lista de Élite (Lo que el bot revisará si Gemini dice que hay movimiento)
RADAR_TICKERS = {
    "Índices": ["^GSPC", "^IXIC", "^DJI"],
    "ETFs Top": ["SPY", "QQQ", "KBE", "XLE", "GLD"], # Mercado, Tech, Bancos, Energía, Oro
    "Acciones": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "JPM", "BAC"],
    "Cripto": ["BTC-USD", "ETH-USD", "SOL-USD"],
    "Materias Primas": ["CL=F", "GC=F"] # Petróleo, Oro
}

DICCIONARIO_TICKERS = {
    "s&p 500": "^GSPC", "s&p": "^GSPC", "sp500": "^GSPC",
    "nasdaq": "^IXIC", "dow jones": "^DJI",
    "bitcoin": "BTC-USD", "btc": "BTC-USD", 
    "ethereum": "ETH-USD", "solana": "SOL-USD",
    "oro": "GC=F", "petroleo": "CL=F", 
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", 
    "bancos": "KBE"
}

def obtener_hora_local():
    tz = datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET))
    return datetime.datetime.now(tz)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ============================================================
#  📊 FUNCIONES DE DATOS Y ANÁLISIS
# ============================================================
def obtener_precio(ticker: str) -> dict:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if not hist.empty:
            precio = hist['Close'].iloc[-1]
            if not math.isnan(precio):
                if len(hist) >= 2:
                    prev = hist['Close'].iloc[-2]
                    cambio = ((precio - prev) / prev) * 100
                    return {"c": round(float(precio), 2), "dp": round(float(cambio), 2)}
                return {"c": round(float(precio), 2), "dp": 0.0}
    except Exception:
        pass
    return {"c": "N/D", "dp": 0.0}

def consultar_deepseek(prompt: str, system_prompt: str = "Eres un analista financiero experto. Responde en español.") -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def generar_grafico_velas(ticker: str, tiempo: str = "semana") -> str:
    try:
        t = yf.Ticker(ticker)
        data = t.history(period="1d", interval="5m") if tiempo == "hoy" else t.history(period="5d", interval="15m")
        if data.empty: return None
        if data.index.tz is not None: data.index = data.index.tz_localize(None)
        
        filename = f"{ticker.replace('^','')}_chart.png"
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=False)
        mpf.plot(data, type='candle', style=s, title=f'{ticker}', volume=True, savefig=filename)
        return filename
    except Exception:
        return None

# ============================================================
#  🚨 EL RADAR DE 3 PASOS (Gemini + Filtro + DeepSeek)
# ============================================================
async def tarea_radar(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID is None: return
    
    # PASO 1: Gemini investiga las noticias
    prompt_gemini = (
        "Actúa como un analista de Wall Street. Revisa las noticias financieras de HOY. "
        "Dame un resumen ULTRA BREVE (máximo 3 líneas) de qué sectores están en crisis, euforia o moviéndose raro. "
        "Menciona solo lo más importante (ej. bancos, IA, cripto, petróleo)."
    )
    
    try:
        response = client_gemini.models.generate_content(model='gemini-1.5-flash', contents=prompt_gemini)
        contexto_noticias = response.text
    except Exception as e:
        logging.error(f"Error Gemini: {e}")
        contexto_noticias = "No se pudieron cargar las noticias de última hora."

    # PASO 2: Escanear la Lista de Élite (Yahoo Finance)
    movimientos_fuertes = []
    for categoria, tickers in RADAR_TICKERS.items():
        for ticker in tickers:
            q = obtener_precio(ticker)
            if q.get('c') != "N/D":
                cambio = q.get("dp", 0)
                # Diferentes umbrales según el activo
                umbral = 3.0 # Base para acciones e índices
                if "USD" in ticker: umbral = 5.0 # Cripto se mueve más
                if ticker in ["KBE", "^GSPC", "^IXIC"]: umbral = 1.5 # ETFs e índices se mueven menos
                
                if abs(cambio) >= umbral:
                    movimientos_fuertes.append({
                        "ticker": ticker, "precio": q.get('c'), "cambio": cambio, "categoria": categoria
                    })

    # PASO 3: DeepSeek analiza los movimientos
    if movimientos_fuertes:
        prompt_ds = (
            f"El mercado tuvo estos movimientos fuertes: {json.dumps(movimientos_fuertes)}. "
            f"Contexto de noticias (Gemini): {contexto_noticias}. "
            "Dame un análisis breve y directo de si es oportunidad de trading (compra/venta) o riesgo de colapso."
        )
        analisis_ds = consultar_deepseek(prompt_ds)
        
        mensaje = "🚨🚨 **¡RADAR DE MERCADO DETECTADO!** 🚨🚨\n\n"
        mensaje += f"🕵️‍♂️ *Contexto (Gemini):*\n_{contexto_noticias}_\n\n"
        mensaje += "📊 *Movimientos Fuertes:*\n"
        for m in movimientos_fuertes:
            emoji = "📈" if m['cambio'] > 0 else "📉"
            mensaje += f"{emoji} **{m['ticker']}** ({m['categoria']}): ${m['precio']} ({m['cambio']:+.2f}%)\n"
            
        mensaje += f"\n🧠 *Análisis (DeepSeek):*\n{analisis_ds}"
        await context.bot.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")

# ============================================================
#  🤖 EL MENÚ INTERACTIVO
# ============================================================
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text("📊 **Menú**\n¿De qué activo quieres saber? (Ej. *Apple, Bitcoin, S&P 500*)", parse_mode="Markdown")
    context.user_data['esperando_ticker'] = True

async def recibir_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('esperando_ticker'):
        return 
    
    texto = update.message.text.lower().strip()
    ticker = DICCIONARIO_TICKERS.get(texto, update.message.text.upper())
    context.user_data['ticker_actual'] = ticker
    context.user_data['esperando_ticker'] = False
    
    keyboard = [
        [InlineKeyboardButton("📊 Gráfico Hoy", callback_data="grafico_hoy"), InlineKeyboardButton("📊 Gráfico Semana", callback_data="grafico_semana")],
        [InlineKeyboardButton("📰 Noticias", callback_data="noticias"), InlineKeyboardButton("🧠 Análisis 360°", callback_data="analisis")]
    ]
    await update.message.reply_text(f"Has seleccionado: **{ticker}**\n¿Qué quieres hacer?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def botones_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accion = query.data
    ticker = context.user_data.get('ticker_actual', 'AAPL')
    
    if accion.startswith("grafico"):
        tiempo = "hoy" if accion == "grafico_hoy" else "semana"
        await query.edit_message_text(f"📉 Generando gráfico de {ticker}...")
        archivo = generar_grafico_velas(ticker, tiempo)
        if archivo:
            with open(archivo, 'rb') as photo:
                await query.message.reply_photo(photo=photo, caption=f"📊 Velas de {ticker}")
            os.remove(archivo)
            await query.delete_message()
        else:
            await query.edit_message_text("❌ No se pudo generar el gráfico.")
            
    elif accion == "noticias":
        await query.edit_message_text(f"📰 Buscando noticias de {ticker}...")
        # Aquí podríamos usar Gemini, pero por rapidez usamos DeepSeek
        prompt = f"Dame un resumen de las noticias más recientes de {ticker}."
        notis = consultar_deepseek(prompt)
        await query.edit_message_text(f"📰 **Noticias de {ticker}:**\n\n{notis}", parse_mode="Markdown")
        
    elif accion == "analisis":
        await query.edit_message_text(f"🧠 Analizando {ticker}...")
        q = obtener_precio(ticker)
        prompt = (
            f"Analiza {ticker}. Precio: {q.get('c')}, Cambio hoy: {q.get('dp')}%. "
            "ESTRUCTURA: 1. LO MÁS IMPORTANTE HOY. 2. LECTURA DE VELAS. 3. CORTO PLAZO (Predicción). 4. MEDIO PLAZO. 5. LARGO PLAZO."
        )
        analisis = consultar_deepseek(prompt)
        await query.edit_message_text(f"🧠 **Análisis 360° de {ticker}:**\n\n{analisis}", parse_mode="Markdown")

# ============================================================
#  ▶️ ARRANQUE Y COMANDOS
# ============================================================
async def listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID, job_vigilancia
    CHAT_ID = update.effective_chat.id
    if job_vigilancia is not None:
        await update.message.reply_text("✅ El Radar ya está encendido.")
        return
    ahora = obtener_hora_local()
    proxima = ahora.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    job_vigilancia = context.job_queue.run_repeating(tarea_radar, interval=3600, first=(proxima - ahora).total_seconds())
    await update.message.reply_text("🚀 **¡Radar Gemini + DeepSeek Activado!**\nEscaneando mercado cada hora.")

async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_vigilancia
    if job_vigilancia is not None:
        job_vigilancia.schedule_removal()
        job_vigilancia = None
    await update.message.reply_text("🛑 Radar detenido.")

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
    req = HTTPXRequest(read_timeout=30.0, connect_timeout=15.0)
    app = Application.builder().token(TOKEN_TELEGRAM).request(req).get_updates_request(req).build()
    
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("listo", listo))
    app.add_handler(CommandHandler("parar", parar))
    app.add_handler(CallbackQueryHandler(botones_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ticker))
    
    app.run_polling()
