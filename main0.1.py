# ============================================================
#  🤖 BOT FINANCIERO — VERSIÓN 2.2 (VALIDACIÓN CON GOOGLE)
# ============================================================
import os
import json
import math
import logging
import datetime
import threading
import requests
import yfinance as yf
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from http.server import BaseHTTPRequestHandler, HTTPServer
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

# 🔑 ZONA DE CLAVES
TOKEN_TELEGRAM = os.environ.get("TOKEN_TELEGRAM", "")
API_KEY_DEEPSEEK = os.environ.get("API_KEY_DEEPSEEK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
API_KEY_GEMINI = os.environ.get("GEMINI_API_KEY", "")

try:
    client_gemini = genai.Client(api_key=API_KEY_GEMINI)
except Exception as e:
    logging.error(f"Error Gemini: {e}")
    client_gemini = None

# ⚙️ CONFIGURACIÓN Y ESTADO
ZONA_HORARIA_OFFSET = -6 
CHAT_ID = None
job_radar = None
job_sniper = None

ESTADO_SNIPER_FILE = "sniper_state.json"
sniper_estado = {"activo": False, "ticker": "", "fin": "", "chat_id": None}

def cargar_estado():
    global sniper_estado
    if os.path.exists(ESTADO_SNIPER_FILE):
        try:
            with open(ESTADO_SNIPER_FILE, 'r') as f:
                sniper_estado = json.load(f)
        except: pass

def guardar_estado():
    with open(ESTADO_SNIPER_FILE, 'w') as f:
        json.dump(sniper_estado, f)

cargar_estado()

def obtener_hora_local():
    tz = datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET))
    return datetime.datetime.now(tz)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

DICCIONARIO = {
    "apple": "AAPL", "manzana": "AAPL", "tesla": "TSLA", "nvidia": "NVDA",
    "microsoft": "MSFT", "google": "GOOGL", "amazon": "AMZN", "meta": "META",
    "netflix": "NFLX", "samsung": "SSNLF", "toyota": "TM",
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sp500": "^GSPC", "s&p": "^GSPC", "s&p500": "^GSPC",
    "nasdaq": "^IXIC", "bancos": "KBE", "oro": "GC=F", "petroleo": "CL=F"
}

RADAR_ACTIVOS = {
    "Índices": ["^GSPC", "^IXIC"],
    "ETFs": ["SPY", "QQQ", "KBE", "GLD"],
    "Acciones": ["AAPL", "TSLA", "NVDA", "MSFT"],
    "Cripto": ["BTC-USD", "ETH-USD", "SOL-USD"],
    "Materias Primas": ["CL=F", "GC=F"]
}

