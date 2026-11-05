# ============================================================
#  🤖 BOT FINANCIERO — VERSIÓN FINAL (GRÁFICOS Y DATOS BLINDADOS)
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
ZONA_HORARIA_OFFSET = -6 

def obtener_hora_local():
    tz = datetime.timezone(datetime.timedelta(hours=ZONA_HORARIA_OFFSET))
    return datetime.datetime.now(tz)

CHAT_ID = None
job_vigilancia = None
job_sniper = None

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ============================================================
#  📊 FUNCIONES DE DATOS (BLINDADAS CONTRA ERRORES)
# ============================================================
def obtener_precio(ticker: str) -> dict:
    # 1. Intentar con Yahoo Finance
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if not hist.empty:
            precio = hist['Close'].iloc[-1]
            if not math.isnan(precio): # Verificar que no sea NaN
                if len(hist) >= 2:
                    prev = hist['Close'].iloc[-2]
                    cambio = ((precio - prev) / prev) * 100
                    return {"c": round(float(precio), 2), "dp": round(float(cambio), 2)}
                else:
                    return {"c": round(float(precio), 2), "dp": 0.0}
    except Exception as e:
        logging.error(f"Error YFinance {ticker}: {e}")

    # 2. Intentar con Finnhub como respaldo
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("c") and data.get("c") > 0: # Si Finnhub devuelve un precio real
            return data
    except Exception as e:
        logging.error(f"Error Finnhub {ticker}: {e}")
    
    # 3. Si todo falla, devolver N/D
    return {"c": "N/D", "dp": 0.0}

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
        return ["No se pudieron cargar noticias."]

# ============================================================
#  📉 GENERADOR DE VELAS JAPONESAS (ARREGLADO)
# ============================================================
def generar_grafico_velas(ticker: str) -> str:
    try:
        # Usamos Ticker().history() en lugar de download() porque es más estable en servidores
        t = yf.Ticker(ticker)
        data = t.history(period="5d", interval="15m")
        
        # Si no hay datos (fin de semana o feriado), intentamos con datos diarios
        if data.empty:
            data = t.history(period="1mo", interval="1d")
            
        if data.empty: 
            return None
        
        # ¡EL TRUCO! Quitamos la zona horaria del índice (mplfinance suele fallar por esto)
        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)
        
        filename = f"{ticker}_chart.png"
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=False)
        
        mpf.plot(data, type='candle', style=s, title=f'{ticker} - Velas Japonesas', 
                 volume=True, savefig=filename)
        return filename
    except Exception as e:
        logging.error(f"Error gráfico {ticker}: {e}")
        return None

