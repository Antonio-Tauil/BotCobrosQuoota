import os
import re
import json
import gspread
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler

app = App(token=os.environ["SLACK_BOT_TOKEN"])


# ============ FUNCIÓN PARA LEER NÚMEROS EN CUALQUIER FORMATO ============
def parse_numero(texto):
    if texto is None:
        raise ValueError("vacío")
    s = re.sub(r"[^0-9.,\-]", "", str(texto).strip())
    if s in ("", "-", ".", ","):
        raise ValueError("sin dígitos")
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


# ============ LISTA DE COBRADORES (compartida) ============
COBRADORES = ["DIEGO", "IARA", "REBECA", "MARIANGEL", "LUISMAR", "ANGELY", "DANIEL", "BARBARA", "MARIANA", "ANDRES"]


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
                {"type": "input", "block_id": "nombre_cobrador",
                 "label": {"type": "plain_text", "text": "Nombre del Cobrador"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}},
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
                             ]}},
                {"type": "input", "block_id": "tasa_bcv",
                 "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                 "element": {"type": "plain_text_input", "action_id": "valor"}}
            ]
        }
    )


@app.view("form_cobro")
def recibir_cobro(ack, body, client):
    ack()
    valores = body["view"]["state"]["values"]
    nombre_cobrador = valores["nombre_cobrador"]["valor"]["value"]
    descripcion = valores["descripcion"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    numero = valores["numero"]["valor"]["value"]
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    cobrador_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Domiciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /domiciliar ============


# ============ COMANDO /cobro-callcenter (Call Center Seguros) ============
SHEET_ID_COBRO2 = "1KbWx1d5ujGmNwjGbdb-c_QAwiEkxJpxLb1BOFOCY9QM"


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
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /conciliar ============


# ============ COMANDOS DE LIQUIDACIONES (Lista VIP) ============
SHEET_ID_LIQUIDACIONES = "1MYKQ-CnyMQBTEZcSBIXt-KDsBbfJt-tUmG-k5aZvDI0"
CANAL_LIQUIDACIONES = "C0BE1HLRV1R"

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
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"]
    cedula = valores["cedula"]["valor"]["value"]
    cliente_empresa = valores["cliente"]["valor"]["value"]
    base = valores["base"]["valor"]["selected_option"]["value"]
    estatus = valores["estatus"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    ack()
    valores = body["view"]["state"]["values"]
    cedula = valores["cedula"]["valor"]["value"]
    nombre = valores["nombre"]["valor"]["value"]
    estatus = valores["estatus"]["valor"]["selected_option"]["value"]
    usuario_slack = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cambio de estatus RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDOS DE LIQUIDACIONES ============


# ============ COMANDO /cobro-comercial (Equipo Comercial) ============
SHEET_ID_COMERCIAL = "1Zayi6aQPoSjDadbAozhLGJaO7dU-6p51dQ5SXnaU6mc"
CANAL_COMERCIAL = "C0BE5LJL729"


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
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /cobro-comercial ============


# ============ COMANDO /contacto-legal (Equipo Legal) ============
SHEET_ID_LEGAL = "1Zayi6aQPoSjDadbAozhLGJaO7dU-6p51dQ5SXnaU6mc"
CANAL_LEGAL = "C0BJYNVG5PW"


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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
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

# ============ RADAR DE PROMESAS DE PAGO (Fase 1) ============
CANAL_SEGUIMIENTO = "C0BJWPMA3NF"
SUPERVISOR_ID = "U0B51AREWDU"  # Leandro Quoota

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

            grupo = por_cobrador.setdefault(cobrador, {"hoy": [], "vencidas": []})
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


if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    # Programar el Radar de Promesas todos los días a las 4:00 PM (hora Venezuela)
    scheduler = BackgroundScheduler(timezone=ZoneInfo("America/Caracas"))
    scheduler.add_job(generar_resumen_promesas, "cron", hour=16, minute=0)
    scheduler.start()
    print("⏰ Scheduler del Radar de Promesas activo (4:00 PM Venezuela).")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
