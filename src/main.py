import os
import re
import json
import unicodedata
import gspread
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler

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

SUPERVISOR_ID = "U0B51AREWDU"  # Leandro Quoota (escalamiento del Radar de promesas)

# Nombres de pestañas dentro del Sheet principal de Cobros (SHEET_ID)
PESTANA_INDICADORES = "Indicadores"           # Tasa del día (vigente) — B20=valor, C20=fecha
PESTANA_HISTORIAL_TASAS = "Historial Tasas"   # Historial de tasas por fecha (columnas: Fecha, Tasa)
# ============ FIN CONFIGURACIÓN GENERAL ============


# ============ FUNCIÓN PARA LEER NÚMEROS EN CUALQUIER FORMATO ============
def parse_numero(texto):
    if texto is None:
        raise ValueError("vacío")
    s = re.sub(r"[^0-9.,\-]", "", str(texto).strip())
    # Quitar separadores sobrantes al inicio/fin (ej. el punto que deja "Bs.")
    neg = s.startswith("-")
    s = s.lstrip("-").strip(".,")
    if s == "":
        raise ValueError("sin dígitos")
    if neg:
        s = "-" + s
    tiene_punto = "." in s
    tiene_coma = "," in s
    if tiene_punto and tiene_coma:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif tiene_coma:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            _, _, dec = s.partition(",")
            s = s.replace(",", "") if len(dec) == 3 else s.replace(",", ".")
    elif tiene_punto:
        if s.count(".") > 1:
            s = s.replace(".", "")
        else:
            _, _, dec = s.partition(".")
            if len(dec) == 3:
                s = s.replace(".", "")
    return float(s)
# ============ FIN FUNCIÓN parse_numero ============


# ============ BLINDAJE ANTI-DUPLICADOS ============
def _id_amigable(prefijo, ts):
    try:
        dt = datetime.fromtimestamp(float(ts), ZoneInfo("America/Caracas"))
        frac = str(ts).split(".")[-1]
        return f"{prefijo}-{dt.strftime('%Y%m%d-%H%M%S')}-{frac}"
    except Exception:
        return f"{prefijo}-{ts}"


def _registro_ya_guardado(sheet, registro_id):
    if not registro_id:
        return False
    objetivo = str(registro_id).strip()
    try:
        valores = sheet.get_all_values()
        if not valores:
            return False
        encabezados = [c.strip().lower() for c in valores[0]]
        if "id registro" in encabezados:
            col = encabezados.index("id registro")
            return any(len(fila) > col and str(fila[col]).strip() == objetivo for fila in valores[1:])
        return any(str(celda).strip() == objetivo for fila in valores for celda in fila)
    except Exception as e:
        print(f"⚠️ No se pudo verificar duplicado: {e}")
        return False


def _ya_procesado(texto):
    return texto.lstrip().startswith(("✅", "❌", "⚠"))
# ============ FIN BLINDAJE ANTI-DUPLICADOS ============


# ============ GUARDAR EN SHEETS POR NOMBRE DE COLUMNA (Fase 2 - Sostenibilidad) ============
def _normalizar_encabezado(texto):
    """Deja un encabezado listo para comparar: minúsculas, sin tildes, sin espacios de sobra."""
    t = str(texto or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def _guardar_fila_por_encabezado(sheet, datos):
    """
    Guarda una fila nueva en 'sheet' colocando cada valor en la columna que le corresponde
    POR NOMBRE, no por posición. 'datos' es un diccionario {nombre_de_columna: valor}, en el
    orden en que se quiere que caigan los datos que no tengan columna en el Sheet.

    Compara los nombres de columna ignorando mayúsculas, tildes y espacios de más, para que un
    encabezado como "Conciliación" o "Conciliacion" (con o sin tilde) se reconozca igual.

    Si alguna clave de 'datos' no tiene una columna con ese nombre en el Sheet, su valor se
    agrega al final de la fila, siempre en el mismo orden, para no perder el dato ni desalinear
    las columnas que sí existen.
    """
    encabezados_sheet = sheet.row_values(1)
    restantes = dict(datos)
    fila = []
    for encabezado in encabezados_sheet:
        objetivo = _normalizar_encabezado(encabezado)
        valor_encontrado = ""
        for clave in list(restantes.keys()):
            if _normalizar_encabezado(clave) == objetivo:
                valor_encontrado = restantes.pop(clave)
                break
        fila.append(valor_encontrado)
    # Cualquier dato que no tenía columna con ese nombre se agrega al final (siempre el mismo orden)
    fila.extend(restantes.values())
    sheet.append_row(fila)
# ============ FIN GUARDAR POR NOMBRE DE COLUMNA ============


# ============ LISTA DE COBRADORES (compartida) ============
COBRADORES = ["DIEGO", "IARA", "REBECA", "MARIANGEL", "LUISMAR", "ANGELY", "DANIEL", "BARBARA", "MARIANA", "ANDRES", "NELMAYRI", "ALEJANDRO", "ISAAC"]


def _opciones_cobradores():
    return [{"text": {"type": "plain_text", "text": c}, "value": c} for c in COBRADORES]


# ============ NUEVO COMANDO /contactar ============
def guardar_en_contactados(fecha, nombre, telefono, cedula, compromiso, cobrador, comentario):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        sheet = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == "contactados":
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Contactados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return
        sheet.append_row([fecha, nombre, telefono, cedula, compromiso, cobrador, comentario])
        print(f"✅ Contacto guardado en hoja '{sheet.title}'")
    except Exception as e:
        print(f"❌ Error guardando en Contactados: {type(e).__name__}: {e}")



# ============ VALIDACIÓN DE DATOS (estricta) ============
def _es_cedula_valida(texto):
    t = str(texto or "").strip().upper()
    if not t:
        return False, "La cédula está vacía."
    t2 = t.replace(".", "").replace("-", "").replace(" ", "")
    m = re.match(r"^([VEJPG]?)(\d+)$", t2)
    if not m:
        return False, "Cédula inválida. Usa números y opcional V/E/J/P (ej: V-12.345.678)."
    digitos = m.group(2)
    if not (6 <= len(digitos) <= 10):
        return False, f"La cédula debe tener entre 6 y 10 dígitos (tiene {len(digitos)})."
    return True, ""


def _es_telefono_valido(texto):
    t = str(texto or "").strip()
    if not t:
        return False, "El teléfono está vacío."
    limpio = re.sub(r"[()\-\s+]", "", t)
    if not limpio.isdigit():
        return False, "El teléfono solo debe tener números (ej: 0414-1234567)."
    n = len(limpio)
    if n not in (10, 11, 12):
        return False, f"El teléfono debe tener 10, 11 o 12 dígitos (tiene {n}). Ej: 04141234567."
    return True, ""


def _es_fecha_valida(texto):
    t = str(texto or "").strip()
    if not t:
        return False, "La fecha está vacía."
    partes = re.split(r"[/\-]", t)
    if len(partes) != 3:
        return False, "Fecha inválida. Usa el formato DD/MM/AAAA (ej: 25/12/2026)."
    try:
        d = int(partes[0]); mth = int(partes[1]); y = int(partes[2])
    except ValueError:
        return False, "La fecha debe tener solo números en formato DD/MM/AAAA."
    if y < 100:
        y += 2000
    try:
        date(y, mth, d)
    except ValueError:
        return False, "Esa fecha no existe. Revisa día/mes (formato DD/MM/AAAA)."
    if not (2024 <= y <= 2030):
        return False, "El año parece un error de tipeo. Usa DD/MM/AAAA (ej: 25/12/2026)."
    return True, ""


_VALIDADORES = {
    "cedula": _es_cedula_valida,
    "telefono": _es_telefono_valido,
    "fecha": _es_fecha_valida,
}


def _validar_view(valores, specs):
    """Devuelve un dict {block_id: mensaje} con los campos inválidos (vacío si todo OK)."""
    errores = {}
    for block_id, tipo in specs:
        try:
            valor = valores[block_id]["valor"]["value"]
        except (KeyError, TypeError):
            valor = ""
        ok, msg = _VALIDADORES[tipo](valor)
        if not ok:
            errores[block_id] = msg
    return errores
# ============ FIN VALIDACIÓN DE DATOS ============


@app.command("/contactar")
def reportar_contacto(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_contactar",
            "title": {"type": "plain_text", "text": "Reportar Contacto"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "telefono",
                 "label": {"type": "plain_text", "text": "Teléfono"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "compromiso",
                 "label": {"type": "plain_text", "text": "Compromiso de pago (DD/MM/YYYY)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cobrador",
                 "label": {"type": "plain_text", "text": "Cobrador"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_cobradores()}},
                {"type": "input", "block_id": "comentario",
                 "label": {"type": "plain_text", "text": "Comentario"},
                 "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}}
            ]
        }
    )


@app.view("form_contactar")
def recibir_contacto(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('telefono', 'telefono'), ('compromiso', 'fecha')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    compromiso = valores["compromiso"]["valor"]["value"]
    cobrador = valores["cobrador"]["valor"]["selected_option"]["value"]
    comentario = valores["comentario"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    guardar_en_contactados(fecha, nombre, telefono, cedula, compromiso, cobrador, comentario)
    texto = (
        f"*Nuevo contacto registrado* 📞\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Cliente:* {nombre}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Cédula:* {cedula}\n"
        f"*Compromiso de pago:* {compromiso}\n"
        f"*Cobrador:* {cobrador}\n"
        f"*Comentario:* {comentario}"
    )
    client.chat_postMessage(
        channel="#cobranzas-contactados",
        text="Nuevo contacto registrado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": texto}}]
    )
# ============ FIN COMANDO /contactar ============


# ============ COMANDO /cobro ============
def guardar_en_sheet(fecha, cobrador, descripcion, numero, cedula, monto_bs, forma_pago, banco, tasa_bcv, monto_usd, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        sheet = cliente.open_by_key(os.environ["SHEET_ID"]).worksheet("Pagos Recibidos")
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Cobro duplicado (ya guardado), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, descripcion, numero, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, cobrador, registro_id])
        print("✅ Cobro guardado en Google Sheets")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en sheet: {e}")
        return "ERROR"


