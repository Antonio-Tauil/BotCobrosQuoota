"""
config.py — Configuración general del bot: conexión a Slack, IDs de Sheets y canales,
lista de cobradores, y la conexión a Google Sheets que se comparte entre /buscar-cliente
y el Radar de Promesas. TODOS los demás archivos importan 'app' desde aquí — por eso
este archivo se importa PRIMERO, antes que cualquier otro módulo del bot.
"""
import os
import json
import gspread
from slack_bolt import App
from google.oauth2.service_account import Credentials

app = App(token=os.environ["SLACK_BOT_TOKEN"])



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

COBRADORES = ["DIEGO", "IARA", "REBECA", "MARIANGEL", "LUISMAR", "ANGELY", "DANIEL", "BARBARA", "MARIANA", "ANDRES", "NELMAYRI", "ALEJANDRO", "ISAAC"]


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