# ============================================================
#  📊 FUNCIONES DE DATOS Y UTILIDADES
# ============================================================
def obtener_precio(ticker: str, verificar=False) -> dict:
    precio_yahoo = None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if not hist.empty:
            precio = hist['Close'].iloc[-1]
            if not math.isnan(precio) and len(hist) >= 2:
                cambio = ((precio - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100
                precio_yahoo = {"c": round(float(precio), 2), "dp": round(float(cambio), 2), "fuente": "Yahoo"}
    except Exception: pass

    # Validación rápida con Finnhub si el movimiento es mayor al 4%
    if verificar and precio_yahoo and abs(precio_yahoo["dp"]) >= 4.0 and not ticker.startswith("^"):
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
            data = requests.get(url, timeout=5).json()
            if data.get("c") and data.get("c") > 0:
                precio_fh = round(float(data["c"]), 2)
                cambio_fh = round(float(data.get("dp", 0)), 2)
                if abs(precio_yahoo["dp"] - cambio_fh) > 2.0:
                    return {"c": precio_fh, "dp": cambio_fh, "fuente": "Finnhub (Corregido)"}
        except Exception: pass

    if precio_yahoo: return precio_yahoo

    if not ticker.startswith("^"):
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
            data = requests.get(url, timeout=5).json()
            if data.get("c") and data.get("c") > 0:
                return {"c": round(float(data["c"]), 2), "dp": round(float(data.get("dp", 0)), 2), "fuente": "Finnhub"}
        except Exception: pass

    return {"c": "N/D", "dp": 0.0, "fuente": "Ninguna"}

def validar_con_google(ticker: str, cambio: float) -> bool:
    """Si hay una caída/subida muy fuerte, Gemini busca en Google si es real o un error."""
    if not client_gemini: return True # Si no hay Gemini, dejamos pasar la alerta
    
    prompt = (
        f"El sistema detectó que {ticker} tuvo un movimiento del {cambio:.2f}%. "
        "Busca en Google si HOY hubo una noticia real o evento de mercado que explique este movimiento. "
        "Responde ÚNICAMENTE con la palabra 'VERDADERO' si es un movimiento real y hay noticias, "
        "o 'FALSO' si el mercado está tranquilo y parece un error de datos."
    )
    try:
        res = client_gemini.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        return "VERDADERO" in res.text.upper()
    except Exception:
        return True # Si Gemini falla, dejamos pasar la alerta por seguridad

def generar_grafico(ticker: str, tiempo="semana") -> str:
    try:
        t = yf.Ticker(ticker)
        data = t.history(period="1d", interval="5m") if tiempo == "hoy" else t.history(period="5d", interval="15m")
        if data.empty: return None
        if data.index.tz is not None: data.index = data.index.tz_localize(None)
        filename = f"{ticker.replace('^','')}_tmp.png"
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=False)
        mpf.plot(data, type='candle', style=s, title=f'{ticker}', volume=True, savefig=filename)
        return filename
    except Exception: return None
    finally: plt.close('all')

def consultar_ia(prompt: str, system: str, motor="deepseek") -> str:
    if motor == "gemini" and client_gemini:
        try:
            res = client_gemini.models.generate_content(model='gemini-1.5-flash', contents=f"{system}\n\n{prompt}")
            return res.text
        except Exception: motor = "deepseek"

    if motor == "deepseek":
        try:
            url = "https://api.deepseek.com/chat/completions"
            headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
            payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            r = requests.post(url, headers=headers, json=payload, timeout=45)
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return "❌ Error: El cerebro analítico (DeepSeek) está saturado o Render se está reiniciando."
    return "Error de IA."

# ============================================================
#  🎯 MODO SNIPER Y RADAR GLOBAL
# ============================================================
async def tarea_sniper(context: ContextTypes.DEFAULT_TYPE):
    global sniper_estado
    if not sniper_estado["activo"]: return
    ahora = obtener_hora_local()
    fin = datetime.datetime.fromisoformat(sniper_estado["fin"]).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET)))
    
    if ahora >= fin:
        sniper_estado["activo"] = False
        guardar_estado()
        await context.bot.send_message(chat_id=sniper_estado["chat_id"], text="🏁 *Misión cumplida.* Modo Sniper desactivado.", parse_mode="Markdown")
        return

    q = obtener_precio(sniper_estado["ticker"], verificar=True)
    if q["c"] != "N/D" and abs(q["dp"]) >= 0.5:
        direccion = "📈 SUBIÓ" if q["dp"] > 0 else "📉 BAJÓ"
        msg = f"🚨 **¡ALERTA SNIPER EN {sniper_estado['ticker']}!**\nPrecio {direccion} un **{q['dp']:.2f}%**.\n💰 Actual: **${q['c']}**\n\n🤖 _Dime '¿vendo o espero?'_"
        await context.bot.send_message(chat_id=sniper_estado["chat_id"], text=msg, parse_mode="Markdown")