@app.command("/cobro")
def reportar_cobro(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_cobro",
            "title": {"type": "plain_text", "text": "Reportar Cobro"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "fecha_pago", "optional": True,
                 "label": {"type": "plain_text", "text": "Fecha del Pago (DD/MM/AAAA) — déjalo vacío si es de hoy"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "nombre_cobrador",
                 "label": {"type": "plain_text", "text": "Nombre del Cobrador"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_cobradores()}},
                {"type": "input", "block_id": "descripcion",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "numero",
                 "label": {"type": "plain_text", "text": "Teléfono o Referencia"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "monto_bs",
                 "label": {"type": "plain_text", "text": "Monto en Bs"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "forma_pago",
                 "label": {"type": "plain_text", "text": "Forma de Pago"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
                                 {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
                                 {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
                                 {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "banco",
                 "label": {"type": "plain_text", "text": "Banco"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
                                 {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
                                 {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
                                 {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
                                 {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
                                 {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
                                 {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}}
            ]
        }
    )


@app.view("form_cobro")
def recibir_cobro(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula')])

    hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    fecha_pago_input = (_v.get("fecha_pago", {}).get("valor", {}).get("value") or "").strip()
    if fecha_pago_input:
        _ok_fecha, _msg_fecha = _es_fecha_valida(fecha_pago_input)
        if not _ok_fecha:
            _err["fecha_pago"] = _msg_fecha
        fecha_pago_final = fecha_pago_input
    else:
        fecha_pago_final = hoy_txt

    _tasa_num = _tasa_de_pago(fecha_pago_final, hoy_txt)
    if _tasa_num is None:
        if fecha_pago_final == hoy_txt:
            _err["monto_bs"] = "⚠️ Falta la tasa de hoy. Pide que pongan /tasa-hoy [valor] antes de reportar cobros."
        else:
            _err["fecha_pago"] = (f"⚠️ No hay tasa registrada para el {fecha_pago_final}. Pide que la fijen con "
                                   f"/tasa-hoy {fecha_pago_final} [valor] antes de reportar este cobro.")
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre_cobrador = valores["nombre_cobrador"]["valor"]["selected_option"]["value"]
    descripcion = valores["descripcion"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    numero = valores["numero"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_num = _tasa_num
    tasa_bcv_str = f"{tasa_bcv_num:,.4f}"
    cobrador_slack = body["user"]["id"]
    fecha = fecha_pago_final
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    texto = (
        f"*Nuevo cobro reportado* 💰\n"
        f"*Fecha:* {fecha}\n"
        f"*Cobrador:* {nombre_cobrador} (<@{cobrador_slack}>)\n"
        f"*Cliente:* {descripcion}\n"
        f"*Cédula:* {cedula}\n"
        f"*Teléfono:* {numero}\n"
        f"*Monto Bs:* {monto_bs_fmt}\n"
        f"*Forma de Pago:* {forma_pago}\n"
        f"*Banco:* {banco}\n"
        f"*Tasa BCV:* {tasa_bcv_str}\n"
        f"*Monto USD:* {monto_usd_str}"
    )
    client.chat_postMessage(
        channel="#cobranzas-log",
        text="Nuevo cobro reportado",
        metadata={"event_type": "cobro_reportado", "event_payload": {
            "fecha": fecha, "cobrador": nombre_cobrador, "descripcion": descripcion,
            "numero": numero, "cedula": cedula, "monto_bs": monto_bs_fmt,
            "forma_pago": forma_pago, "banco": banco, "tasa_bcv": tasa_bcv_str, "monto_usd": monto_usd_str}},
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar"}
            ]}
        ]
    )


@app.action("aprobar")
def aprobar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("COBRO", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_en_sheet(
            meta.get("fecha", fecha_revision), meta.get("cobrador", body["user"]["id"]),
            meta.get("descripcion", ""), meta.get("numero", ""), meta.get("cedula", ""),
            meta.get("monto_bs", ""), meta.get("forma_pago", ""), meta.get("banco", ""),
            meta.get("tasa_bcv", ""), meta.get("monto_usd", ""), registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — este cobro ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar")
def rechazar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /cobro ============


# ============ COMANDO /domiciliar ============
def guardar_en_domiciliacion(fecha, empresa, cuenta, monto_bs, banco, monto_usd, tasa_bcv, cobrador, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        sheet = None
        for ws in spreadsheet.worksheets():
            titulo = ws.title.strip().lower()
            if titulo in ("domiciliación", "domiciliacion"):
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Domiciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return "ERROR"
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Domiciliación duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, empresa, cuenta, monto_bs, banco, monto_usd, tasa_bcv, cobrador, registro_id])
        print(f"✅ Domiciliación guardada en hoja '{sheet.title}'")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en Domiciliación: {type(e).__name__}: {e}")
        return "ERROR"


@app.command("/domiciliar")
def reportar_domiciliacion(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_domiciliar",
            "title": {"type": "plain_text", "text": "Registrar Domiciliación"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "empresa",
                 "label": {"type": "plain_text", "text": "Empresa"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cuenta",
                 "label": {"type": "plain_text", "text": "Cuenta por cobrar"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "monto_bs",
                 "label": {"type": "plain_text", "text": "Monto en Bs"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "banco",
                 "label": {"type": "plain_text", "text": "Banco"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
                                 {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
                                 {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
                                 {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
                                 {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
                                 {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
                                 {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "tasa_bcv",
                 "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cobrador",
                 "label": {"type": "plain_text", "text": "Cobrador"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_cobradores()}}
            ]
        }
    )


@app.view("form_domiciliar")
def recibir_domiciliacion(ack, body, client):
    ack()
    valores = body["view"]["state"]["values"]
    empresa = valores["empresa"]["valor"]["value"]
    cuenta = valores["cuenta"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    cobrador = valores["cobrador"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    try:
        cuenta_num = parse_numero(cuenta)
        cuenta_fmt = f"Bs. {cuenta_num:,.2f}"
    except (ValueError, AttributeError):
        cuenta_fmt = f"Bs. {cuenta}"
    texto = (
        f"*Nueva domiciliación reportada* 🏦\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Empresa:* {empresa}\n"
        f"*Cuenta por cobrar:* {cuenta_fmt}\n"
        f"*Monto Bs:* {monto_bs_fmt}\n"
        f"*Banco:* {banco}\n"
        f"*Tasa BCV:* {tasa_bcv_str}\n"
        f"*Monto USD:* {monto_usd_str}\n"
        f"*Cobrador:* {cobrador}"
    )
    try:
        client.chat_postMessage(
            channel="#cobranzas-domiciliacion",
            text="Nueva domiciliación reportada",
            metadata={"event_type": "domiciliacion_reportada", "event_payload": {
                "fecha": fecha, "empresa": empresa, "cuenta": cuenta_fmt, "monto_bs": monto_bs_fmt,
                "banco": banco, "monto_usd": monto_usd_str, "tasa_bcv": tasa_bcv_str, "cobrador": cobrador}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_domiciliacion"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_domiciliacion"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de domiciliación: {e}")


@app.action("aprobar_domiciliacion")
def aprobar_domiciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("DOMIC", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_en_domiciliacion(
            meta.get("fecha", fecha_revision), meta.get("empresa", ""), meta.get("cuenta", ""),
            meta.get("monto_bs", ""), meta.get("banco", ""), meta.get("monto_usd", ""),
            meta.get("tasa_bcv", ""), meta.get("cobrador", ""), registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta domiciliación ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Domiciliación procesada",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_domiciliacion")
def rechazar_domiciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Domiciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /domiciliar ============


# ============ COMANDO /cobro-callcenter (Call Center Seguros) ============
def guardar_en_sheet_cobro2(fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, referencia, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(SHEET_ID_COBRO2)
        try:
            sheet = spreadsheet.worksheet("Hoja1")
        except Exception:
            sheet = spreadsheet.sheet1
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Cobro Call Center duplicado (ya guardado), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, referencia, registro_id])
        print("✅ Cobro (Call Center) guardado en Google Sheets")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en sheet cobro2: {type(e).__name__}: {e}")
        return "ERROR"


@app.command("/cobro-callcenter")
def reportar_cobro2(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_cobro2",
            "title": {"type": "plain_text", "text": "Cobro Call Center"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "telefono",
                 "label": {"type": "plain_text", "text": "Teléfono"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "monto_bs",
                 "label": {"type": "plain_text", "text": "Monto en Bs"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "forma_pago",
                 "label": {"type": "plain_text", "text": "Forma de Pago"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
                                 {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
                                 {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
                                 {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "banco",
                 "label": {"type": "plain_text", "text": "Banco"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
                                 {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
                                 {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
                                 {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
                                 {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
                                 {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
                                 {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "tasa_bcv",
                 "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "referencia",
                 "label": {"type": "plain_text", "text": "N° de referencia del pago"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}}
            ]
        }
    )


@app.view("form_cobro2")
def recibir_cobro2(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('telefono', 'telefono')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    referencia = valores["referencia"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    texto = (
        f"*Nuevo cobro reportado (Call Center)* 📞💰\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Cliente:* {nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Monto Bs:* {monto_bs_fmt}\n"
        f"*Forma de Pago:* {forma_pago}\n"
        f"*Banco:* {banco}\n"
        f"*Tasa BCV:* {tasa_bcv_str}\n"
        f"*Monto USD:* {monto_usd_str}\n"
        f"*N° referencia pago:* {referencia}"
    )
    client.chat_postMessage(
        channel="C0BAS4M970S",
        text="Nuevo cobro reportado (Call Center)",
        metadata={"event_type": "cobro2_reportado", "event_payload": {
            "fecha": fecha, "nombre": nombre, "telefono": telefono, "cedula": cedula,
            "monto_bs": monto_bs_fmt, "forma_pago": forma_pago, "banco": banco,
            "monto_usd": monto_usd_str, "tasa_bcv": tasa_bcv_str, "referencia": referencia}},
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_cobro2"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_cobro2"}
            ]}
        ]
    )


@app.action("aprobar_cobro2")
def aprobar_cobro2(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("CALLCENTER", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_en_sheet_cobro2(
            meta.get("fecha", fecha_revision), meta.get("nombre", ""), meta.get("telefono", ""),
            meta.get("cedula", ""), meta.get("monto_bs", ""), meta.get("forma_pago", ""),
            meta.get("banco", ""), meta.get("monto_usd", ""), meta.get("tasa_bcv", ""),
            meta.get("referencia", ""), registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — este cobro ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_cobro2")
def rechazar_cobro2(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /cobro-callcenter ============


# ============ COMANDO /conciliar ============
def guardar_en_conciliacion(fecha_conciliacion, cliente_nombre, cedula, referencia, banco,
                            monto_reportado, monto_banco, diferencia, estado,
                            fecha_movimiento, conciliador, observaciones, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        sheet = None
        for ws in spreadsheet.worksheets():
            titulo = ws.title.strip().lower()
            if titulo in ("conciliación", "conciliacion"):
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Conciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return "ERROR"
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Conciliación duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha_conciliacion, cliente_nombre, cedula, referencia, banco,
                          monto_reportado, monto_banco, diferencia, estado,
                          fecha_movimiento, conciliador, observaciones, registro_id])
        print(f"✅ Conciliación guardada en hoja '{sheet.title}'")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en Conciliación: {type(e).__name__}: {e}")
        return "ERROR"


@app.command("/conciliar")
def reportar_conciliacion(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_conciliar",
            "title": {"type": "plain_text", "text": "Conciliar Pago"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "cliente",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "referencia",
                 "label": {"type": "plain_text", "text": "N° de referencia del pago"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "banco",
                 "label": {"type": "plain_text", "text": "Banco"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
                                 {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
                                 {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
                                 {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
                                 {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
                                 {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
                                 {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "monto_reportado",
                 "label": {"type": "plain_text", "text": "Monto reportado (Bs)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "monto_banco",
                 "label": {"type": "plain_text", "text": "Monto según el banco (Bs)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "fecha_movimiento",
                 "label": {"type": "plain_text", "text": "Fecha del movimiento bancario (DD/MM/YYYY)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "conciliador",
                 "label": {"type": "plain_text", "text": "Conciliador"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_cobradores()}},
                {"type": "input", "block_id": "observaciones", "optional": True,
                 "label": {"type": "plain_text", "text": "Observaciones"},
                 "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}}
            ]
        }
    )


@app.view("form_conciliar")
def recibir_conciliacion(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('fecha_movimiento', 'fecha')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    cliente_nombre = valores["cliente"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    referencia = valores["referencia"]["valor"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    monto_reportado_str = valores["monto_reportado"]["valor"]["value"]
    monto_banco_str = valores["monto_banco"]["valor"]["value"]
    fecha_movimiento = valores["fecha_movimiento"]["valor"]["value"]
    conciliador = valores["conciliador"]["valor"]["selected_option"]["value"]
    observaciones = valores["observaciones"]["valor"]["value"] or ""
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    try:
        rep_num = parse_numero(monto_reportado_str)
        banco_num = parse_numero(monto_banco_str)
        diferencia_num = banco_num - rep_num
        monto_reportado_fmt = f"Bs. {rep_num:,.2f}"
        monto_banco_fmt = f"Bs. {banco_num:,.2f}"
        diferencia_fmt = f"Bs. {diferencia_num:,.2f}"
        if abs(diferencia_num) < 0.01:
            estado = "Conciliado"
            emoji_estado = "✅"
        else:
            estado = "Con diferencia"
            emoji_estado = "⚠️"
    except (ValueError, AttributeError):
        monto_reportado_fmt = f"Bs. {monto_reportado_str}"
        monto_banco_fmt = f"Bs. {monto_banco_str}"
        diferencia_fmt = "(No calculable)"
        estado = "Revisar manualmente"
        emoji_estado = "❓"
    texto = (
        f"*Nueva conciliación reportada* 🧾\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Cliente:* {cliente_nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*N° referencia pago:* {referencia}\n"
        f"*Banco:* {banco}\n"
        f"*Monto reportado:* {monto_reportado_fmt}\n"
        f"*Monto según banco:* {monto_banco_fmt}\n"
        f"*Diferencia:* {diferencia_fmt}\n"
        f"*Estado:* {emoji_estado} {estado}\n"
        f"*Fecha movimiento banco:* {fecha_movimiento}\n"
        f"*Conciliador:* {conciliador}\n"
        f"*Observaciones:* {observaciones}"
    )
    try:
        client.chat_postMessage(
            channel="#cobranzas-conciliar",
            text="Nueva conciliación reportada",
            metadata={"event_type": "conciliacion_reportada", "event_payload": {
                "fecha": fecha, "cliente": cliente_nombre, "cedula": cedula, "referencia": referencia,
                "banco": banco, "monto_reportado": monto_reportado_fmt, "monto_banco": monto_banco_fmt,
                "diferencia": diferencia_fmt, "estado": estado, "fecha_movimiento": fecha_movimiento,
                "conciliador": conciliador, "observaciones": observaciones}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_conciliacion"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_conciliacion"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de conciliación: {e}")


@app.action("aprobar_conciliacion")
def aprobar_conciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("CONC", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_en_conciliacion(
            meta.get("fecha", fecha_revision), meta.get("cliente", ""), meta.get("cedula", ""),
            meta.get("referencia", ""), meta.get("banco", ""), meta.get("monto_reportado", ""),
            meta.get("monto_banco", ""), meta.get("diferencia", ""), meta.get("estado", ""),
            meta.get("fecha_movimiento", ""), meta.get("conciliador", ""), meta.get("observaciones", ""), registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta conciliación ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación procesada",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_conciliacion")
def rechazar_conciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /conciliar ============


# ============ COMANDOS DE LIQUIDACIONES (Lista VIP) ============
ESTATUS_LIQUIDACION = [
    "Pending", "In validation", "Template contract", "Waiting contract",
    "Contract in validation", "Fecha primer pago", "Pending deposit"
]
BASES_LIQUIDACION = ["Base 1", "Base 2", "Base 3", "Base 4"]


def _abrir_hoja_liquidaciones():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_LIQUIDACIONES)
    try:
        return spreadsheet.worksheet("Hoja1")
    except Exception:
        return spreadsheet.sheet1


def _opciones_lista(lista):
    return [{"text": {"type": "plain_text", "text": x}, "value": x} for x in lista]


def guardar_liquidacion_nueva(fecha, nombre, cedula, cliente_empresa, base, estatus, registro_id=""):
    try:
        sheet = _abrir_hoja_liquidaciones()
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Liquidación duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, nombre, cedula, cliente_empresa, base, estatus, fecha, registro_id])
        print(f"✅ Liquidación nueva guardada: {nombre} ({cedula})")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando liquidación nueva: {type(e).__name__}: {e}")
        return "ERROR"


def actualizar_estatus_liquidacion(cedula, nuevo_estatus, fecha_actualizacion):
    try:
        sheet = _abrir_hoja_liquidaciones()
        valores = sheet.get_all_values()
        cedula_buscada = str(cedula).strip()
        for i, fila in enumerate(valores):
            if i == 0:
                continue
            if len(fila) > 2 and fila[2].strip() == cedula_buscada:
                num_fila = i + 1
                sheet.update_cell(num_fila, 6, nuevo_estatus)
                sheet.update_cell(num_fila, 7, fecha_actualizacion)
                print(f"✅ Estatus actualizado para cédula {cedula_buscada}: {nuevo_estatus}")
                return True
        print(f"⚠️ No se encontró la cédula {cedula_buscada} en Liquidaciones")
        return False
    except Exception as e:
        print(f"❌ Error actualizando estatus: {type(e).__name__}: {e}")
        return False


@app.command("/liquidacion-nueva")
def reportar_liquidacion_nueva(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "form_liquidacion_nueva",
            "title": {"type": "plain_text", "text": "Nueva Liquidación"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre completo"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cliente",
                 "label": {"type": "plain_text", "text": "Cliente / Empresa"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "base",
                 "label": {"type": "plain_text", "text": "Base"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_lista(BASES_LIQUIDACION)}},
                {"type": "input", "block_id": "estatus",
                 "label": {"type": "plain_text", "text": "Estatus inicial"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_lista(ESTATUS_LIQUIDACION)}}
            ]
        }
    )


@app.view("form_liquidacion_nueva")
def recibir_liquidacion_nueva(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    cliente_empresa = valores["cliente"]["valor"]["value"]
    base = valores["base"]["valor"]["selected_option"]["value"]
    estatus = valores["estatus"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    texto = (
        f"*Nueva persona en Lista VIP* 🌟\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Nombre:* {nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*Cliente/Empresa:* {cliente_empresa}\n"
        f"*Base:* {base}\n"
        f"*Estatus:* {estatus}"
    )
    try:
        client.chat_postMessage(
            channel=CANAL_LIQUIDACIONES,
            text="Nueva persona en Lista VIP",
            metadata={"event_type": "liquidacion_nueva", "event_payload": {
                "fecha": fecha, "nombre": nombre, "cedula": cedula,
                "cliente": cliente_empresa, "base": base, "estatus": estatus}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_liquidacion_nueva"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_liquidacion_nueva"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de liquidaciones: {e}")


@app.action("aprobar_liquidacion_nueva")
def aprobar_liquidacion_nueva(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("LIQNUEVA", body["message"]["ts"])
    meta = body["message"].get("metadata", {}).get("event_payload", {})
    resultado = guardar_liquidacion_nueva(
        meta.get("fecha", fecha_revision), meta.get("nombre", ""), meta.get("cedula", ""),
        meta.get("cliente", ""), meta.get("base", ""), meta.get("estatus", ""), registro_id)
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    elif resultado == "OK":
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"⚠️ *APROBADO pero hubo error guardando (revisar logs)* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Liquidación procesada",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_liquidacion_nueva")
def rechazar_liquidacion_nueva(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Liquidación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.command("/liquidacion-estatus")
def reportar_liquidacion_estatus(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "form_liquidacion_estatus",
            "title": {"type": "plain_text", "text": "Cambiar Estatus"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula de la persona"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre (referencia)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "estatus",
                 "label": {"type": "plain_text", "text": "Nuevo estatus"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_lista(ESTATUS_LIQUIDACION)}}
            ]
        }
    )


@app.view("form_liquidacion_estatus")
def recibir_liquidacion_estatus(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    cedula = valores["cedula"]["valor"]["value"]
    nombre = valores["nombre"]["valor"]["value"]
    estatus = valores["estatus"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    texto = (
        f"*Cambio de estatus solicitado* 🔄\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Nombre:* {nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*Nuevo estatus:* {estatus}"
    )
    try:
        client.chat_postMessage(
            channel=CANAL_LIQUIDACIONES,
            text="Cambio de estatus solicitado",
            metadata={"event_type": "liquidacion_estatus", "event_payload": {
                "fecha": fecha, "nombre": nombre, "cedula": cedula, "estatus": estatus}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_liquidacion_estatus"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_liquidacion_estatus"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de liquidaciones: {e}")


@app.action("aprobar_liquidacion_estatus")
def aprobar_liquidacion_estatus(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    meta = body["message"].get("metadata", {}).get("event_payload", {})
    encontrado = actualizar_estatus_liquidacion(meta.get("cedula", ""), meta.get("estatus", ""), fecha_revision)
    if encontrado:
        encabezado = f"✅ *ESTATUS ACTUALIZADO* por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"⚠️ *NO SE ENCONTRÓ ESA CÉDULA EN LA LISTA* (revisado por <@{body['user']['id']}> el {fecha_revision}). No se actualizó nada."
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cambio de estatus procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_liquidacion_estatus")
def rechazar_liquidacion_estatus(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cambio de estatus RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDOS DE LIQUIDACIONES ============


# ============ COMANDO /cobro-comercial (Equipo Comercial) ============
def guardar_en_sheet_comercial(fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, empresa, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(SHEET_ID_COMERCIAL)
        try:
            sheet = spreadsheet.worksheet("Sheet1")
        except Exception:
            sheet = spreadsheet.sheet1
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Cobro comercial duplicado (ya guardado), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, empresa, registro_id])
        print("✅ Cobro (Comercial) guardado en Google Sheets")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en sheet comercial: {type(e).__name__}: {e}")
        return "ERROR"


@app.command("/cobro-comercial")
def reportar_cobro_comercial(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "form_cobro_comercial",
            "title": {"type": "plain_text", "text": "Cobro Comercial"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "telefono",
                 "label": {"type": "plain_text", "text": "Teléfono"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "monto_bs",
                 "label": {"type": "plain_text", "text": "Monto en Bs"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "forma_pago",
                 "label": {"type": "plain_text", "text": "Forma de Pago"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
                                 {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
                                 {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
                                 {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "banco",
                 "label": {"type": "plain_text", "text": "Banco"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
                                 {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
                                 {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
                                 {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
                                 {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
                                 {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
                                 {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
                                 {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                             ]}},
                {"type": "input", "block_id": "tasa_bcv",
                 "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "empresa",
                 "label": {"type": "plain_text", "text": "Empresa"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}}
            ]
        }
    )


@app.view("form_cobro_comercial")
def recibir_cobro_comercial(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('telefono', 'telefono')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    empresa = valores["empresa"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    texto = (
        f"*Nuevo cobro reportado (Comercial)* 🤝💰\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Cliente:* {nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Monto Bs:* {monto_bs_fmt}\n"
        f"*Forma de Pago:* {forma_pago}\n"
        f"*Banco:* {banco}\n"
        f"*Tasa BCV:* {tasa_bcv_str}\n"
        f"*Monto USD:* {monto_usd_str}\n"
        f"*Empresa:* {empresa}"
    )
    client.chat_postMessage(
        channel=CANAL_COMERCIAL,
        text="Nuevo cobro reportado (Comercial)",
        metadata={"event_type": "cobro_comercial_reportado", "event_payload": {
            "fecha": fecha, "nombre": nombre, "telefono": telefono, "cedula": cedula,
            "monto_bs": monto_bs_fmt, "forma_pago": forma_pago, "banco": banco,
            "monto_usd": monto_usd_str, "tasa_bcv": tasa_bcv_str, "empresa": empresa}},
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_comercial"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_comercial"}
            ]}
        ]
    )


@app.action("aprobar_comercial")
def aprobar_comercial(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("COMERCIAL", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_en_sheet_comercial(
            meta.get("fecha", fecha_revision), meta.get("nombre", ""), meta.get("telefono", ""),
            meta.get("cedula", ""), meta.get("monto_bs", ""), meta.get("forma_pago", ""),
            meta.get("banco", ""), meta.get("monto_usd", ""), meta.get("tasa_bcv", ""),
            meta.get("empresa", ""), registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_comercial")
def rechazar_comercial(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /cobro-comercial ============


# ============ COMANDO /contacto-legal (Equipo Legal) ============
def guardar_en_contactados_legal(fecha, nombre, telefono, cedula, compromiso, cobrador, comentario, registro_id=""):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(SHEET_ID_LEGAL)
        sheet = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == "contactados":
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Contactados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return "ERROR"
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Contacto legal duplicado (ya guardado), se omite.")
            return "DUPLICADO"
        sheet.append_row([fecha, nombre, telefono, cedula, compromiso, cobrador, comentario, registro_id])
        print(f"✅ Contacto (Legal) guardado en hoja '{sheet.title}'")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en Contactados (Legal): {type(e).__name__}: {e}")
        return "ERROR"


@app.command("/contacto-legal")
def reportar_contacto_legal(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "form_contacto_legal",
            "title": {"type": "plain_text", "text": "Contacto Legal"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "telefono",
                 "label": {"type": "plain_text", "text": "Teléfono"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "compromiso",
                 "label": {"type": "plain_text", "text": "Compromiso de pago (DD/MM/YYYY)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cobrador",
                 "label": {"type": "plain_text", "text": "Cobrador"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "Maria"}, "value": "Maria"},
                                 {"text": {"type": "plain_text", "text": "Gabriela"}, "value": "Gabriela"}
                             ]}},
                {"type": "input", "block_id": "comentario",
                 "label": {"type": "plain_text", "text": "Comentario"},
                 "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}}
            ]
        }
    )


@app.view("form_contacto_legal")
def recibir_contacto_legal(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('telefono', 'telefono'), ('compromiso', 'fecha')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    compromiso = valores["compromiso"]["valor"]["value"]
    cobrador = valores["cobrador"]["valor"]["selected_option"]["value"]
    comentario = valores["comentario"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    texto = (
        f"*Nuevo contacto Legal* ⚖️\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Cliente:* {nombre}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Cédula:* {cedula}\n"
        f"*Compromiso de pago:* {compromiso}\n"
        f"*Cobrador:* {cobrador}\n"
        f"*Comentario:* {comentario}"
    )
    try:
        client.chat_postMessage(
            channel=CANAL_LEGAL,
            text="Nuevo contacto Legal",
            metadata={"event_type": "contacto_legal", "event_payload": {
                "fecha": fecha, "nombre": nombre, "telefono": telefono, "cedula": cedula,
                "compromiso": compromiso, "cobrador": cobrador, "comentario": comentario}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_contacto_legal"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_contacto_legal"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal legal: {e}")


@app.action("aprobar_contacto_legal")
def aprobar_contacto_legal(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("LEGAL", body["message"]["ts"])
    meta = body["message"].get("metadata", {}).get("event_payload", {})
    resultado = guardar_en_contactados_legal(
        meta.get("fecha", fecha_revision), meta.get("nombre", ""), meta.get("telefono", ""),
        meta.get("cedula", ""), meta.get("compromiso", ""), meta.get("cobrador", ""),
        meta.get("comentario", ""), registro_id)
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    elif resultado == "OK":
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"⚠️ *APROBADO pero hubo error guardando (revisar logs)* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Contacto Legal procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_contacto_legal")
def rechazar_contacto_legal(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Contacto Legal RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /contacto-legal ============


# ============ COMANDO TEMPORAL /listar-ids ============
# Devuelve (solo a quien lo ejecuta) la lista de miembros del workspace con su ID de Slack.
# Útil para recolectar los IDs de los cobradores. Se puede borrar después.
@app.command("/listar-ids")
def listar_ids(ack, body, client):
    ack()
    solicitante = body["user_id"]
    canal = body["channel_id"]
    try:
        miembros = []
        cursor = None
        while True:
            resp = client.users_list(limit=200, cursor=cursor)
            for u in resp["members"]:
                if u.get("deleted"):
                    continue
                if u.get("is_bot"):
                    continue
                if u.get("id") == "USLACKBOT":
                    continue
                perfil = u.get("profile", {}) or {}
                nombre = (perfil.get("real_name") or u.get("real_name")
                          or perfil.get("display_name") or u.get("name") or "sin nombre")
                miembros.append(f"{nombre} = {u['id']}")
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        if not miembros:
            client.chat_postEphemeral(channel=canal, user=solicitante,
                                      text="No se encontraron miembros.")
            return

        miembros.sort(key=lambda x: x.lower())
        encabezado = f"🪪 *Miembros del workspace ({len(miembros)}):*\n"
        # Slack limita el tamaño del mensaje; enviamos en bloques de 50 líneas
        bloque = []
        conteo = 0
        for i, linea in enumerate(miembros, 1):
            bloque.append(linea)
            if len(bloque) == 50 or i == len(miembros):
                conteo += 1
                texto = (encabezado if conteo == 1 else f"*(continuación {conteo})*\n") + "```\n" + "\n".join(bloque) + "\n```"
                client.chat_postEphemeral(channel=canal, user=solicitante, text=texto)
                bloque = []
    except Exception as e:
        client.chat_postEphemeral(
            channel=canal, user=solicitante,
            text=(f"❌ No se pudo obtener la lista: {type(e).__name__}: {e}\n\n"
                  "Puede que al bot le falte el permiso *users:read*. "
                  "Ve a api.slack.com → tu app → OAuth & Permissions → Scopes → agrega *users:read* → Reinstall.")
        )
# ============ FIN COMANDO /listar-ids ============


# ============ COMANDO /cliente-escalado (Clientes Escalados) ============
# Columnas: Fecha, Nombre del cliente, Teléfono, Cédula, Empresa, Incidencia, Reportada por
def guardar_cliente_escalado(fecha, nombre, telefono, cedula, empresa, incidencia, reportada_por):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(SHEET_ID_ESCALADOS)
        sheet = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == "clientes escalados":
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Clientes escalados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return
        sheet.append_row([fecha, nombre, telefono, cedula, empresa, incidencia, reportada_por])
        print(f"✅ Cliente escalado guardado en hoja '{sheet.title}'")
    except Exception as e:
        print(f"❌ Error guardando en Clientes escalados: {type(e).__name__}: {e}")


@app.command("/clientes-escalados")
def reportar_cliente_escalado(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_cliente_escalado",
            "title": {"type": "plain_text", "text": "Cliente Escalado"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {"type": "input", "block_id": "nombre",
                 "label": {"type": "plain_text", "text": "Nombre del cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "telefono",
                 "label": {"type": "plain_text", "text": "Teléfono del cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "cedula",
                 "label": {"type": "plain_text", "text": "Cédula del cliente"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "empresa",
                 "label": {"type": "plain_text", "text": "Empresa"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "incidencia",
                 "label": {"type": "plain_text", "text": "Incidencia (describe el problema)"},
                 "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}},
                {"type": "input", "block_id": "reportada_por",
                 "label": {"type": "plain_text", "text": "Reportada por"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": _opciones_cobradores()}}
            ]
        }
    )


@app.view("form_cliente_escalado")
def recibir_cliente_escalado(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula'), ('telefono', 'telefono')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    empresa = valores["empresa"]["valor"]["value"]
    incidencia = valores["incidencia"]["valor"]["value"]
    reportada_por = valores["reportada_por"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    guardar_cliente_escalado(fecha, nombre, telefono, cedula, empresa, incidencia, reportada_por)
    texto = (
        f"*Nuevo cliente escalado* 🚩\n"
        f"*Fecha:* {fecha}\n"
        f"*Reportado por:* <@{usuario_slack}> ({reportada_por})\n"
        f"*Cliente:* {nombre}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Cédula:* {cedula}\n"
        f"*Empresa:* {empresa}\n"
        f"*Incidencia:* {incidencia}"
    )
    try:
        client.chat_postMessage(channel=CANAL_ESCALADOS, text="Nuevo cliente escalado",
                                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": texto}}])
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de escalados: {e}")
# ============ FIN COMANDO /cliente-escalado ============


# ============ COMANDO /buscar-cliente (consulta por cédula) ============
# Busca una cédula en todas las hojas y devuelve el historial (solo lo ve quien pregunta).

def _solo_digitos(texto):
    return re.sub(r"\D", "", str(texto or ""))


def _quitar_acentos(texto):
    reemplazos = (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"))
    t = str(texto).lower()
    for a, b in reemplazos:
        t = t.replace(a, b)
    return t


def _buscar_en_hoja(cliente, sheet_id, nombre_hoja, etiqueta, cedula_digitos):
    """Devuelve lista de líneas (strings) con las coincidencias en una hoja."""
    resultados = []
    try:
        spreadsheet = cliente.open_by_key(sheet_id)
        # Encontrar la hoja (tolerante a mayúsculas/acentos)
        hoja = None
        objetivo = _quitar_acentos(nombre_hoja).strip()
        for ws in spreadsheet.worksheets():
            if _quitar_acentos(ws.title).strip() == objetivo:
                hoja = ws
                break
        if hoja is None:
            return resultados
        valores = hoja.get_all_values()
        if not valores:
            return resultados
        encabezados = [_quitar_acentos(c).strip() for c in valores[0]]
        # Buscar la columna de cédula por su encabezado
        col_ced = None
        for idx, h in enumerate(encabezados):
            if "ced" in h:  # cubre "cedula", "cédula", "cedula del cliente"
                col_ced = idx
                break
        if col_ced is None:
            return resultados  # esta hoja no tiene cédula (ej. Domiciliación)
        for fila in valores[1:]:
            if len(fila) > col_ced and _solo_digitos(fila[col_ced]) == cedula_digitos and cedula_digitos:
                fecha = fila[0] if len(fila) > 0 else ""
                nombre = fila[1] if len(fila) > 1 else ""
                resultados.append(f"   • {fecha} — {nombre}")
    except Exception as e:
        print(f"⚠️ Error buscando en '{etiqueta}': {type(e).__name__}: {e}")
    return resultados


@app.command("/buscar-cliente")
def buscar_cliente(ack, body, client):
    ack()
    texto = (body.get("text") or "").strip()
    canal = body["channel_id"]
    usuario = body["user_id"]

    if not texto:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text="Escribe la cédula después del comando. Ejemplo: `/buscar-cliente 12345678`")
        return

    cedula_digitos = _solo_digitos(texto)
    if not cedula_digitos:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text="No detecté números en lo que escribiste. Ejemplo: `/buscar-cliente 12345678`")
        return

    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"🔎 Buscando la cédula *{texto}* en todas las hojas... un momento.")

    try:
        gcliente = get_cliente_busqueda()
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ No pude conectar con Google Sheets: {type(e).__name__}: {e}")
        return

    # Fuentes: (sheet_id, nombre_hoja, etiqueta)
    fuentes = [
        (os.environ["SHEET_ID"], "Contactados", "📞 Contactos"),
        (os.environ["SHEET_ID"], "Pagos Recibidos", "💰 Cobros"),
        (os.environ["SHEET_ID"], "Conciliacion", "🧾 Conciliaciones"),
        (SHEET_ID_COBRO2, "Hoja1", "📞 Call Center"),
        (SHEET_ID_LIQUIDACIONES, "Hoja1", "🌟 Liquidaciones"),
        (SHEET_ID_COMERCIAL, "Sheet1", "🤝 Comercial"),
        (SHEET_ID_LEGAL, "Contactados", "⚖️ Legal"),
        (SHEET_ID_ESCALADOS, "Clientes escalados", "🚩 Escalados"),
    ]

    bloques = []
    total = 0
    for sheet_id, nombre_hoja, etiqueta in fuentes:
        lineas = _buscar_en_hoja(gcliente, sheet_id, nombre_hoja, etiqueta, cedula_digitos)
        if lineas:
            total += len(lineas)
            bloque = [f"*{etiqueta}* ({len(lineas)}):"]
            bloque.extend(lineas[:10])
            if len(lineas) > 10:
                bloque.append(f"   … y {len(lineas) - 10} más")
            bloques.append("\n".join(bloque))

    if total == 0:
        mensaje = f"🔎 No encontré registros para la cédula *{texto}* en ninguna hoja."
    else:
        mensaje = f"🗂️ *Historial de la cédula {texto}* — {total} registro(s):\n\n" + "\n\n".join(bloques)

    client.chat_postEphemeral(channel=canal, user=usuario, text=mensaje)


# Conexión propia para la búsqueda (usa el mismo patrón del resto del bot)
def get_cliente_busqueda():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)
# ============ FIN COMANDO /buscar-cliente ============

# ============ RADAR DE PROMESAS DE PAGO (Fase 1) ============

# Tabla nombre del cobrador (como aparece en el Sheet) -> ID(s) de Slack
COBRADOR_SLACK_IDS = {
    "DIEGO": ["U0B3BAA8Y01", "U0B68124C9E"],
    "IARA": ["U0B58192UJH"],
    "REBECA": ["U0B59P76H8U"],
    "MARIANGEL": ["U0B4QM8D3PH"],
    "LUISMAR": ["U0BA6BWJWBF"],
    "ANGELY": ["U0BA8AHVA3Z"],
    "DANIEL": ["U0BAH6AFMA7"],
    "MARIANA": ["U0BHUF23EQY"],
    "ANDRES": ["U0BH22WRTQR"],
    "NELMAYRI": ["U0BK8E35T9A"],
    "ALEJANDRO": ["U0BLAV5EVSR"],
    "ISAAC": ["U0BL80V55DZ"],
    # BARBARA aún sin ID: se muestra en texto
}

# Cuántos días hacia atrás/adelante se considera una fecha "creíble"
RADAR_RANGO_DIAS = 90


def _mencion_cobrador(nombre):
    clave = str(nombre).strip().upper()
    ids = COBRADOR_SLACK_IDS.get(clave)
    if ids:
        return " ".join(f"<@{uid}>" for uid in ids)
    return f"*{nombre or 'Sin cobrador'}*"


def _parsear_fecha_radar(texto, hoy):
    """Devuelve (fecha, motivo). fecha=date si es creíble; si no, fecha=None y motivo explica por qué.
    Corrige años de 3 dígitos (206->2026) y de 2 dígitos. Marca como 'revisar' lo fuera de rango."""
    if not texto or not str(texto).strip():
        return None, "sin fecha"
    t = str(texto).strip()
    # Separar día/mes/año aceptando / o -
    partes = re.split(r"[/\-]", t)
    if len(partes) != 3:
        return None, "formato raro"
    try:
        dia = int(partes[0]); mes = int(partes[1]); anio = int(partes[2])
    except ValueError:
        return None, "no numérica"
    # Corregir el año
    if anio < 100:            # 2 dígitos: 26 -> 2026
        anio += 2000
    elif 100 <= anio < 1000:  # 3 dígitos: 206 -> 2026 (toma el año actual como base)
        anio = int(str(hoy.year)[:1] + str(anio).zfill(3))  # 206 -> 2 + 026? -> ajuste abajo
        # Ajuste simple y seguro: usar el año actual (los de 3 dígitos son claramente error de tipeo)
        anio = hoy.year
    try:
        f = date(anio, mes, dia)
    except ValueError:
        return None, "día/mes inválido"
    # ¿Está dentro del rango creíble?
    if abs((f - hoy).days) > RADAR_RANGO_DIAS:
        return None, "fuera de rango"
    return f, "ok"


def generar_resumen_promesas():
    """Lee 'Contactados', agrupa promesas por cobrador y publica el resumen."""
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        sheet = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == "contactados":
                sheet = ws
                break
        if sheet is None:
            print("❌ Radar: no se encontró la hoja 'Contactados'")
            return

        valores = sheet.get_all_values()
        hoy = datetime.now(ZoneInfo("America/Caracas")).date()
        hoy_txt = hoy.strftime("%d/%m/%Y")

        # Estructura: por cobrador -> {"hoy": [...], "vencidas": [...]}
        por_cobrador = {}
        revisar = []       # fechas raras
        total_hoy = 0
        total_venc = 0

        for i, fila in enumerate(valores):
            if i == 0:
                continue

            def celda(idx):
                return fila[idx].strip() if len(fila) > idx else ""

            nombre = celda(1) or "(sin nombre)"
            cedula = celda(3)
            compromiso = celda(4)
            cobrador = celda(5) or "Sin cobrador"
            estado = celda(7)
            if estado:  # ya resuelta
                continue

            fecha_prom, motivo = _parsear_fecha_radar(compromiso, hoy)

            if fecha_prom is None:
                # Solo mandamos a "revisar" las que tienen ALGO escrito pero está mal
                if motivo in ("formato raro", "no numérica", "día/mes inválido", "fuera de rango"):
                    revisar.append({"nombre": nombre, "cedula": cedula,
                                    "cobrador": cobrador, "texto": compromiso})
                continue  # "sin fecha" simplemente se ignora

            grupo = por_cobrador.setdefault(cobrador, {"hoy": [], "vencidas": [], "vistas": set()})
            # Evitar duplicados: misma cédula ya contada para este cobrador
            ced_norm = _solo_digitos(cedula)
            if ced_norm and ced_norm in grupo["vistas"]:
                continue
            if ced_norm:
                grupo["vistas"].add(ced_norm)
            item = {"nombre": nombre, "cedula": cedula, "fecha": fecha_prom}
            if fecha_prom == hoy:
                grupo["hoy"].append(item); total_hoy += 1
            elif fecha_prom < hoy:
                grupo["vencidas"].append(item); total_venc += 1
            # (futuras dentro de rango: no se listan hoy)

        # ---- Armar el mensaje ----
        lineas = []
        lineas.append(f"📅 *RADAR DE PROMESAS DE PAGO*  ·  {hoy_txt}")
        lineas.append(f"Resumen: *{total_hoy}* para hoy · *{total_venc}* vencidas · *{len(revisar)}* por revisar")
        lineas.append("━━━━━━━━━━━━━━━━━━━━")

        if not por_cobrador:
            lineas.append("")
            lineas.append("✅ No hay promesas de hoy ni vencidas con fecha válida.")
        else:
            # Orden alfabético por cobrador
            for cobrador in sorted(por_cobrador.keys(), key=lambda x: x.upper()):
                grupo = por_cobrador[cobrador]
                if not grupo["hoy"] and not grupo["vencidas"]:
                    continue
                lineas.append("")
                lineas.append(f"👤 *{cobrador.upper()}*  {_mencion_cobrador(cobrador)}")

                if grupo["hoy"]:
                    lineas.append(f"   ☀️ _Para hoy ({len(grupo['hoy'])}):_")
                    for it in grupo["hoy"]:
                        ced = f" · {it['cedula']}" if it["cedula"] else ""
                        lineas.append(f"      • {it['nombre']}{ced}")

                if grupo["vencidas"]:
                    # Más recientes primero (fecha más nueva arriba)
                    grupo["vencidas"].sort(key=lambda x: x["fecha"], reverse=True)
                    total_v = len(grupo["vencidas"])
                    lineas.append(f"   ⏰ _Vencidas ({total_v}):_")
                    for it in grupo["vencidas"][:10]:
                        ced = f" · {it['cedula']}" if it["cedula"] else ""
                        dias = (hoy - it["fecha"]).days
                        alerta = " 🔴" if dias >= 3 else ""
                        lineas.append(f"      • {it['nombre']}{ced} — hace {dias}d{alerta}")
                    if total_v > 10:
                        lineas.append(f"      … y {total_v - 10} vencida(s) más (ver Sheet)")

        # ---- Sección "revisar fecha" (resumida, no abruma) ----
        if revisar:
            lineas.append("")
            lineas.append("━━━━━━━━━━━━━━━━━━━━")
            lineas.append(f"⚠️ *{len(revisar)} promesa(s) con fecha rara* (corregir en el Sheet). Ejemplos:")
            for it in revisar[:5]:
                ced = f" · {it['cedula']}" if it["cedula"] else ""
                lineas.append(f"      • {it['nombre']}{ced} — escrito: \"{it['texto']}\"")
            if len(revisar) > 5:
                lineas.append(f"      … y {len(revisar) - 5} más.")

        # ---- Escalamiento al supervisor ----
        muy_vencidas = total_venc  # cuántas vencidas totales
        if muy_vencidas > 0:
            lineas.append("")
            lineas.append(f"🔴 <@{SUPERVISOR_ID}> hay *{muy_vencidas}* promesa(s) vencida(s) que requieren seguimiento.")

        mensaje = "\n".join(lineas).strip()

        # Slack corta mensajes muy largos (~40k). Si es enorme, avisamos y mandamos recortado.
        if len(mensaje) > 38000:
            mensaje = mensaje[:38000] + "\n\n… (lista recortada por tamaño; hay más en el Sheet)"

        app.client.chat_postMessage(channel=CANAL_SEGUIMIENTO, text=mensaje)
        print(f"✅ Radar publicado: {total_hoy} hoy, {total_venc} vencidas, {len(revisar)} por revisar")
    except Exception as e:
        print(f"❌ Error generando el resumen de promesas: {type(e).__name__}: {e}")


# Comando manual para PROBAR el radar sin esperar a las 4 PM
@app.command("/probar-radar")
def probar_radar(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando el radar de promesas ahora mismo... revisa el canal #cobranzas-seguimiento."
    )
    generar_resumen_promesas()
# ============ FIN RADAR DE PROMESAS ============



# ============ FASE 2: MARCAR PROMESAS (cumplida / fallida) ============
# Marca TODAS las filas de 'Contactados' con esa cédula:
#   Columna H (8) = Estado de promesa (Cumplida/Fallida)
#   Columna I (9) = Fecha resultado (hoy)
# Así la promesa desaparece del radar.

def _marcar_promesa(cedula_texto, estado):
    """Devuelve (cantidad_marcada, nombre_ejemplo)."""
    cedula_digitos = _solo_digitos(cedula_texto)
    if not cedula_digitos:
        return 0, None
    cliente = get_cliente_busqueda()
    spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
    hoja = None
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "contactados":
            hoja = ws
            break
    if hoja is None:
        return 0, None
    valores = hoja.get_all_values()
    hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    marcadas = 0
    nombre_ejemplo = None
    for i, fila in enumerate(valores):
        if i == 0:
            continue
        # Columna D (índice 3) = Cédula
        if len(fila) > 3 and _solo_digitos(fila[3]) == cedula_digitos:
            num_fila = i + 1  # gspread cuenta desde 1
            hoja.update_cell(num_fila, 8, estado)   # H = Estado de promesa
            hoja.update_cell(num_fila, 9, hoy_txt)  # I = Fecha resultado
            marcadas += 1
            if nombre_ejemplo is None and len(fila) > 1:
                nombre_ejemplo = fila[1]
    return marcadas, nombre_ejemplo


def _comando_marcar(ack, body, client, estado, emoji):
    ack()
    texto = (body.get("text") or "").strip()
    canal = body["channel_id"]
    usuario = body["user_id"]
    sufijo = "cumplida" if estado == "Cumplida" else "fallida"
    if not texto:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"Escribe la cédula. Ejemplo: `/promesa-{sufijo} 12345678`")
        return
    try:
        marcadas, nombre = _marcar_promesa(texto, estado)
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al marcar: {type(e).__name__}: {e}")
        return
    if marcadas == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"⚠️ No encontré la cédula *{texto}* en Contactados. No se marcó nada.")
        return
    nombre_txt = f" ({nombre})" if nombre else ""
    # Aviso público en el canal de seguimiento (quién marcó qué)
    try:
        client.chat_postMessage(
            channel=CANAL_SEGUIMIENTO,
            text=f"{emoji} <@{usuario}> marcó como *{estado}* la promesa de la cédula *{texto}*{nombre_txt} — {marcadas} registro(s) actualizado(s)."
        )
    except Exception as e:
        print(f"⚠️ No se pudo avisar en el canal de seguimiento: {e}")
    # Confirmación a quien ejecutó
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"{emoji} Listo. Marqué {marcadas} registro(s) de la cédula {texto} como {estado}.")


@app.command("/promesa-cumplida")
def promesa_cumplida(ack, body, client):
    _comando_marcar(ack, body, client, "Cumplida", "✅")


@app.command("/promesa-fallida")
def promesa_fallida(ack, body, client):
    _comando_marcar(ack, body, client, "Fallida", "❌")
# ============ FIN FASE 2 ============


# ============ CIERRE DIARIO DE COBROS (reporte automático 6 PM) ============


def generar_cierre_diario():
    """Lee 'Pagos Recibidos', suma los cobros de HOY y publica el cierre del día."""
    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        try:
            hoja = spreadsheet.worksheet("Pagos Recibidos")
        except Exception:
            print("❌ Cierre: no se encontró la hoja 'Pagos Recibidos'")
            return
        valores = hoja.get_all_values()
        hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

        # Columnas (0-based): 0 fecha, 4 monto_bs, 7 monto_usd, 9 cobrador
        total_usd = 0.0
        total_bs = 0.0
        cantidad = 0
        por_cobrador = {}  # nombre -> {"n": x, "usd": x, "bs": x}

        for i, fila in enumerate(valores):
            if i == 0:
                continue

            def celda(idx):
                return fila[idx].strip() if len(fila) > idx else ""

            fecha_celda = celda(0).split()[0] if celda(0) else ""  # quita hora si la hubiera
            if fecha_celda != hoy_txt:
                continue

            cantidad += 1
            cobrador_raw = celda(9) or "Sin cobrador"
            # Normalizar: ignorar mayúsculas/minúsculas y espacios extra (REBECA = rebeca = Rebeca)
            clave = " ".join(cobrador_raw.split()).upper()
            nombre_bonito = " ".join(cobrador_raw.split()).title()

            try:
                usd = parse_numero(celda(7))
            except (ValueError, ZeroDivisionError):
                usd = 0.0
            try:
                bs = parse_numero(celda(4))
            except (ValueError, ZeroDivisionError):
                bs = 0.0

            total_usd += usd
            total_bs += bs
            g = por_cobrador.setdefault(clave, {"nombre": nombre_bonito, "n": 0, "usd": 0.0, "bs": 0.0})
            g["n"] += 1
            g["usd"] += usd
            g["bs"] += bs

        # Armar el mensaje
        lineas = [f"📊 *CIERRE DEL DÍA — {hoy_txt}*", ""]
        if cantidad == 0:
            lineas.append("No se registraron cobros hoy.")
        else:
            lineas.append(f"💰 *Total cobrado:* Bs. {total_bs:,.2f}  ·  ${total_usd:,.2f}")
            lineas.append(f"📝 *Cantidad de cobros:* {cantidad}")
            lineas.append("")
            lineas.append("*Por cobrador:*")
            for clave in sorted(por_cobrador.keys(), key=lambda x: por_cobrador[x]["usd"], reverse=True):
                g = por_cobrador[clave]
                lineas.append(f"   • {g['nombre']} — {g['n']} cobro(s) — ${g['usd']:,.2f}")

        mensaje = "\n".join(lineas)
        app.client.chat_postMessage(channel=CANAL_CIERRE, text=mensaje)
        print(f"✅ Cierre diario publicado: {cantidad} cobros, ${total_usd:,.2f}")
    except Exception as e:
        print(f"❌ Error generando el cierre diario: {type(e).__name__}: {e}")


# Comando manual para probar el cierre sin esperar las 6 PM
@app.command("/probar-cierre")
def probar_cierre(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando el cierre del día ahora mismo... revisa el canal #cobranzas-log."
    )
    generar_cierre_diario()
# ============ FIN CIERRE DIARIO DE COBROS ============


# ============ COMANDO /mis-promesas (con botones para marcar) ============
MIS_PROMESAS_LIMITE = 15  # cuántas promesas mostrar con botones a la vez


def _cobrador_por_slack_id(user_id):
    """Busca a qué nombre de cobrador corresponde el usuario de Slack que escribe."""
    for nombre, ids in COBRADOR_SLACK_IDS.items():
        if user_id in ids:
            return nombre
    return None


def _boton_marcar(cedula, nombre, estado):
    """Crea un botón con confirmación nativa para marcar una promesa."""
    emoji = "✅" if estado == "Cumplida" else "❌"
    estilo = "primary" if estado == "Cumplida" else "danger"
    valor = json.dumps({"c": cedula, "e": estado, "n": nombre[:60]})
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": f"{emoji} {estado}"},
        "style": estilo,
        "action_id": f"marcar_{estado.lower()}_btn",
        "value": valor,
        "confirm": {
            "title": {"type": "plain_text", "text": "¿Confirmar?"},
            "text": {"type": "mrkdwn", "text": f"Marcar la promesa de *{nombre}* (cédula {cedula}) como *{estado}*?"},
            "confirm": {"type": "plain_text", "text": "Sí, marcar"},
            "deny": {"type": "plain_text", "text": "Cancelar"}
        }
    }


@app.command("/mis-promesas")
def mis_promesas(ack, body, client):
    ack()
    canal = body["channel_id"]
    usuario = body["user_id"]

    nombre_cobrador = _cobrador_por_slack_id(usuario)
    if not nombre_cobrador:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text="No te reconozco como cobrador en la lista. Pídele al administrador que agregue tu ID de Slack.")
        return

    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        hoja = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == "contactados":
                hoja = ws
                break
        if hoja is None:
            client.chat_postEphemeral(channel=canal, user=usuario, text="❌ No encontré la hoja 'Contactados'.")
            return
        valores = hoja.get_all_values()
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al leer las promesas: {type(e).__name__}: {e}")
        return

    hoy = datetime.now(ZoneInfo("America/Caracas")).date()
    objetivo = nombre_cobrador.strip().upper()
    de_hoy = []
    vencidas = []
    vistas = set()

    for i, fila in enumerate(valores):
        if i == 0:
            continue

        def celda(idx):
            return fila[idx].strip() if len(fila) > idx else ""

        if celda(5).strip().upper() != objetivo:
            continue
        if celda(7):  # ya tiene estado
            continue
        f, _motivo = _parsear_fecha_radar(celda(4), hoy)
        if f is None:
            continue
        ced = _solo_digitos(celda(3))
        if ced and ced in vistas:
            continue
        if ced:
            vistas.add(ced)
        item = {"nombre": celda(1) or "(sin nombre)", "cedula": celda(3), "fecha": f}
        if f == hoy:
            de_hoy.append(item)
        elif f < hoy:
            vencidas.append(item)

    total = len(de_hoy) + len(vencidas)
    if total == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"✅ *{nombre_cobrador}*, no tienes promesas pendientes ni vencidas. ¡Estás al día!")
        return

    # Ordenar vencidas: más recientes primero
    vencidas.sort(key=lambda x: x["fecha"], reverse=True)
    # Combinar: primero las de hoy, luego vencidas
    lista = [("hoy", it) for it in de_hoy] + [("venc", it) for it in vencidas]

    bloques = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"📋 *Tus promesas pendientes — {nombre_cobrador}* ({total})\nMarca cada una con los botones. Se te pedirá confirmar."}
    }, {"type": "divider"}]

    mostradas = 0
    for tipo, it in lista:
        if mostradas >= MIS_PROMESAS_LIMITE:
            break
        ced = f" · {it['cedula']}" if it["cedula"] else ""
        if tipo == "hoy":
            etiqueta = "☀️ hoy"
        else:
            dias = (hoy - it["fecha"]).days
            alerta = " 🔴" if dias >= 3 else ""
            etiqueta = f"⏰ hace {dias}d{alerta}"
        bloques.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"• *{it['nombre']}*{ced} — {etiqueta}"}
        })
        bloques.append({
            "type": "actions",
            "elements": [
                _boton_marcar(it["cedula"], it["nombre"], "Cumplida"),
                _boton_marcar(it["cedula"], it["nombre"], "Fallida"),
            ]
        })
        mostradas += 1

    if total > mostradas:
        bloques.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"… y {total - mostradas} más. Marca estas y vuelve a escribir `/mis-promesas` para ver las siguientes."}]
        })

    client.chat_postEphemeral(channel=canal, user=usuario, blocks=bloques,
                              text=f"Tus promesas pendientes ({total})")


def _procesar_boton_marcar(ack, body, action, client, estado):
    ack()
    usuario = body["user"]["id"]
    canal = body["channel"]["id"]
    try:
        data = json.loads(action["value"])
    except Exception:
        data = {}
    cedula = data.get("c", "")
    nombre = data.get("n", "")
    try:
        marcadas, _n = _marcar_promesa(cedula, estado)
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al marcar: {type(e).__name__}: {e}")
        return
    emoji = "✅" if estado == "Cumplida" else "❌"
    if marcadas == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"⚠️ No encontré la cédula {cedula} en Contactados.")
        return
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"{emoji} Marcaste a *{nombre}* ({cedula}) como *{estado}*. ({marcadas} registro/s)")
    # Aviso público en el canal de seguimiento
    try:
        client.chat_postMessage(channel=CANAL_SEGUIMIENTO,
            text=f"{emoji} <@{usuario}> marcó como *{estado}* la promesa de {nombre} ({cedula}).")
    except Exception as e:
        print(f"⚠️ No se pudo avisar en seguimiento: {e}")


@app.action("marcar_cumplida_btn")
def marcar_cumplida_btn(ack, body, action, client):
    _procesar_boton_marcar(ack, body, action, client, "Cumplida")


@app.action("marcar_fallida_btn")
def marcar_fallida_btn(ack, body, action, client):
    _procesar_boton_marcar(ack, body, action, client, "Fallida")
# ============ FIN COMANDO /mis-promesas ============


# ============ TASA DEL DÍA (una vez al día) — BLINDADO ============
# Se guarda en la pestaña "Indicadores": A20=etiqueta, B20=valor, C20=fecha
FILA_TASA = 20
TASA_MIN = 1            # una tasa por debajo de 1 Bs/USD es imposible
TASA_MAX = 100_000_000  # tope de seguridad para atrapar tipeos absurdos
TASA_CAMBIO_ALERTA = 0.5  # avisa si la nueva tasa cambia más de 50% vs la anterior


def _abrir_indicadores():
    """Abre la hoja 'Indicadores'. Devuelve None si hay cualquier problema (nunca lanza error)."""
    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == PESTANA_INDICADORES.lower():
                return ws
        print(f"⚠️ Tasa: no se encontró la hoja '{PESTANA_INDICADORES}'")
        return None
    except Exception as e:
        print(f"⚠️ Tasa: error abriendo 'Indicadores': {type(e).__name__}: {e}")
        return None


def _guardar_tasa_dia(valor_num):
    """Guarda la tasa de HOY (etiqueta + valor + fecha) en una sola operación. Devuelve True/False.
    También la deja registrada en el historial (pestaña 'Historial Tasas'), para poder
    consultarla más adelante si alguien reporta un pago atrasado de este mismo día."""
    ws = _abrir_indicadores()
    if ws is None:
        return False
    hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    try:
        # Una sola escritura (A20:C20) para evitar quedar a medias
        ws.update(f"A{FILA_TASA}:C{FILA_TASA}", [["Tasa del día", str(valor_num), hoy_txt]])
        _guardar_en_historial_tasas(hoy_txt, valor_num)
        return True
    except Exception as e:
        print(f"❌ Tasa: error guardando: {type(e).__name__}: {e}")
        return False


def _abrir_historial_tasas():
    """Abre la pestaña de historial de tasas (mismo Sheet que 'Indicadores'). None si hay problema."""
    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == PESTANA_HISTORIAL_TASAS.lower():
                return ws
        print(f"⚠️ Historial Tasas: no se encontró la pestaña '{PESTANA_HISTORIAL_TASAS}'")
        return None
    except Exception as e:
        print(f"⚠️ Historial Tasas: error abriendo la pestaña: {type(e).__name__}: {e}")
        return None


def _buscar_columnas_historial_tasas(ws):
    """Ubica por NOMBRE (no por posición) las columnas 'Fecha' y 'Tasa' del historial.
    Devuelve (col_fecha, col_tasa) como índices base 0, o (None, None) si faltan."""
    encabezados = [_normalizar_encabezado(c) for c in ws.row_values(1)]
    col_fecha = encabezados.index("fecha") if "fecha" in encabezados else None
    col_tasa = encabezados.index("tasa") if "tasa" in encabezados else None
    return col_fecha, col_tasa


def _guardar_en_historial_tasas(fecha_str, valor_num):
    """Guarda o actualiza la tasa de 'fecha_str' en el historial (una fila por fecha),
    colocando cada dato en la columna que le corresponde POR NOMBRE (Fecha, Tasa), no por
    posición — igual que el resto del bot desde la Fase 2 de sostenibilidad.
    No lanza error nunca — si el historial no existe todavía, simplemente no hace nada."""
    ws = _abrir_historial_tasas()
    if ws is None:
        return False
    try:
        col_fecha, col_tasa = _buscar_columnas_historial_tasas(ws)
        if col_fecha is None or col_tasa is None:
            print(f"⚠️ Historial Tasas: faltan las columnas 'Fecha' y/o 'Tasa' en la pestaña '{PESTANA_HISTORIAL_TASAS}'. "
                  f"Encabezados leídos (fila 1): {ws.row_values(1)!r}")
            return False
        valores = ws.get_all_values()
        for i, fila in enumerate(valores[1:], start=2):  # fila 1 = encabezados
            if len(fila) > col_fecha and str(fila[col_fecha]).strip() == fecha_str:
                ws.update_cell(i, col_tasa + 1, str(valor_num))  # gspread usa columnas base 1
                return True
        _guardar_fila_por_encabezado(ws, {"Fecha": fecha_str, "Tasa": str(valor_num)})
        return True
    except Exception as e:
        print(f"❌ Historial Tasas: error guardando: {type(e).__name__}: {e}")
        return False


def _tasa_de_fecha(fecha_str):
    """Devuelve el número de la tasa registrada para 'fecha_str' en el historial, o None
    si no hay ninguna registrada o no es válida. Busca la columna por nombre, no por
    posición. Nunca lanza error."""
    ws = _abrir_historial_tasas()
    if ws is None:
        return None
    try:
        col_fecha, col_tasa = _buscar_columnas_historial_tasas(ws)
        if col_fecha is None or col_tasa is None:
            print(f"⚠️ Historial Tasas: faltan las columnas 'Fecha' y/o 'Tasa' en la pestaña '{PESTANA_HISTORIAL_TASAS}'. "
                  f"Encabezados leídos (fila 1): {ws.row_values(1)!r}")
            return None
        valores = ws.get_all_values()
        for fila in valores[1:]:
            if len(fila) > col_fecha and str(fila[col_fecha]).strip() == fecha_str:
                if len(fila) <= col_tasa or not fila[col_tasa]:
                    return None
                num = parse_numero(fila[col_tasa])
                if num is None or num < TASA_MIN or num > TASA_MAX:
                    return None
                return num
        return None
    except Exception as e:
        print(f"⚠️ Historial Tasas: error leyendo: {type(e).__name__}: {e}")
        return None


def _tasa_de_pago(fecha_pago, hoy_txt):
    """Devuelve la tasa aplicable para un pago del día 'fecha_pago'.
    Si es de hoy, usa la tasa de hoy (Indicadores). Si es de otro día, la busca en el
    historial. Devuelve None si no hay ninguna tasa válida para esa fecha."""
    if fecha_pago == hoy_txt:
        return _tasa_de_hoy()
    return _tasa_de_fecha(fecha_pago)


def _leer_tasa_dia():
    """Devuelve (valor_str, fecha_str) o (None, None). Nunca lanza error."""
    ws = _abrir_indicadores()
    if ws is None:
        return None, None
    try:
        valor = ws.cell(FILA_TASA, 2).value
        fecha = ws.cell(FILA_TASA, 3).value
        return valor, fecha
    except Exception as e:
        print(f"⚠️ Tasa: error leyendo: {type(e).__name__}: {e}")
        return None, None


def _tasa_de_hoy():
    """Devuelve el número de la tasa SOLO si es de hoy y válida; si no, None. Nunca lanza error."""
    try:
        valor, fecha = _leer_tasa_dia()
        if not valor or not fecha:
            return None
        hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
        if str(fecha).strip().split()[0] != hoy_txt:
            return None
        num = parse_numero(valor)
        if num is None or num < TASA_MIN or num > TASA_MAX:
            return None
        return num
    except Exception as e:
        print(f"⚠️ Tasa: error en _tasa_de_hoy: {type(e).__name__}: {e}")
        return None


@app.command("/tasa-hoy")
def tasa_hoy(ack, body, client):
    ack()
    texto = (body.get("text") or "").strip()
    canal = body["channel_id"]
    usuario = body["user_id"]
    hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    # ---- Sin número: consultar la tasa actual (de hoy) ----
    if not texto:
        try:
            valor, fecha = _leer_tasa_dia()
            if not valor:
                client.chat_postEphemeral(channel=canal, user=usuario,
                    text="No hay tasa registrada aún. Fíjala con `/tasa-hoy 520,50`.")
            else:
                vigente = "✅ (es de hoy)" if str(fecha).strip().split()[0] == hoy_txt else "⚠️ (NO es de hoy, hay que ponerla de nuevo)"
                client.chat_postEphemeral(channel=canal, user=usuario,
                    text=f"La tasa registrada es *Bs. {valor}* por USD (puesta el {fecha}) {vigente}.")
        except Exception as e:
            client.chat_postEphemeral(channel=canal, user=usuario,
                text=f"❌ No pude leer la tasa ahora mismo. Intenta de nuevo en un momento.")
            print(f"❌ /tasa-hoy consulta: {type(e).__name__}: {e}")
        return

    # ---- Con número: fijar una tasa. Puede venir como "520,50" (hoy) o "03/08/2026 148,90" (un día pasado) ----
    partes = texto.split()
    fecha_arg = None
    valor_arg = partes[0]
    if len(partes) >= 2 and "/" in partes[0]:
        fecha_arg = partes[0]
        valor_arg = partes[1]
        _ok_fecha, _msg_fecha = _es_fecha_valida(fecha_arg)
        if not _ok_fecha:
            client.chat_postEphemeral(channel=canal, user=usuario,
                text=f"'{fecha_arg}' no es una fecha válida. {_msg_fecha} Ejemplo: `/tasa-hoy 03/08/2026 148,90`.")
            return

    try:
        valor_num = parse_numero(valor_arg)
    except (ValueError, ZeroDivisionError, TypeError):
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"'{valor_arg}' no es una tasa válida. Ejemplo: `/tasa-hoy 520,50` o `/tasa-hoy 03/08/2026 148,90` para un día pasado.")
        return

    # Blindaje de rango: atrapa 0, negativos y valores absurdos por tipeo
    if valor_num < TASA_MIN or valor_num > TASA_MAX:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"⚠️ La tasa *{valor_num:,.4f}* parece un error de tipeo (fuera de un rango razonable). Revisa y vuelve a intentar.")
        return

    fecha_destino = fecha_arg or hoy_txt

    # ---- Fecha de un día pasado: se guarda solo en el historial, sin tocar la tasa vigente de hoy ----
    if fecha_destino != hoy_txt:
        if not _guardar_en_historial_tasas(fecha_destino, valor_num):
            client.chat_postEphemeral(channel=canal, user=usuario,
                text=(f"❌ No pude guardar la tasa del {fecha_destino} (revisa que exista la pestaña "
                      f"'{PESTANA_HISTORIAL_TASAS}' con columnas 'Fecha' y 'Tasa'). Intenta de nuevo."))
            return
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=(f"✅ Tasa fijada para el *{fecha_destino}*: Bs. {valor_num:,.4f} por USD. "
                  f"Ya pueden reportar con /cobro los pagos atrasados de ese día."))
        return

    # ---- Fecha de hoy (con o sin escribirla explícitamente): fija la tasa vigente, como siempre ----
    # Aviso si cambia demasiado respecto a la tasa anterior (posible tipeo tipo 52050)
    aviso_cambio = ""
    try:
        valor_ant, _fecha_ant = _leer_tasa_dia()
        if valor_ant:
            ant = parse_numero(valor_ant)
            if ant and ant > 0:
                cambio = abs(valor_num - ant) / ant
                if cambio > TASA_CAMBIO_ALERTA:
                    aviso_cambio = (f"\n\n⚠️ *Ojo:* la tasa anterior era Bs. {ant:,.4f} y ahora pusiste Bs. {valor_num:,.4f} "
                                    f"(un cambio grande). Si fue un error, vuelve a ponerla correcta.")
    except Exception:
        pass  # el aviso es opcional, no debe romper nada

    if not _guardar_tasa_dia(valor_num):
        client.chat_postEphemeral(channel=canal, user=usuario,
            text="❌ No pude guardar la tasa (revisa que exista la hoja 'Indicadores'). Intenta de nuevo.")
        return

    try:
        client.chat_postMessage(channel="#cobranzas-log",
            text=f"💱 <@{usuario}> fijó la *tasa del día*: Bs. {valor_num:,.4f} por USD ({hoy_txt}). Ya pueden reportar cobros.")
    except Exception as e:
        print(f"⚠️ No se pudo avisar la tasa en el canal: {e}")
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"✅ Tasa del día fijada: Bs. {valor_num:,.4f} por USD.{aviso_cambio}")
# ============ FIN TASA DEL DÍA ============


# ============ MÓDULO DE MERCADEO (Conciliación de Pagos e Incidencias Técnicas) ============
def _abrir_hoja_mercadeo(nombre_pestana):
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_MERCADEO)
    return spreadsheet.worksheet(nombre_pestana)


def guardar_conciliacion_mercadeo(fecha_reporte, nombre_colaborador, telefono, cedula, monto_bs, forma_pago,
                                   banco, fecha_pago, monto_usd, tasa_bcv, referencia, estado, revisor,
                                   fecha_revision, registro_id):
    try:
        sheet = _abrir_hoja_mercadeo("Conciliacion")
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Conciliación de Mercadeo duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        datos = {
            "Fecha de Reporte": fecha_reporte,
            "Nombre de Colaborador": nombre_colaborador,
            "Telefono": telefono,
            "Cedula": cedula,
            "Monto en Bs": monto_bs,
            "Forma de Pago": forma_pago,
            "Banco de Origen": banco,
            "Fecha de Pago": fecha_pago,
            "Monto en USD": monto_usd,
            "Tasa BCV Aplicada": tasa_bcv,
            "Numero de Referencia": referencia,
            "Comprobante": "",
            "Estado": estado,
            "Revisado por": revisor,
            "Fecha de Revision": fecha_revision,
            "ID Registro": registro_id,
        }
        _guardar_fila_por_encabezado(sheet, datos)
        print(f"✅ Conciliación de Mercadeo guardada: {nombre_colaborador} ({cedula})")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando conciliación de Mercadeo: {type(e).__name__}: {e}")
        return "ERROR"


def guardar_incidencia_mercadeo(fecha_reporte, nombre, cedula, empresa, incidencia, descripcion,
                                 estado, revisor, fecha_revision, registro_id):
    try:
        sheet = _abrir_hoja_mercadeo("Incidencias Tecnicas")
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Incidencia técnica duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        datos = {
            "Fecha": fecha_reporte,
            "Nombre": nombre,
            "Cedula": cedula,
            "Empresa": empresa,
            "Incidencias": incidencia,
            "Descripcion": descripcion,
            "Estado": estado,
            "Revisado por": revisor,
            "Fecha de Revision": fecha_revision,
            "ID Registro": registro_id,
        }
        _guardar_fila_por_encabezado(sheet, datos)
        print(f"✅ Incidencia técnica guardada: {nombre} ({cedula})")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando incidencia técnica: {type(e).__name__}: {e}")
        return "ERROR"


_BANCOS_MERCADEO = [
    {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
    {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
    {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
    {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
    {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
    {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
    {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
    {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
]

_FORMAS_PAGO_MERCADEO = [
    {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Móvil"},
    {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
    {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
    {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
    {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
]

_INCIDENCIAS_MERCADEO = [
    {"text": {"type": "plain_text", "text": "No accede"}, "value": "No accede"},
    {"text": {"type": "plain_text", "text": "Falla al pagar"}, "value": "Falla al pagar"},
    {"text": {"type": "plain_text", "text": "Crash"}, "value": "Crash"},
    {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
]


def _vista_form_conciliacion_mercadeo():
    return {
        "type": "modal",
        "callback_id": "form_merca_conciliacion",
        "title": {"type": "plain_text", "text": "Conciliación de Pago"},
        "submit": {"type": "plain_text", "text": "Enviar"},
        "blocks": [
            {"type": "input", "block_id": "nombre_colaborador",
             "label": {"type": "plain_text", "text": "Nombre de Colaborador"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "telefono",
             "label": {"type": "plain_text", "text": "Teléfono"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "cedula",
             "label": {"type": "plain_text", "text": "Cédula"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "monto_bs",
             "label": {"type": "plain_text", "text": "Monto en Bs"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "forma_pago",
             "label": {"type": "plain_text", "text": "Forma de Pago"},
             "element": {"type": "static_select", "action_id": "valor",
                         "placeholder": {"type": "plain_text", "text": "Selecciona"},
                         "options": _FORMAS_PAGO_MERCADEO}},
            {"type": "input", "block_id": "banco",
             "label": {"type": "plain_text", "text": "Banco de Origen"},
             "element": {"type": "static_select", "action_id": "valor",
                         "placeholder": {"type": "plain_text", "text": "Selecciona"},
                         "options": _BANCOS_MERCADEO}},
            {"type": "input", "block_id": "fecha_pago",
             "label": {"type": "plain_text", "text": "Fecha de Pago (DD/MM/AAAA)"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "monto_usd",
             "label": {"type": "plain_text", "text": "Monto en USD"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "tasa_bcv",
             "label": {"type": "plain_text", "text": "Tasa BCV Aplicada"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "referencia",
             "label": {"type": "plain_text", "text": "Número de Referencia"},
             "element": {"type": "plain_text_input", "action_id": "valor"}}
        ]
    }


def _vista_form_incidencia_mercadeo():
    return {
        "type": "modal",
        "callback_id": "form_merca_incidencia",
        "title": {"type": "plain_text", "text": "Incidencia Técnica"},
        "submit": {"type": "plain_text", "text": "Enviar"},
        "blocks": [
            {"type": "input", "block_id": "nombre",
             "label": {"type": "plain_text", "text": "Nombre"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "cedula",
             "label": {"type": "plain_text", "text": "Cédula"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "empresa",
             "label": {"type": "plain_text", "text": "Empresa"},
             "element": {"type": "plain_text_input", "action_id": "valor"}},
            {"type": "input", "block_id": "incidencia",
             "label": {"type": "plain_text", "text": "Incidencia"},
             "element": {"type": "static_select", "action_id": "valor",
                         "placeholder": {"type": "plain_text", "text": "Selecciona"},
                         "options": _INCIDENCIAS_MERCADEO}},
            {"type": "input", "block_id": "descripcion",
             "label": {"type": "plain_text", "text": "Descripción"},
             "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}}
        ]
    }


@app.command("/merca-reporte")
def abrir_selector_mercadeo(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_merca_tipo",
            "title": {"type": "plain_text", "text": "Reporte de Mercadeo"},
            "submit": {"type": "plain_text", "text": "Continuar"},
            "blocks": [
                {"type": "input", "block_id": "tipo_caso",
                 "label": {"type": "plain_text", "text": "¿Qué tipo de caso deseas registrar?"},
                 "element": {"type": "static_select", "action_id": "valor",
                             "placeholder": {"type": "plain_text", "text": "Selecciona"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "Conciliación de Pago"}, "value": "conciliacion"},
                                 {"text": {"type": "plain_text", "text": "Incidencia Técnica / Acceso App"}, "value": "incidencia"}
                             ]}}
            ]
        }
    )


@app.view("form_merca_tipo")
def elegir_tipo_mercadeo(ack, body):
    tipo = body["view"]["state"]["values"]["tipo_caso"]["valor"]["selected_option"]["value"]
    if tipo == "conciliacion":
        ack(response_action="update", view=_vista_form_conciliacion_mercadeo())
    else:
        ack(response_action="update", view=_vista_form_incidencia_mercadeo())


@app.view("form_merca_conciliacion")
def recibir_conciliacion_mercadeo(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('telefono', 'telefono'), ('cedula', 'cedula'), ('fecha_pago', 'fecha')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre_colaborador = valores["nombre_colaborador"]["valor"]["value"]
    telefono = valores["telefono"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    fecha_pago = valores["fecha_pago"]["valor"]["value"]
    monto_usd_str = valores["monto_usd"]["valor"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    referencia = valores["referencia"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha_reporte = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    try:
        monto_bs_fmt = f"Bs. {parse_numero(monto_bs_str):,.2f}"
    except (ValueError, AttributeError):
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    try:
        monto_usd_fmt = f"$ {parse_numero(monto_usd_str):,.2f}"
    except (ValueError, AttributeError):
        monto_usd_fmt = f"$ {monto_usd_str}"
    try:
        tasa_bcv_fmt = f"Bs. {parse_numero(tasa_bcv_str):,.4f}"
    except (ValueError, AttributeError):
        tasa_bcv_fmt = f"Bs. {tasa_bcv_str}"

    texto = (
        f"*Nueva conciliación de pago (Mercadeo)* 🧾\n"
        f"*Fecha de Reporte:* {fecha_reporte}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Colaborador:* {nombre_colaborador}\n"
        f"*Teléfono:* {telefono}\n"
        f"*Cédula:* {cedula}\n"
        f"*Monto en Bs:* {monto_bs_fmt}\n"
        f"*Forma de Pago:* {forma_pago}\n"
        f"*Banco de Origen:* {banco}\n"
        f"*Fecha de Pago:* {fecha_pago}\n"
        f"*Monto en USD:* {monto_usd_fmt}\n"
        f"*Tasa BCV Aplicada:* {tasa_bcv_fmt}\n"
        f"*Número de Referencia:* {referencia}"
    )
    try:
        client.chat_postMessage(
            channel=CANAL_MERCADEO_PAGOS,
            text="Nueva conciliación de pago (Mercadeo)",
            metadata={"event_type": "merca_conciliacion", "event_payload": {
                "fecha_reporte": fecha_reporte, "nombre_colaborador": nombre_colaborador, "telefono": telefono,
                "cedula": cedula, "monto_bs": monto_bs_fmt, "forma_pago": forma_pago, "banco": banco,
                "fecha_pago": fecha_pago, "monto_usd": monto_usd_fmt, "tasa_bcv": tasa_bcv_fmt,
                "referencia": referencia}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_merca_conciliacion"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_merca_conciliacion"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de mercadeo-pagos: {e}")


@app.action("aprobar_merca_conciliacion")
def aprobar_merca_conciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("MERCACONC", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_conciliacion_mercadeo(
            meta.get("fecha_reporte", fecha_revision), meta.get("nombre_colaborador", ""), meta.get("telefono", ""),
            meta.get("cedula", ""), meta.get("monto_bs", ""), meta.get("forma_pago", ""), meta.get("banco", ""),
            meta.get("fecha_pago", ""), meta.get("monto_usd", ""), meta.get("tasa_bcv", ""), meta.get("referencia", ""),
            "Aprobado", body["user"]["id"], fecha_revision, registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta conciliación ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación de Mercadeo procesada",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_merca_conciliacion")
def rechazar_merca_conciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación de Mercadeo RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.view("form_merca_incidencia")
def recibir_incidencia_mercadeo(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('cedula', 'cedula')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    empresa = valores["empresa"]["valor"]["value"]
    incidencia = valores["incidencia"]["valor"]["selected_option"]["value"]
    descripcion = valores["descripcion"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha_reporte = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    texto = (
        f"*Nueva incidencia técnica (Mercadeo)* 🛠️\n"
        f"*Fecha de Reporte:* {fecha_reporte}\n"
        f"*Reportado por:* <@{usuario_slack}>\n"
        f"*Nombre:* {nombre}\n"
        f"*Cédula:* {cedula}\n"
        f"*Empresa:* {empresa}\n"
        f"*Incidencia:* {incidencia}\n"
        f"*Descripción:* {descripcion}"
    )
    try:
        client.chat_postMessage(
            channel=CANAL_MERCADEO_INCIDENCIAS,
            text="Nueva incidencia técnica (Mercadeo)",
            metadata={"event_type": "merca_incidencia", "event_payload": {
                "fecha_reporte": fecha_reporte, "nombre": nombre, "cedula": cedula, "empresa": empresa,
                "incidencia": incidencia, "descripcion": descripcion}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar_merca_incidencia"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar_merca_incidencia"}
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar mensaje al canal de mercadeo-incidencias: {e}")


@app.action("aprobar_merca_incidencia")
def aprobar_merca_incidencia(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("MERCAINC", body["message"]["ts"])
    resultado = "ERROR"
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_incidencia_mercadeo(
            meta.get("fecha_reporte", fecha_revision), meta.get("nombre", ""), meta.get("cedula", ""),
            meta.get("empresa", ""), meta.get("incidencia", ""), meta.get("descripcion", ""),
            "Aprobado", body["user"]["id"], fecha_revision, registro_id)
    except Exception as e:
        print(f"Error: {e}")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta incidencia ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Incidencia técnica procesada",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_merca_incidencia")
def rechazar_merca_incidencia(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Incidencia técnica RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN MÓDULO DE MERCADEO ============


if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    # Programar el Radar de Promesas todos los días a las 4:00 PM (hora Venezuela)
    scheduler = BackgroundScheduler(timezone=ZoneInfo("America/Caracas"))
    scheduler.add_job(generar_resumen_promesas, "cron", hour=16, minute=0)
    scheduler.add_job(generar_cierre_diario, "cron", hour=18, minute=0)
    scheduler.start()
    print("⏰ Scheduler del Radar de Promesas activo (4:00 PM Venezuela).")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
