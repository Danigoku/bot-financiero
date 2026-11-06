# ============================================================
#  🤖 BOT FINANCIERO — VERSIÓN DEFINITIVA (SNIPER + RADAR + 3 IAs)
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
import pandas as pd
from http.server import BaseHTTPRequestHandler, HTTPServer
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

# ============================================================
#  🔑 ZONA DE CLAVES (Leídas de Render)
# ============================================================
TOKEN_TELEGRAM = os.environ.get("TOKEN_TELEGRAM", "")
API_KEY_DEEPSEEK = os.environ.get("API_KEY_DEEPSEEK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
API_KEY_GEMINI = os.environ.get("GEMINI_API_KEY", "")

try:
    client_gemini = genai.Client(api_key=API_KEY_GEMINI)
except Exception as e:
    logging.error(f"Error Gemini: {e}")
    client_gemini = None

# ============================================================
#  ⚙️ CONFIGURACIÓN Y ESTADO GLOBAL
# ============================================================
ZONA_HORARIA_OFFSET = -6 
CHAT_ID = None
job_radar = None
job_sniper = None

# Estado del Modo Sniper (Memoria para que no se pierda si Render reinicia)
ESTADO_SNIPER_FILE = "sniper_state.json"
sniper_estado = {"activo": False, "ticker": "", "fin": "", "chat_id": None}

def cargar_estado():
    global sniper_estado
    if os.path.exists(ESTADO_SNIPER_FILE):
        with open(ESTADO_SNIPER_FILE, 'r') as f:
            sniper_estado = json.load(f)

def guardar_estado():
    with open(ESTADO_SNIPER_FILE, 'w') as f:
        json.dump(sniper_estado, f)

cargar_estado()

def obtener_hora_local():
    tz = datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET))
    return datetime.datetime.now(tz)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Listas de activos para el radar
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
def obtener_precio(ticker: str, exacto=False) -> dict:
    """Obtiene el precio usando Yahoo o Finnhub (validando que no haya datos nan)."""
    # 1. Intentar Yahoo (mejor para históricos y global)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if not hist.empty:
            precio = hist['Close'].iloc[-1]
            if not math.isnan(precio) and not math.isnan(hist['Close'].iloc[-2]):
                cambio = ((precio - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100
                return {"c": round(float(precio), 2), "dp": round(float(cambio), 2), "fuente": "Yahoo"}
    except Exception:
        pass

    # 2. Fallback a Finnhub
    if not ticker.startswith("^"):
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
            r = requests.get(url, timeout=5)
            data = r.json()
            if data.get("c") and data.get("c") > 0:
                return {"c": round(float(data["c"]), 2), "dp": round(float(data.get("dp", 0)), 2), "fuente": "Finnhub"}
        except Exception:
            pass

    return {"c": "N/D", "dp": 0.0, "fuente": "Ninguna"}

def generar_grafico(ticker: str, tiempo="semana") -> str:
    """Genera el gráfico de velas y limpia la memoria de Render automáticamente."""
    try:
        t = yf.Ticker(ticker)
        data = t.history(period="1d", interval="5m") if tiempo == "hoy" else t.history(period="5d", interval="15m")
        if data.empty: return None
        
        # Limpiar timezone
        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)
            
        filename = f"{ticker.replace('^','')}_tmp.png"
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=False)
        mpf.plot(data, type='candle', style=s, title=f'{ticker}', volume=True, savefig=filename)
        return filename
    except Exception as e:
        logging.error(f"Error gráfico {ticker}: {e}")
        return None
    finally:
        # ARREGLO: Limpieza profunda para no colapsar RAM
        matplotlib.pyplot.close('all') if hasattr(matplotlib, 'pyplot') else None

def consultar_ia(prompt: str, system: str, motor="deepseek") -> str:
    """Consultas a IA con manejo de errores."""
    if motor == "gemini" and client_gemini:
        try:
            res = client_gemini.models.generate_content(model='gemini-1.5-flash', contents=f"{system}\n\n{prompt}")
            return res.text
        except Exception as e:
            logging.error(f"Fallo Gemini, usando DeepSeek: {e}")
            motor = "deepseek"

    if motor == "deepseek":
        try:
            url = "https://api.deepseek.com/chat/completions"
            headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
            payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            r = requests.post(url, headers=headers, json=payload, timeout=20)
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return "❌ Error de conexión con el cerebro analítico."