async def tarea_radar(context: ContextTypes.DEFAULT_TYPE, manual=False):
    if sniper_estado["activo"] or CHAT_ID is None: return
    
    hora_actual = obtener_hora_local()
    if not manual and (hora_actual.weekday() >= 5 or hora_actual.hour < 8 or hora_actual.hour > 17):
        return 
        
    if manual:
        msg_espera = await context.bot.send_message(chat_id=CHAT_ID, text="🔍 Iniciando escaneo manual del Radar Global...")

    contexto_noticias = consultar_ia("Resume en 3 líneas el sentimiento del mercado HOY.", "Analista macro.", "gemini")
    movimientos = []
    
    for cat, tickers in RADAR_ACTIVOS.items():
        for ticker in tickers:
            q = obtener_precio(ticker, verificar=True)
            if q["c"] != "N/D":
                umbral = 4.0 if "USD" in ticker else (1.5 if ticker in ["SPY","QQQ","^GSPC"] else 3.0)
                if abs(q["dp"]) >= umbral:
                    # 🚨 AQUÍ ESTÁ LA MAGIA: SI ES MUY GRANDE, CONSULTAMOS CON GOOGLE
                    if abs(q["dp"]) >= 5.0:
                        es_real = validar_con_google(ticker, q["dp"])
                        if not es_real:
                            logging.info(f"Alerta ignorada para {ticker}: Google no encontró noticias.")
                            continue # Ignoramos este movimiento falso
                    
                    movimientos.append(f"- {ticker}: {q['dp']:+.2f}% (${q['c']}) [{q['fuente']}]")

    if movimientos:
        analisis = consultar_ia(f"Movimientos: {', '.join(movimientos)}. Noticias: {contexto_noticias}. Da análisis de trading.", "Trader.", "deepseek")
        msg = f"🚨 **RADAR GLOBAL: MOVIMIENTOS** 🚨\n\n🕵️‍♂️ *Contexto:*\n_{contexto_noticias}_\n\n📊 *Precios:*\n" + "\n".join(movimientos[:5]) + f"\n\n🧠 *Análisis:*\n{analisis}"
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    else:
        if manual:
            await context.bot.send_message(chat_id=CHAT_ID, text="✅ Escaneo terminado. No hay movimientos reales y anormales en este momento (Google confirmó que todo está tranquilo).")

