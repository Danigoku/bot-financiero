# ============================================================
#  🤖 BOT FINANCIERO — 0.1
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import datetime
import requests
import os
import json
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
job_sniper = None # Variable para controlar el Modo Sniper

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ============================================================
#  📊 FUNCIONES DE DATOS (Yahoo + Finnhub)
# ============================================================
def obtener_precio(ticker: str) -> dict:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if len(hist) >= 2:
            precio = hist['Close'].iloc[-1]
            prev = hist['Close'].iloc[-2]
            cambio = ((precio - prev) / prev) * 100
            return {"c": round(float(precio), 2), "dp": round(float(cambio), 2)}
    except Exception as e:
        logging.error(f"Error YFinance {ticker}: {e}")

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Error Finnhub {ticker}: {e}")
        return {"c": "N/A", "dp": 0}

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
#  📉 GENERADOR DE VELAS JAPONESAS
# ============================================================
def generar_grafico_velas(ticker: str) -> str:
    try:
        data = yf.download(ticker, period="5d", interval="15m", progress=False)
        if data.empty: return None
        
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
#  🎯 TAREA DEL MODO SNIPER (Cada 3 minutos)
# ============================================================
async def tarea_sniper(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    tickers = job.data["tickers"]
    
    for ticker in tickers:
        q = obtener_precio(ticker)
        precio_actual = q.get("c")
        
        if precio_actual == "N/A": continue

        # Obtenemos el precio anterior guardado en la memoria del bot
        precio_anterior = context.bot_data.get(f"precio_{ticker}", precio_actual)
        
        # Calculamos la diferencia en estos 3 minutos
        if precio_anterior != precio_actual:
            cambio_rapido = ((precio_actual - precio_anterior) / precio_anterior) * 100
            # Si el cambio es mayor al 0.5% (puedes ajustarlo aquí si quieres)
            if abs(cambio_rapido) >= 0.5:
                direccion = "📈 SUBIÓ" if cambio_rapido > 0 else "📉 BAJÓ"
                mensaje = (
                    f"🚨 *¡Alerta Sniper en {ticker}!*\n\n"
                    f"El precio {direccion} un *{cambio_rapido:.2f}%* en los últimos 3 minutos.\n"
                    f"💰 Precio actual: ${precio_actual}\n\n"
                    f"🤖 _La IA dice: Podría ser un movimiento importante, revisa el gráfico con /grafico {ticker}_"
                )
                await context.bot.send_message(chat_id=chat_id, text=mensaje, parse_mode="Markdown")
                
        # Actualizamos el precio en memoria para la próxima revisión
        context.bot_data[f"precio_{ticker}"] = precio_actual

# ============================================================
#  🧠 EL CEREBRO CENTRAL
# ============================================================
async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global job_sniper
    texto = update.message.text
    if texto.startswith('/'):
        texto = texto.split(' ', 1)[1] if ' ' in texto else texto.replace('/', '')
    
    if not texto.strip():
        await update.message.reply_text("⚠️ No entendí tu mensaje.")
        return

    mensaje_espera = await update.message.reply_text("🤔 Analizando...")

    system_prompt_clasificador = (
        "Eres un clasificador de intenciones. Devuelve ÚNICAMENTE un objeto JSON válido. "
        '{"accion": "noticias" | "precio" | "analisis_corto" | "analisis_largo" | "hora" | "reporte" | "grafico" | "sniper" | "cancelar_sniper" | "charla", '
        '"ticker": "AAPL" o "null", "hora_texto": "15:40" o "null", "duracion_minutos": 120 o null}\n'
        "REGLAS:\n"
        "1. Traduce empresas a tickers (Samsung -> SSNLF, Toyota -> TM).\n"
        "- 'sniper': si pide modo alerta, sniper, vigilancia extrema por un tiempo específico. Extrae los minutos en 'duracion_minutos' (ej. 2 horas = 120). Si no menciona ticker, pon 'null' para vigilar todas.\n"
        "- 'cancelar_sniper': si pide apagar la alerta o detener el modo trading.\n"
        "- 'analisis_corto': trading, corto plazo.\n"
        "- 'analisis_largo': inversión, largo plazo.\n"
        "- 'precio': precio general.\n"
        "- 'noticias': novedades.\n"
        "- 'hora': hora actual.\n"
        "- 'reporte': aviso a hora específica.\n"
        "- 'grafico': velas japonesas.\n"
        "- 'charla': general."
    )

    try:
        respuesta_json_str = consultar_deepseek(texto, system_prompt_clasificador)
        respuesta_json_str = respuesta_json_str.replace("```json", "").replace("```", "").strip()
        clasificacion = json.loads(respuesta_json_str)
        
        accion = clasificacion.get("accion")
        ticker = clasificacion.get("ticker")
        hora_texto = clasificacion.get("hora_texto")
        duracion_minutos = clasificacion.get("duracion_minutos")

        # --- LÓGICA DEL MODO SNIPER ---
        if accion == "sniper" and duracion_minutos:
            # Si ya había uno activo, lo cancelamos
            if job_sniper:
                job_sniper.schedule_removal()
            
            # Definir qué acciones vigilar
            tickers_a_vigilar = [ticker] if ticker and ticker != "null" else TICKERS_VIGILAR
            
            # Calcular a qué hora termina
            tiempo_fin = obtener_hora_local() + datetime.timedelta(minutes=duracion_minutos)
            fin_str = tiempo_fin.strftime("%I:%M %p")
            
            # Arrancar el job cada 3 minutos
            job_sniper = context.job_queue.run_repeating(
                tarea_sniper, 
                interval=180, # 180 segundos = 3 minutos
                first=10, 
                data={"chat_id": update.effective_chat.id, "tickers": tickers_a_vigilar},
                last=tiempo_fin # Se apaga automáticamente a la hora indicada
            )
            
            tickers_msg = ", ".join(tickers_a_vigilar)
            await mensaje_espera.delete()
            await update.message.reply_text(
                f"🎯 *¡Modo Sniper Activado!*\n\n"
                f"Vigilando: {tickers_msg}\n"
                f"Revisando cada: 3 minutos\n"
                f"Tiempo límite: {duracion_minutos} minutos (hasta las {fin_str})\n\n"
                f"Te avisaré si hay picos fuertes. Di 'cancela' si quieres detenerlo antes.",
                parse_mode="Markdown"
            )
            return

        elif accion == "cancelar_sniper":
            if job_sniper:
                job_sniper.schedule_removal()
                job_sniper = None
                await mensaje_espera.delete()
                await update.message.reply_text("🛑 Modo Sniper desactivado. Vuelvo a la normalidad.")
            else:
                await mensaje_espera.delete()
                await update.message.reply_text("ℹ️ No hay ningún Modo Sniper activo en este momento.")
            return

        # --- RESTO DE ACCIONES ---
        elif accion == "hora":
            ahora = obtener_hora_local().strftime("%I:%M %p")
            await mensaje_espera.delete()
            await update.message.reply_text(f"🕒 La hora actual es: {ahora} (Hora Central EE.UU.).")
            return

        elif accion == "reporte" and hora_texto:
            try:
                h, m = map(int, hora_texto.split(":"))
                ahora = obtener_hora_local()
                objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
                if objetivo < ahora: objetivo += datetime.timedelta(days=1)
                
                espera = (objetivo - ahora).total_seconds()
                context.job_queue.run_once(
                    tarea_reporte_agendado, 
                    when=espera, 
                    data={"chat_id": update.effective_chat.id, "ticker": ticker}
                )
                await mensaje_espera.delete()
                ticker_msg = f"de {ticker}" if ticker and ticker != "null" else "general"
                await update.message.reply_text(f"✅ ¡Entendido! Te mandaré el reporte {ticker_msg} a las {hora_texto}.")
            except ValueError:
                await mensaje_espera.edit_text("❌ No entendí la hora. Intenta decir 'Avísame a las 15:40'.")
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
                await mensaje_espera.edit_text("❌ No pude generar el gráfico. Verifica el ticker.")
            return

        elif accion == "noticias" and ticker and ticker != "null":
            notis = finnhub_noticias(ticker, 3)
            if notis and "No se pudieron cargar" not in notis[0]:
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker}:*\n\n" + "\n".join(notis), parse_mode="Markdown")
                return
            else:
                prompt_resumen = f"Dame un resumen breve de las noticias de {ticker}."
                resumen = consultar_deepseek(prompt_resumen)
                await mensaje_espera.delete()
                await update.message.reply_text(f"📰 *Noticias de {ticker} (IA):*\n\n{resumen}", parse_mode="Markdown")
                return

        elif accion in ["precio", "analisis_corto", "analisis_largo"] and ticker and ticker != "null":
            q = obtener_precio(ticker)
            notis = finnhub_noticias(ticker)
            
            if accion == "analisis_corto":
                prompt = f"Actúa como un trader experto. Analiza {ticker} a CORTO PLAZO. Precio: {q.get('c')}, Cambio: {q.get('dp')}%. Noticias: {', '.join(notis)}. Dime si es buen momento para entrar y salir rápido."
                titulo = f"⚡ *Trading a Corto Plazo: {ticker}*"
            elif accion == "analisis_largo":
                prompt = f"Actúa como un inversor de valor. Analiza {ticker} a LARGO PLAZO. Precio: {q.get('c')}. Noticias: {', '.join(notis)}. Ignora la volatilidad diaria. Dime si es sólida para GUARDAR por años."
                titulo = f"🏦 *Inversión a Largo Plazo: {ticker}*"
            else:
                prompt = f"Analiza la acción {ticker}. Precio: {q.get('c')}, Cambio: {q.get('dp')}%. Noticias: {', '.join(notis)}. Da análisis de 1. Corto Plazo y 2. Largo Plazo."
                titulo = f"📊 *Análisis General: {ticker}*"

            analisis = consultar_deepseek(prompt)
            await mensaje_espera.delete()
            await update.message.reply_text(f"{titulo}\n\n{analisis}", parse_mode="Markdown")
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
#  🤖 TAREAS Y COMANDOS BÁSICOS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 ¡Hola! Soy tu asistente financiero.\n\n"
        "👉 _\"Análisis a corto plazo de Tesla\"_\n"
        "👉 _\"Dame el gráfico de velas de Apple\"_\n"
        "👉 _\"Ponte alerta por 3 horas\"_ (Modo Sniper)\n\n"
        "Comandos:\n"
        "• /listo → Vigilancia cada hora.\n"
        "• /parar → Detener vigilancia.",
        parse_mode="Markdown"
    )

async def tarea_reporte_agendado(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    ticker = job.data.get("ticker")
    
    if ticker and ticker != "null":
        await context.bot.send_message(chat_id=chat_id, text=f"📊 *Reporte de {ticker}:*", parse_mode="Markdown")
        try:
            q = obtener_precio(ticker)
            await context.bot.send_message(chat_id=chat_id, text=f"📈 {ticker}: ${q.get('c')} ({q.get('dp')}%)")
        except:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Error al buscar {ticker}")
    else:
        await context.bot.send_message(chat_id=chat_id, text="📊 *Reporte General:*", parse_mode="Markdown")
        for t in TICKERS_VIGILAR:
            try:
                q = obtener_precio(t)
                await context.bot.send_message(chat_id=chat_id, text=f"📈 {t}: ${q.get('c')} ({q.get('dp')}%)")
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
            q = obtener_precio(t)
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