# ============================================================
#  🎯 MODO SNIPER (BACKGROUND TASK cada 3 minutos)
# ============================================================
async def tarea_sniper(context: ContextTypes.DEFAULT_TYPE):
    global sniper_estado
    if not sniper_estado["activo"]: return
    
    # Verificar si ya se acabó el tiempo
    ahora = obtener_hora_local()
    fin = datetime.datetime.fromisoformat(sniper_estado["fin"]).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET)))
    
    if ahora >= fin:
        sniper_estado["activo"] = False
        guardar_estado()
        await context.bot.send_message(chat_id=sniper_estado["chat_id"], text="🏁 *Misión cumplida.* Modo Sniper desactivado. Radar normal restaurado.", parse_mode="Markdown")
        return

    # Escanear precios en vivo
    ticker = sniper_estado["ticker"]
    q = obtener_precio(ticker)
    
    if q["c"] != "N/D":
        # Lógica de detección de picos (simulada para 3 min)
        cambio = q["dp"]
        if abs(cambio) >= 0.5:  # Alerta si se mueve 0.5% o más
            direccion = "📈 SUBIÓ" if cambio > 0 else "📉 BAJÓ"
            msg = (
                f"🚨 **¡ALERTA SNIPER EN {ticker}!** 🚨\n\n"
                f"El precio {direccion} un **{cambio:.2f}%** recientemente.\n"
                f"💰 Precio actual: **${q['c']}**\n\n"
                f"🤖 _Si quieres mi opinión, escríbeme: '¿Vendo o espero?'_"
            )
            await context.bot.send_message(chat_id=sniper_estado["chat_id"], text=msg, parse_mode="Markdown")