# ============================================================
#  🧠 CEREBRO CENTRAL
# ============================================================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global sniper_estado, CHAT_ID
    CHAT_ID = update.effective_chat.id
    texto = update.message.text.lower().strip()
    
    if "radar global" in texto or "escanear mercado" in texto:
        await tarea_radar(context, manual=True)
        return

    if sniper_estado["activo"]:
        if "cancelar" in texto or "suficiente" in texto or "parar" in texto:
            sniper_estado["activo"] = False
            guardar_estado()
            await update.message.reply_text("🛑 Modo Sniper desactivado.")
            return
        if "vendo" in texto or "espero" in texto or "que hago" in texto:
            q = obtener_precio(sniper_estado["ticker"], verificar=True)
            ans = consultar_ia(f"El usuario pregunta: '{texto}'. Precio: {q['c']} ({q['dp']}%). Responde como amigo trader en 2 líneas.", "Trader directo.")
            await update.message.reply_text(ans)
            return
        await update.message.reply_text("🎯 En modo Sniper. Dime 'cancelar' para salir.")
        return

    if "estado" in texto or "que tienes prendido" in texto:
        ping = "🟢 OK" if consultar_ia("Hola", "test") != "Error" else "🔴 FALLO"
        msg = f"📟 **PANEL**\nRadar: {'🔴 PAUSADO' if sniper_estado['activo'] else '🟢 ACTIVO'}\nSniper: {'🟢 ACTIVO en ' + sniper_estado['ticker'] if sniper_estado['activo'] else '🔴 INACTIVO'}\nDeepSeek: {ping}\nGemini: {'🟢 OK' if client_gemini else '🔴 FALLO'}"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if "/menu" in texto or texto == "menu":
        keyboard = [
            [InlineKeyboardButton("Apple", callback_data="menu_AAPL"), InlineKeyboardButton("Tesla", callback_data="menu_TSLA")],
            [InlineKeyboardButton("Bitcoin", callback_data="menu_BTC-USD"), InlineKeyboardButton("S&P 500", callback_data="menu_^GSPC")],
            [InlineKeyboardButton("🔍 Otro Activo", callback_data="otro_activo")]
        ]
        await update.message.reply_text("📊 **Menú Principal**\nSelecciona uno o dime qué empresa buscas.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    ticker_encontrado = None
    for key, val in DICCIONARIO.items():
        if key in texto:
            ticker_encontrado = val
            break
            
    if not ticker_encontrado:
        msg_espera = await update.message.reply_text("🔍 Identificando activo...")
        intento = consultar_ia(f"Extrae SOLO el ticker de: '{texto}'. Si no es un activo financiero, responde 'CHARLA'.", "Clasificador estricto.")
        ticker_encontrado = intento.strip().upper()
        await msg_espera.delete()
        
        if ticker_encontrado == 'CHARLA' or len(ticker_encontrado) > 7 or "ERROR" in ticker_encontrado:
            ans = consultar_ia(f"Responde amigablemente a: '{texto}'", "Asistente financiero amigable.")
            await update.message.reply_text(ans)
            return

    keyboard = [
        [InlineKeyboardButton("📊 Gráfico", callback_data=f"acc_grafico_{ticker_encontrado}")],
        [InlineKeyboardButton("🧠 Análisis 360°", callback_data=f"acc_analisis_{ticker_encontrado}")],
        [InlineKeyboardButton("📰 Noticias", callback_data=f"acc_noticias_{ticker_encontrado}")]
    ]
    await update.message.reply_text(f"Activo: **{ticker_encontrado}**\n¿Qué quieres hacer?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ============================================================
#  📱 BOTONES INTERACTIVOS
# ============================================================
async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data 
    
    if data == "otro_activo":
        await query.edit_message_text("Escribe el nombre de la empresa o el ticker que buscas (ej. *Coca-Cola* o *KO*).")
        return
        
    if data.startswith("menu_"):
        ticker = data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("📊 Gráfico", callback_data=f"acc_grafico_{ticker}")],
            [InlineKeyboardButton("🧠 Análisis 360°", callback_data=f"acc_analisis_{ticker}")],
            [InlineKeyboardButton("📰 Noticias", callback_data=f"acc_noticias_{ticker}")]
        ]
        await query.edit_message_text(f"Activo: **{ticker}**\n¿Qué quieres hacer?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("acc_"):
        _, accion, ticker = data.split("_")
        if accion == "grafico":
            await query.edit_message_text(f"📉 Generando velas de {ticker}...")
            file = generar_grafico(ticker)
            if file:
                with open(file, 'rb') as p: await query.message.reply_photo(photo=p, caption=f"📊 {ticker}")
                os.remove(file)
            await query.delete_message()
        elif accion == "analisis":
            await query.edit_message_text(f"🧠 Analizando {ticker}...")
            q = obtener_precio(ticker, verificar=True)
            prompt = f"Analiza {ticker}. Precio: {q['c']}. 1. IMPORTANTE HOY. 2. VELAS. 3. CORTO PLAZO. 4. MEDIO. 5. LARGO PLAZO."
            ans = consultar_ia(prompt, "Analista experto.")
            await query.edit_message_text(f"🧠 **Análisis 360° de {ticker}:**\n\n{ans}", parse_mode="Markdown")
        elif accion == "noticias":
            await query.edit_message_text(f"📰 Buscando noticias...")
            ans = consultar_ia(f"Noticias recientes de {ticker}.", "Periodista.")
            await query.edit_message_text(f"📰 **Noticias de {ticker}:**\n\n{ans}", parse_mode="Markdown")

# ============================================================
#  ▶️ ARRANQUE
# ============================================================
async def post_init(application: Application):
    global job_sniper, job_radar
    if sniper_estado["activo"]:
        job_sniper = application.job_queue.run_repeating(tarea_sniper, interval=180, first=5)
    
    ahora = obtener_hora_local()
    proxima = ahora.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    job_radar = application.job_queue.run_repeating(tarea_radar, interval=3600, first=(proxima - ahora).total_seconds())

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
    
    app = Application.builder().token(TOKEN_TELEGRAM).request(req).get_updates_request(req).post_init(post_init).build()
    
    app.add_handler(CommandHandler("menu", manejar_mensaje))
    app.add_handler(CommandHandler("estado", manejar_mensaje))
    app.add_handler(CallbackQueryHandler(manejar_botones))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    
    app.run_polling(drop_pending_updates=True)