# ============================================================
#  🧠 FUNCIONES DE IA
# ============================================================
def consultar_deepseek(prompt: str, system_prompt: str = "Eres un analista financiero experto y amigable. Responde en español.") -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY_DEEPSEEK}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ============================================================
#  🎯 TAREA DEL MODO SNIPER
# ============================================================
async def tarea_sniper(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    tickers = job.data["tickers"]
    
    for ticker in tickers:
        q = obtener_precio(ticker)
        precio_actual = q.get("c")
        if precio_actual == "N/D": continue
        
        precio_anterior = context.bot_data.get(f"precio_{ticker}", precio_actual)
        
        if precio_anterior != precio_actual:
            cambio_rapido = ((precio_actual - precio_anterior) / precio_anterior) * 100
            if abs(cambio_rapido) >= 0.5:
                direccion = "📈 SUBIÓ" if cambio_rapido > 0 else "📉 BAJÓ"
                mensaje = f"🚨 *¡Alerta Sniper en {ticker}!*\nEl precio {direccion} un *{cambio_rapido:.2f}%* en los últimos 3 min.\n💰 Precio: ${precio_actual}"
                await context.bot.send_message(chat_id=chat_id, text=mensaje, parse_mode="Markdown")
        context.bot_data[f"precio_{ticker}"] = precio_actual

# ============================================================
#  🧠 EL CEREBRO CENTRAL
# ============================================================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_sniper
    texto = update.message.text
    if texto.startswith('/'):
        texto = texto.split(' ', 1)[1] if ' ' in texto else texto.replace('/', '')
    if not texto.strip(): return

    mensaje_espera = await update.message.reply_text("🤔 Analizando...")

    system_prompt_clasificador = (
        "Eres un clasificador de intenciones. Devuelve ÚNICAMENTE un objeto JSON válido. "
        '{"accion": "noticias" | "precio" | "analisis_corto" | "analisis_largo" | "hora" | "reporte" | "grafico" | "sniper" | "cancelar_sniper" | "charla", '
        '"ticker": "AAPL" o "null", "hora_texto": "19:30" o "null", "duracion_minutos": 120 o null}\n'
        "REGLAS ESTRICTAS:\n"
        "1. Traduce empresas a tickers (Samsung -> SSNLF, Apple -> AAPL).\n"
        "2. PARA LA HORA (hora_texto): CONVIERTE SIEMPRE A FORMATO DE 24 HORAS. Si el usuario dice '7:30 de la tarde' o '7:30 pm', devuelve '19:30'. Si dice '9 am', devuelve '09:00'.\n"
        "- 'grafico': SI DICE 'GRÁFICO', 'VELAS', 'IMAGEN', 'CHART'.\n"
        "- 'sniper': si pide modo alerta o vigilancia (extrae 'duracion_minutos').\n"
        "- 'cancelar_sniper': si pide apagar alerta.\n"
        "- 'analisis_corto': trading.\n"
        "- 'analisis_largo': inversión.\n"
        "- 'precio': general.\n"
        "- 'noticias': novedades.\n"
        "- 'hora': hora actual.\n"
        "- 'reporte': aviso a hora específica."
    )

    try:
        respuesta_json_str = consultar_deepseek(texto, system_prompt_clasificador)
        respuesta_json_str = respuesta_json_str.replace("```json", "").replace("```", "").strip()
        clasificacion = json.loads(respuesta_json_str)
        
        accion = clasificacion.get("accion")
        ticker = clasificacion.get("ticker")
        hora_texto = clasificacion.get("hora_texto")
        duracion_minutos = clasificacion.get("duracion_minutos")

        if accion == "sniper" and duracion_minutos:
            if job_sniper: job_sniper.schedule_removal()
            tickers_a_vigilar = [ticker] if ticker and ticker != "null" else TICKERS_VIGILAR
            tiempo_fin = obtener_hora_local() + datetime.timedelta(minutes=duracion_minutos)
            job_sniper = context.job_queue.run_repeating(
                tarea_sniper, interval=180, first=10,
                data={"chat_id": update.effective_chat.id, "tickers": tickers_a_vigilar}, last=tiempo_fin
            )
            await mensaje_espera.delete()
            await update.message.reply_text(f"🎯 *¡Modo Sniper Activado!*\nVigilando: {', '.join(tickers_a_vigilar)}\nTiempo límite: {duracion_minutos} min (hasta las {tiempo_fin.strftime('%I:%M %p')}).", parse_mode="Markdown")
            return

        elif accion == "cancelar_sniper":
            if job_sniper:
                job_sniper.schedule_removal()
                job_sniper = None
                await mensaje_espera.delete()
                await update.message.reply_text("🛑 Modo Sniper desactivado.")
            return

        elif accion == "hora":
            ahora = obtener_hora_local().strftime("%I:%M %p")
            await mensaje_espera.delete()
            await update.message.reply_text(f"🕒 La hora actual es: {ahora} (Hora Central EE.UU.).")
            return

        elif accion == "reporte" and hora_texto:
            try:
                if ":" in hora_texto:
                    h_str, m_str = hora_texto.split(":")
                    h, m = int(h_str), int(m_str)
                else: raise ValueError
                
                ahora = obtener_hora_local()
                objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
                if objetivo < ahora: objetivo += datetime.timedelta(days=1)
                
                espera = (objetivo - ahora).total_seconds()
                context.job_queue.run_once(tarea_reporte_agendado, when=espera, data={"chat_id": update.effective_chat.id, "ticker": ticker})
                
                await mensaje_espera.delete()
                await update.message.reply_text(f"✅ ¡Entendido! Te mandaré el reporte a las {objetivo.strftime('%I:%M %p')}.")
            except Exception:
                await mensaje_espera.edit_text("❌ No entendí la hora. Intenta decir 'Avísame a las 19:30'.")
            return

        elif accion == "grafico" and ticker and ticker != "null":
            await mensaje_espera.edit_text(f"📉 Generando velas japonesas de {ticker}...")
            archivo_grafico = generar_grafico_velas(ticker)
            if archivo_grafico:
                await mensaje_espera.delete()
                with open(archivo_grafico, 'rb') as photo:
                    await update.message.reply_photo(photo=photo, caption=f"📊 Velas japonesas de {ticker}.")
                os.remove(archivo_grafico)
            else:
                await mensaje_espera.edit_text("❌ No pude generar el gráfico. Es posible que el mercado esté cerrado o no haya datos disponibles para este ticker.")
            return

        elif accion == "noticias" and ticker and ticker != "null":
            notis = finnhub_noticias(ticker, 3)
            if notis and "No se pudieron cargar" not in notis[0]:
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker}:*\n\n" + "\n".join(notis), parse_mode="Markdown")
                return
            else:
                resumen = consultar_deepseek(f"Resumen de noticias de {ticker}.")
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker} (IA):*\n\n{resumen}", parse_mode="Markdown")
                return

        elif accion in ["precio", "analisis_corto", "analisis_largo"] and ticker and ticker != "null":
            q = obtener_precio(ticker)
            
            # Arreglo para el NaN
            if q.get('c') == "N/D" or isinstance(q.get('c'), float) and math.isnan(q.get('c')):
                await mensaje_espera.edit_text(f"❌ Lo siento, no hay datos de precio disponibles para {ticker} en este momento (puede ser que el mercado esté cerrado).")
                return

            notis = finnhub_noticias(ticker)
            prompt = f"Analiza {ticker} a {accion.replace('analisis_', '')} plazo. Precio: {q.get('c')}, Cambio: {q.get('dp')}%. Noticias: {', '.join(notis)}."
            analisis = consultar_deepseek(prompt)
            await mensaje_espera.delete()
            await update.message.reply_text(f"📊 *Análisis de {ticker}:*\n\n{analisis}", parse_mode="Markdown")
            return

        respuesta = consultar_deepseek(f"Responde amigablemente a esto: '{texto}'")
        await mensaje_espera.delete()
        await update.message.reply_text(respuesta)

    except Exception as e:
        logging.error(f"Error en procesar_mensaje: {e}")
        await mensaje_espera.edit_text("❌ Tuve un problema al procesar tu mensaje.")

