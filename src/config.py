"""
config.py — Configuración general del bot: conexión a Slack, IDs de Sheets y canales,
lista de cobradores, y la conexión a Google Sheets que se comparte entre /buscar-cliente
y el Radar de Promesas. TODOS los demás archivos importan 'app' desde aquí — por eso
este archivo se importa PRIMERO, antes que cualquier otro módulo del bot.
"""
import os
import json
import gspread
from concurrent.futures import ThreadPoolExecutor
from slack_bolt import App
from google.oauth2.service_account import Credentials
from validaciones import _normalizar_encabezado

# Slack Bolt ejecuta TODOS los comandos/acciones/formularios del bot (de /cobro, /contactar,
# los botones de aprobar/rechazar, etc.) sobre un mismo grupo compartido de hilos. Por defecto
# ese grupo es de solo 5 hilos — si varios de esos hilos quedan ocupados a la vez esperando
# (por ejemplo, reintentando una operación de Google Sheets que tardó por una cuota excedida),
# un comando nuevo que llega puede quedarse "en fila" varios segundos antes de que le toque su
# turno. Eso es un problema para /contactar, /cobro, etc. porque abren un modal usando un
# 'trigger_id' que Slack solo deja usar dentro de los primeros ~3 segundos — si el comando
# se queda esperando turno más que eso, el modal falla con un error como 'fatal_error' o
# 'expired_trigger_id'. Con más hilos disponibles, es mucho menos probable que un comando se
# quede esperando tanto tiempo.
app = App(token=os.environ["SLACK_BOT_TOKEN"], listener_executor=ThreadPoolExecutor(max_workers=30))



# ============ CONFIGURACIÓN GENERAL (Sheets, Canales y otros IDs de Slack) ============
# El Sheet principal de Cobros usa la variable de entorno SHEET_ID (puesta en Railway).
SHEET_ID_COBRO2 = "1KbWx1d5ujGmNwjGbdb-c_QAwiEkxJpxLb1BOFOCY9QM"          # Call Center Seguros
SHEET_ID_LIQUIDACIONES = "1MYKQ-CnyMQBTEZcSBIXt-KDsBbfJt-tUmG-k5aZvDI0"   # Liquidaciones (Lista VIP)
SHEET_ID_COMERCIAL = "1Zayi6aQPoSjDadbAozhLGJaO7dU-6p51dQ5SXnaU6mc"      # Equipo Comercial
SHEET_ID_LEGAL = "1Zayi6aQPoSjDadbAozhLGJaO7dU-6p51dQ5SXnaU6mc"          # Equipo Legal (mismo Sheet que Comercial)
SHEET_ID_ESCALADOS = "1Zayi6aQPoSjDadbAozhLGJaO7dU-6p51dQ5SXnaU6mc"      # Clientes Escalados (mismo Sheet que Comercial/Legal)
SHEET_ID_MERCADEO = "1BbSiDUmgQZ0B0myvLv_N4tPPe0nnKvzl4jJerxEgv9U"       # Mercadeo (Conciliación de Pagos e Incidencias Técnicas)

CANAL_LIQUIDACIONES = "C0BE1HLRV1R"
CANAL_COMERCIAL = "C0BE5LJL729"
CANAL_LEGAL = "C0BJYNVG5PW"
CANAL_ESCALADOS = "C0BK1FFH5M3"
CANAL_SEGUIMIENTO = "C0BJWPMA3NF"          # Radar de promesas (4 PM)
CANAL_CIERRE = "#cobranzas-log"           # Cierre diario de cobros (6 PM)
CANAL_MERCADEO_PAGOS = "C0BNMAXSLKW"
CANAL_MERCADEO_INCIDENCIAS = "C0BN27H0N31"
CANAL_REPORTES_MENSUALES = "C0BQY0CQ4GG"  # canal para gerencia: los 5 reportes mensuales juntos

SUPERVISOR_ID = "U0B51AREWDU"  # Leandro Quoota (escalamiento del Radar de promesas)

# Nombres de pestañas dentro del Sheet principal de Cobros (SHEET_ID)
PESTANA_INDICADORES = "Indicadores"           # Tasa del día (vigente) — B20=valor, C20=fecha
PESTANA_HISTORIAL_TASAS = "Historial Tasas"   # Historial de tasas por fecha (columnas: Fecha, Tasa)
# ============ FIN CONFIGURACIÓN GENERAL ============

COBRADORES = ["DIEGO", "IARA", "REBECA", "MARIANGEL", "LUISMAR", "ANGELY", "DANIEL", "BARBARA", "MARIANA", "ANDRES", "NELMAYRI", "ALEJANDRO", "ISAAC", "VALENTINA"]