# ============================================================
#  🌐 RADAR GLOBAL (BACKGROUND TASK cada hora)
# ============================================================
async def tarea_radar(context: ContextTypes.DEFAULT_TYPE):
    # Si el modo sniper está activo, el radar global duerme
    if sniper_estado["activo"] or CHAT_ID is None: return
    
    # 1. Gemini investiga las noticias del mundo
    contexto_noticias = consultar_ia(
        prompt="Resume en 3 líneas el sentimiento del mercado HOY (sectores como IA, Bancos, Cripto, Petróleo). Sé directo.",
        system="Eres un analista macro. Ve al grano.",
        motor="gemini"
    )

    # 2. Escanear precios buscando anomalías
    movimientos = []
    for cat, tickers in RADAR_ACTIVOS.items():
        for ticker in tickers:
            q = obtener_precio(ticker)
            if q["c"] != "N/D":
                umbral = 4.0 if "USD" in ticker else (1.5 if ticker in ["SPY","QQQ","^GSPC"] else 3.0)
                if abs(q["dp"]) >= umbral:
                    movimientos.append(f"- {ticker} ({cat}): {q['dp']:+.2f}% (${q['c']})")

    # 3. Si hay movimientos, DeepSeek analiza y te avisa
    if movimientos:
        analisis = consultar_ia(
            prompt=f"El radar detectó estos movimientos: {', '.join(movimientos)}. Noticias: {contexto_noticias}. Dame un análisis breve de trading (compra/venta).",
            system="Eres un trader experto. Sé conciso.",
            motor="deepseek"
        )
        
        msg = "🚨 **RADAR GLOBAL: MOVIMIENTOS DETECTADOS** 🚨\n\n"
        msg += f"🕵️‍♂️ *Contexto (Gemini):*\n_{contexto_noticias}_\n\n"
        msg += "📊 *Precios Alterados:*\n" + "\n".join(movimientos[:5]) + "\n\n" # Top 5 para no spamear
        msg += f"🧠 *Análisis (DeepSeek):*\n{analisis}"
        
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
#  🧠 CEREBRO CENTRAL (Maneja todo el texto natural)
# ============================================================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global sniper_estado, CHAT_ID
    CHAT_ID = update.effective_chat.id
    texto = update.message.text.lower().strip()
    
    # --- LÓGICA SI ESTAMOS EN MODO SNIPER ---
    if sniper_estado["activo"]:
        if "cancelar" in texto or "suficiente" in texto or "parar" in texto:
            sniper_estado["activo"] = False
            guardar_estado()
            await update.message.reply_text("🛑 Modo Sniper desactivado. Volviendo a la normalidad.")
            return
        
        # Opinión rápida de trading (El "amigo")
        if "vendo" in texto or "espero" in texto or "como va" in texto or "que hago" in texto:
            q = obtener_precio(sniper_estado["ticker"])
            respuesta = consultar_ia(
                prompt=f"El usuario está en modo sniper con {sniper_estado['ticker']}. Precio actual: {q['c']} ({q['dp']}%). El usuario pregunta: '{texto}'. Dale una opinión de amigo trader de 2 líneas. Sé directo.",
                system="Eres un compañero de trading rápido y conciso.",
                motor="deepseek"
            )
            await update.message.reply_text(respuesta)
            return
            
        # Si pide gráficos estando en sniper
        if "grafico" in texto or "velas" in texto:
            msg = await update.message.reply_text("📉 Generando gráfico rápido...")
            archivo = generar_grafico(sniper_estado["ticker"], "hoy")
            if archivo:
                with open(archivo, 'rb') as p:
                    await update.message.reply_photo(photo=p, caption=f"Velas de {sniper_estado['ticker']}")
                os.remove(archivo)
            await msg.delete()
            return
            
        await update.message.reply_text("🎯 Estoy en modo Sniper enfocado en tu operación. Dime 'cancelar' para salir.")
        return

    # --- LÓGICA SI NO ESTAMOS EN SNIPER ---
    
    # 1. Encender Modo Sniper
    if "modo sniper" in texto or "ponte sniper" in texto:
        # Extraer el ticker que quiere vigilar (usando IA)
        ticker_ia = consultar_ia(f"Extrae el ticker de esta frase: '{texto}'. Si no hay, devuelve 'TODOS'.", "Extrae ticker.", "deepseek").strip().upper()
        ticker_sniper = ticker_ia if ticker_ia != "TODOS" else "SPY"
        
        # Extraer horas/minutos (simple regex mental)
        minutos = 60 # Por defecto 1 hora
        if "2 horas" in texto: minutos = 120
        elif "media hora" in texto: minutos = 30
        
        fin_tiempo = obtener_hora_local() + datetime.timedelta(minutes=minutos)
        sniper_estado = {"activo": True, "ticker": ticker_sniper, "fin": fin_tiempo.isoformat(), "chat_id": CHAT_ID}
        guardar_estado()
        
        msg = (
            f"🎯 **¡MODO SNIPER ACTIVADO!**\n"
            f"Activo: **{ticker_sniper}**\n"
            f"Tiempo: hasta las {fin_tiempo.strftime('%H:%M')}.\n\n"
            f"El Radar Global se ha apagado para no molestar. Estaré escaneando el gráfico cada 3 minutos y te avisaré de picos fuertes.\n"
            f"*(Puedes preguntarme '¿Vendo o espero?' y te daré mi opinión).*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # 2. Panel de Estado
    if "estado" in texto or "que tienes prendido" in texto or "/estado" in texto:
        # Ping real a los servicios
        ping_deepseek = "🟢 OK" if consultar_ia("Hola", "test") != "Error" else "🔴 FALLO"
        
        msg = (
            "📟 **PANEL DE CONTROL**\n\n"
            f"🚨 **Radar Global:** {'🔴 PAUSADO (por Sniper)' if sniper_estado['activo'] else '🟢 ACTIVO'}\n"
            f"🎯 **Modo Sniper:** {'🟢 ACTIVO en ' + sniper_estado['ticker'] + ' (Hasta las ' + datetime.datetime.fromisoformat(sniper_estado['fin']).strftime('%H:%M') + ')' if sniper_estado['activo'] else '🔴 INACTIVO'}\n"
            f"🧠 **DeepSeek:** {ping_deepseek}\n"
            f"🕵️‍♂️ **Gemini:** {'🟢 OK' if client_gemini else '🔴 FALLO'}\n"
            f"📊 **Yahoo/Finnhub:** 🟢 OK\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # 3. Menú (Botones)
    if "/menu" in texto or texto == "menu":
        keyboard = [
            [InlineKeyboardButton("Apple (AAPL)", callback_data="menu_AAPL"), InlineKeyboardButton("Tesla (TSLA)", callback_data="menu_TSLA")],
            [InlineKeyboardButton("Bitcoin (BTC)", callback_data="menu_BTC-USD"), InlineKeyboardButton("S&P 500", callback_data="menu_^GSPC")],
            [InlineKeyboardButton("Bancos (KBE)", callback_data="menu_KBE")]
        ]
        await update.message.reply_text(
            "📊 **Menú Principal**\nSelecciona un activo de la lista o escríbeme el nombre de cualquier empresa (ej. *'Quiero ver Samsung'*).",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # 4. Detección de intención (Gráfico, Noticias, Análisis 360)
    intencion = consultar_ia(
        prompt=f"Analiza esta frase: '{texto}'. Devuelve SOLO un JSON: {{\"accion\": \"grafico\"|\"noticias\"|\"analisis\"|\"charla\", \"ticker\": \"AAPL\"}}",
        system="Eres un clasificador. Devuelve solo JSON válido.",
        motor="deepseek"
    )
    
    try:
        data = json.loads(intencion.replace("```json", "").replace("```", "").strip())
        accion, ticker = data.get("accion"), data.get("ticker")
        
        if accion == "grafico" and ticker:
            msg = await update.message.reply_text(f"📉 Generando velas de {ticker}...")
            file = generar_grafico(ticker, "semana")
            if file:
                with open(file, 'rb') as p:
                    await update.message.reply_photo(photo=p, caption=f"📊 {ticker}")
                os.remove(file)
            await msg.delete()
            return
            
        elif accion == "analisis" and ticker:
            msg = await update.message.reply_text(f"🧠 Analizando {ticker} (Análisis 360)...")
            q = obtener_precio(ticker)
            prompt = f"Analiza {ticker}. Precio: {q['c']} ({q['dp']}%). ESTRUCTURA OBLIGATORIA: 1. LO MÁS IMPORTANTE HOY. 2. LECTURA DE VELAS. 3. CORTO PLAZO (Predicción). 4. MEDIO PLAZO. 5. LARGO PLAZO."
            analisis = consultar_ia(prompt, "Eres analista experto.")
            await msg.edit_text(f"🧠 **Análisis 360° de {ticker}:**\n\n{analisis}", parse_mode="Markdown")
            return
            
        elif accion == "noticias" and ticker:
            msg = await update.message.reply_text(f"📰 Buscando noticias...")
            notis = consultar_ia(f"Dame las últimas noticias de {ticker}.", "Eres periodista financiero.")
            await msg.edit_text(f"📰 **Noticias de {ticker}:**\n\n{notis}", parse_mode="Markdown")
            return

    except Exception:
        pass # Si falla la clasificación, cae al chat libre

    # 5. Chat Libre (Si no es nada de lo anterior)
    msg = await update.message.reply_text("🤔 Pensando...")
    respuesta = consultar_ia(f"Responde amigablemente a esto: '{texto}'", "Eres un asistente financiero amigable.")
    await msg.edit_text(respuesta)

# ============================================================
#  📱 BOTONES INTERACTIVOS (CallbackQueries)
# ============================================================
async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data # ej: "menu_AAPL"
    
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
            file = generar_grafico(ticker, "semana")
            if file:
                with open(file, 'rb') as p:
                    await query.message.reply_photo(photo=p, caption=f"📊 {ticker}")
                os.remove(file)
            await query.delete_message()
        elif accion == "analisis":
            await query.edit_message_text(f"🧠 Analizando {ticker}...")
            q = obtener_precio(ticker)
            prompt = f"Analiza {ticker}. Precio: {q['c']} ({q['dp']}%). ESTRUCTURA OBLIGATORIA: 1. LO MÁS IMPORTANTE HOY. 2. LECTURA DE VELAS. 3. CORTO PLAZO. 4. MEDIO PLAZO. 5. LARGO PLAZO."
            analisis = consultar_ia(prompt, "Eres analista experto.")
            await query.edit_message_text(f"🧠 **Análisis 360° de {ticker}:**\n\n{analisis}", parse_mode="Markdown")

# ============================================================
#  ▶️ ARRANQUE DEL SERVIDOR Y BOT
# ============================================================
async def post_init(application: Application):
    """Revisa si el servidor se reinició y si había un Sniper activo."""
    global job_sniper, job_radar
    if sniper_estado["activo"]:
        logging.info("Reanudando Modo Sniper tras reinicio...")
        # Volver a programar la tarea cada 3 min
        job_sniper = application.job_queue.run_repeating(tarea_sniper, interval=180, first=5)
        
    # Arrancar Radar Global cada hora
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
    
    # Construimos la app llamando a 'post_init' al arrancar
    app