# ============================================================
#  🤖 TAREAS Y COMANDOS BÁSICOS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ¡Hola! Prueba con:\n"
        "👉 _\"Gráfico de velas de Apple\"_\n"
        "👉 _\"Análisis a corto plazo de Tesla\"_\n"
        "👉 _\"Avísame de NVDA a las 19:30\"_\n"
        "👉 _\"Ponte alerta por 2 horas\"_",
        parse_mode="Markdown"
    )

async def tarea_reporte_agendado(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    ticker = job.data.get("ticker")
    
    if ticker and ticker != "null":
        q = obtener_precio(ticker)
        if q.get('c') == "N/D" or isinstance(q.get('c'), float) and math.isnan(q.get('c')):
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ *Reporte de {ticker}:* ❌ Datos no disponibles en este momento.", parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ *Reporte programado de {ticker}:*\n📈 {ticker}: ${q.get('c')} ({q.get('dp'):+.2f}%)", parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text="⏰ *Reporte General:*", parse_mode="Markdown")
        for t in TICKERS_VIGILAR:
            q = obtener_precio(t)
            if q.get('c') != "N/D" and not math.isnan(q.get('c', 0)):
                await context.bot.send_message(chat_id=chat_id, text=f"📈 {t}: ${q.get('c')} ({q.get('dp')}%)")

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
        q = obtener_precio(t)
        if q.get('c') != "N/D" and not math.isnan(q.get('c', 0)):
            cambio = q.get("dp", 0)
            if cambio >= UMBRAL_CAMBIO_PCT: alertas.append(f"🔴 *VENDER* {t} — subió {cambio:.2f}%")
            elif cambio <= -UMBRAL_CAMBIO_PCT: alertas.append(f"🟢 *COMPRAR* {t} — cayó {cambio:.2f}%")
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