def _opciones_cobradores():
    return [{"text": {"type": "plain_text", "text": c}, "value": c} for c in COBRADORES]



# ============ CONEXIÓN COMPARTIDA A GOOGLE SHEETS (se arma UNA sola vez) ============
# Antes, cada función que necesitaba leer/escribir en Sheets armaba su propia conexión
# desde cero (leer las credenciales, autenticarse con Google) en cada llamada — eso es la
# parte lenta de hablar con Sheets, mucho más que la lectura/escritura en sí. Con esto se
# arma una sola vez (la primera vez que alguien la pide) y se reutiliza siempre — es seguro
# dejarla en memoria así de indefinido: el token interno de Google se refresca solo cuando
# hace falta, no se "vence" por tenerla cacheada. Todas las funciones "_abrir_hoja_*" del
# bot (en cobros.py, mercadeo.py, reportes.py) usan esta misma función en vez de armar su
# propia conexión — así todo el bot comparte una sola conexión real a Google Sheets.
_CLIENTE_SHEETS_CACHEADO = None


def get_cliente_busqueda():
    global _CLIENTE_SHEETS_CACHEADO
    if _CLIENTE_SHEETS_CACHEADO is not None:
        return _CLIENTE_SHEETS_CACHEADO
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    _CLIENTE_SHEETS_CACHEADO = gspread.authorize(creds)
    return _CLIENTE_SHEETS_CACHEADO
# ============ FIN CONEXIÓN COMPARTIDA A GOOGLE SHEETS ============


# ============ PESTAÑAS CACHEADAS (para no gastar cuota de lectura de más) ============
# Aunque ya se comparte la conexión de arriba, cada '_abrir_hoja_*' del bot todavía le
# pedía a Google, EN CADA LLAMADA: "abre este archivo por su ID" + "dame la lista de sus
# pestañas" — dos lecturas más a la cuota, solo para encontrar la pestaña correcta, antes
# de siquiera leer un dato real. Como la lista de pestañas de un Sheet casi nunca cambia
# mientras el bot está corriendo, se puede cachear el resultado de "encontrar la pestaña X
# del archivo Y" para siempre (dentro de este proceso) — la PRIMERA vez que se pide, sí se
# consulta a Google; de ahí en adelante se reutiliza el mismo objeto. Esto NO cachea los
# DATOS de la pestaña (get_all_values() sigue siendo siempre una lectura fresca y real) —
# solo evita repetir la búsqueda de "¿cuál pestaña es esta?" una y otra vez.
#
# Esta caché fue clave para resolver los errores '429 Quota exceeded' que empezaron a
# aparecer en producción: entre la conexión (ya cacheada) y esto, cada operación del bot
# pasó de costar 3-4 lecturas de cuota a costar 1 sola (la lectura real de datos).
_HOJAS_CACHEADAS = {}


def abrir_pestana_cacheada(spreadsheet_id, nombre_pestana):
    """Abre la pestaña 'nombre_pestana' del Sheet 'spreadsheet_id', comparando el nombre
    ignorando mayúsculas/tildes/espacios de más (igual que el resto del bot). Cachea el
    resultado para siempre (por este proceso) — llamadas siguientes con la MISMA
    combinación (spreadsheet_id, nombre_pestana) no vuelven a gastar cuota de lectura
    buscando la pestaña. Devuelve None si no la encuentra o si algo falla (nunca lanza
    error)."""
    clave = (spreadsheet_id, _normalizar_encabezado(nombre_pestana))
    if clave in _HOJAS_CACHEADAS:
        return _HOJAS_CACHEADAS[clave]
    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(spreadsheet_id)
        objetivo = _normalizar_encabezado(nombre_pestana)
        for ws in spreadsheet.worksheets():
            if _normalizar_encabezado(ws.title) == objetivo:
                _HOJAS_CACHEADAS[clave] = ws
                return ws
    except Exception as e:
        print(f"⚠️ No se pudo abrir la pestaña '{nombre_pestana}': {type(e).__name__}: {e}")
    return None


def guardar_pestana_en_cache(spreadsheet_id, nombre_pestana, ws):
    """Guarda manualmente un objeto de pestaña ya abierto (o recién CREADO, ej. cuando
    'Metricas Actividad' no existía todavía) en la misma caché de abrir_pestana_cacheada —
    así las próximas llamadas la reutilizan también, sin tener que volver a buscarla."""
    clave = (spreadsheet_id, _normalizar_encabezado(nombre_pestana))
    _HOJAS_CACHEADAS[clave] = ws
# ============ FIN PESTAÑAS CACHEADAS ============
