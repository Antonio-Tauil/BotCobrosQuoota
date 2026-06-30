import os
import re
import json
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials

app = App(token=os.environ["SLACK_BOT_TOKEN"])


# ============ FUNCIÓN PARA LEER NÚMEROS EN CUALQUIER FORMATO ============
# Entiende: 12420  |  12420,53  |  12.420,53  |  12,420.53  |  587.4059  |  Bs. 1.500,00
def parse_numero(texto):
    if texto is None:
        raise ValueError("vacío")
    # Dejar solo dígitos, punto, coma y signo negativo (quita "Bs", "$", espacios, etc.)
    s = re.sub(r"[^0-9.,\-]", "", str(texto).strip())
    if s in ("", "-", ".", ","):
        raise ValueError("sin dígitos")
    tiene_punto = "." in s
    tiene_coma = "," in s
    if tiene_punto and tiene_coma:
        # El separador que esté más a la derecha es el decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")   # coma decimal (venezolano)
        else:
            s = s.replace(",", "")                     # punto decimal (gringo)
    elif tiene_coma:
        if s.count(",") > 1:
            s = s.replace(",", "")                     # varias comas = miles
        else:
            _, _, dec = s.partition(",")
            s = s.replace(",", "") if len(dec) == 3 else s.replace(",", ".")
    elif tiene_punto:
        if s.count(".") > 1:
            s = s.replace(".", "")                     # varios puntos = miles
        else:
            _, _, dec = s.partition(".")
            if len(dec) == 3:
                s = s.replace(".", "")                 # 3 dígitos = probablemente miles
            # si no, se deja el punto como decimal
    return float(s)
# ============ FIN FUNCIÓN parse_numero ============


# ============ NUEVO COMANDO /contactar ============
# Función para guardar en hoja "Contactados"
def guardar_en_contactados(fecha, nombre, telefono, cedula, compromiso, cobrador, comentario):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        # Buscar la hoja "Contactados" tolerando mayúsculas/espacios
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
                {
                    "type": "input",
                    "block_id": "nombre",
                    "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "telefono",
                    "label": {"type": "plain_text", "text": "Teléfono"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "compromiso",
                    "label": {"type": "plain_text", "text": "Compromiso de pago (DD/MM/YYYY)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cobrador",
                    "label": {"type": "plain_text", "text": "Cobrador"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "DIEGO"}, "value": "DIEGO"},
                            {"text": {"type": "plain_text", "text": "IARA"}, "value": "IARA"},
                            {"text": {"type": "plain_text", "text": "REBECA"}, "value": "REBECA"},
                            {"text": {"type": "plain_text", "text": "MARIANGEL"}, "value": "MARIANGEL"},
                            {"text": {"type": "plain_text", "text": "LUISMAR"}, "value": "LUISMAR"},
                            {"text": {"type": "plain_text", "text": "ANGELY"}, "value": "ANGELY"},
                            {"text": {"type": "plain_text", "text": "DANIEL"}, "value": "DANIEL"},
                            {"text": {"type": "plain_text", "text": "BARBARA"}, "value": "BARBARA"}
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "comentario",
                    "label": {"type": "plain_text", "text": "Comentario"},
                    "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}
                }
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
    # Guardar directamente en la hoja
    guardar_en_contactados(fecha, nombre, telefono, cedula, compromiso, cobrador, comentario)
    # Enviar mensaje al canal
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


# Función para guardar en Google Sheets
def guardar_en_sheet(fecha, cobrador, descripcion, numero, cedula, monto_bs, forma_pago, banco, tasa_bcv, monto_usd):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        cliente = gspread.authorize(creds)
        sheet = cliente.open_by_key(os.environ["SHEET_ID"]).worksheet("Pagos Recibidos")
        sheet.append_row([fecha, descripcion, numero, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, cobrador])
        print("✅ Cobro guardado en Google Sheets")
    except Exception as e:
        print(f"❌ Error guardando en sheet: {e}")


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
                {
                    "type": "input",
                    "block_id": "nombre_cobrador",
                    "label": {"type": "plain_text", "text": "Nombre del Cobrador"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "descripcion",
                    "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "numero",
                    "label": {"type": "plain_text", "text": "Teléfono o Referencia"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "monto_bs",
                    "label": {"type": "plain_text", "text": "Monto en Bs"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "forma_pago",
                    "label": {"type": "plain_text", "text": "Forma de Pago"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
                            {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
                            {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
                            {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
                            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "banco",
                    "label": {"type": "plain_text", "text": "Banco"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
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
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "tasa_bcv",
                    "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                }
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
        metadata={
            "event_type": "cobro_reportado",
            "event_payload": {
                "fecha": fecha,
                "cobrador": nombre_cobrador,
                "descripcion": descripcion,
                "numero": numero,
                "cedula": cedula,
                "monto_bs": monto_bs_fmt,
                "forma_pago": forma_pago,
                "banco": banco,
                "tasa_bcv": tasa_bcv_str,
                "monto_usd": monto_usd_str
            }
        },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_sheet(
            meta.get("fecha", fecha_revision),
            meta.get("cobrador", body["user"]["id"]),
            meta.get("descripcion", ""),
            meta.get("numero", ""),
            meta.get("cedula", ""),
            meta.get("monto_bs", ""),
            meta.get("forma_pago", ""),
            meta.get("banco", ""),
            meta.get("tasa_bcv", ""),
            meta.get("monto_usd", "")
        )
    except Exception as e:
        print(f"Error: {e}")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cobro APROBADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.action("rechazar")
def rechazar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


# ============ COMANDO /domiciliar ============
# Función para guardar en hoja "Domiciliación"
def guardar_en_domiciliacion(fecha, empresa, cuenta, monto_bs, banco, monto_usd, tasa_bcv, cobrador):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        # Buscar la hoja "Domiciliación" tolerando mayúsculas, espacios y acento
        sheet = None
        for ws in spreadsheet.worksheets():
            titulo = ws.title.strip().lower()
            if titulo in ("domiciliación", "domiciliacion"):
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Domiciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return
        sheet.append_row([fecha, empresa, cuenta, monto_bs, banco, monto_usd, tasa_bcv, cobrador])
        print(f"✅ Domiciliación guardada en hoja '{sheet.title}'")
    except Exception as e:
        print(f"❌ Error guardando en Domiciliación: {type(e).__name__}: {e}")


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
                {
                    "type": "input",
                    "block_id": "empresa",
                    "label": {"type": "plain_text", "text": "Empresa"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cuenta",
                    "label": {"type": "plain_text", "text": "Cuenta por cobrar"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "monto_bs",
                    "label": {"type": "plain_text", "text": "Monto en Bs"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "banco",
                    "label": {"type": "plain_text", "text": "Banco"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
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
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "tasa_bcv",
                    "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cobrador",
                    "label": {"type": "plain_text", "text": "Cobrador"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "DIEGO"}, "value": "DIEGO"},
                            {"text": {"type": "plain_text", "text": "IARA"}, "value": "IARA"},
                            {"text": {"type": "plain_text", "text": "REBECA"}, "value": "REBECA"},
                            {"text": {"type": "plain_text", "text": "MARIANGEL"}, "value": "MARIANGEL"},
                            {"text": {"type": "plain_text", "text": "LUISMAR"}, "value": "LUISMAR"},
                            {"text": {"type": "plain_text", "text": "ANGELY"}, "value": "ANGELY"},
                            {"text": {"type": "plain_text", "text": "DANIEL"}, "value": "DANIEL"},
                            {"text": {"type": "plain_text", "text": "BARBARA"}, "value": "BARBARA"}
                        ]
                    }
                }
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
    # Formatear "Cuenta por cobrar" como bolívares
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
            metadata={
                "event_type": "domiciliacion_reportada",
                "event_payload": {
                    "fecha": fecha,
                    "empresa": empresa,
                    "cuenta": cuenta_fmt,
                    "monto_bs": monto_bs_fmt,
                    "banco": banco,
                    "monto_usd": monto_usd_str,
                    "tasa_bcv": tasa_bcv_str,
                    "cobrador": cobrador
                }
            },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_domiciliacion(
            meta.get("fecha", fecha_revision),
            meta.get("empresa", ""),
            meta.get("cuenta", ""),
            meta.get("monto_bs", ""),
            meta.get("banco", ""),
            meta.get("monto_usd", ""),
            meta.get("tasa_bcv", ""),
            meta.get("cobrador", "")
        )
    except Exception as e:
        print(f"Error: {e}")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Domiciliación APROBADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.action("rechazar_domiciliacion")
def rechazar_domiciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Domiciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /domiciliar ============


# ============ COMANDO /cobro2 (Call Center Seguros) ============
# ID del Google Sheet nuevo "Seguimiento - Call Center Seguros Venezuela"
SHEET_ID_COBRO2 = "1KbWx1d5ujGmNwjGbdb-c_QAwiEkxJpxLb1BOFOCY9QM"


# Función para guardar en el Sheet del Call Center
def guardar_en_sheet_cobro2(fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, referencia):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(SHEET_ID_COBRO2)
        try:
            sheet = spreadsheet.worksheet("Hoja1")
        except Exception:
            sheet = spreadsheet.sheet1
        # Orden de columnas: Fecha, Nombre, Telefono, Cedula, MontoBs, FormaPago, Banco, MontoUsd, TasaBCV, referencia pago
        sheet.append_row([fecha, nombre, telefono, cedula, monto_bs, forma_pago, banco, monto_usd, tasa_bcv, referencia])
        print("✅ Cobro (Call Center) guardado en Google Sheets")
    except Exception as e:
        print(f"❌ Error guardando en sheet cobro2: {type(e).__name__}: {e}")


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
                {
                    "type": "input",
                    "block_id": "nombre",
                    "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "telefono",
                    "label": {"type": "plain_text", "text": "Teléfono"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "monto_bs",
                    "label": {"type": "plain_text", "text": "Monto en Bs"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "forma_pago",
                    "label": {"type": "plain_text", "text": "Forma de Pago"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
                            {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
                            {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
                            {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
                            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "banco",
                    "label": {"type": "plain_text", "text": "Banco"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
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
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "tasa_bcv",
                    "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "referencia",
                    "label": {"type": "plain_text", "text": "N° de referencia del pago"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                }
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
        metadata={
            "event_type": "cobro2_reportado",
            "event_payload": {
                "fecha": fecha,
                "nombre": nombre,
                "telefono": telefono,
                "cedula": cedula,
                "monto_bs": monto_bs_fmt,
                "forma_pago": forma_pago,
                "banco": banco,
                "monto_usd": monto_usd_str,
                "tasa_bcv": tasa_bcv_str,
                "referencia": referencia
            }
        },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_sheet_cobro2(
            meta.get("fecha", fecha_revision),
            meta.get("nombre", ""),
            meta.get("telefono", ""),
            meta.get("cedula", ""),
            meta.get("monto_bs", ""),
            meta.get("forma_pago", ""),
            meta.get("banco", ""),
            meta.get("monto_usd", ""),
            meta.get("tasa_bcv", ""),
            meta.get("referencia", "")
        )
    except Exception as e:
        print(f"Error: {e}")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cobro APROBADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.action("rechazar_cobro2")
def rechazar_cobro2(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /cobro2 ============


# ============ COMANDO /conciliar ============
# Concilia un pago reportado contra lo que realmente llegó al banco.
# El bot calcula automáticamente la diferencia y asigna el estado.

# Función para guardar en hoja "Conciliación"
def guardar_en_conciliacion(fecha_conciliacion, cliente_nombre, cedula, referencia, banco,
                            monto_reportado, monto_banco, diferencia, estado,
                            fecha_movimiento, conciliador, observaciones):
    try:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(
            creds_json,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        cliente = gspread.authorize(creds)
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        # Buscar la hoja "Conciliación" tolerando mayúsculas, espacios y acento
        sheet = None
        for ws in spreadsheet.worksheets():
            titulo = ws.title.strip().lower()
            if titulo in ("conciliación", "conciliacion"):
                sheet = ws
                break
        if sheet is None:
            print(f"❌ No se encontró la hoja 'Conciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
            return
        sheet.append_row([
            fecha_conciliacion, cliente_nombre, cedula, referencia, banco,
            monto_reportado, monto_banco, diferencia, estado,
            fecha_movimiento, conciliador, observaciones
        ])
        print(f"✅ Conciliación guardada en hoja '{sheet.title}'")
    except Exception as e:
        print(f"❌ Error guardando en Conciliación: {type(e).__name__}: {e}")


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
                {
                    "type": "input",
                    "block_id": "cliente",
                    "label": {"type": "plain_text", "text": "Nombre del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula del Cliente"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "referencia",
                    "label": {"type": "plain_text", "text": "N° de referencia del pago"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "banco",
                    "label": {"type": "plain_text", "text": "Banco"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
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
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "monto_reportado",
                    "label": {"type": "plain_text", "text": "Monto reportado (Bs)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "monto_banco",
                    "label": {"type": "plain_text", "text": "Monto según el banco (Bs)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "fecha_movimiento",
                    "label": {"type": "plain_text", "text": "Fecha del movimiento bancario (DD/MM/YYYY)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "conciliador",
                    "label": {"type": "plain_text", "text": "Conciliador"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "DIEGO"}, "value": "DIEGO"},
                            {"text": {"type": "plain_text", "text": "IARA"}, "value": "IARA"},
                            {"text": {"type": "plain_text", "text": "REBECA"}, "value": "REBECA"},
                            {"text": {"type": "plain_text", "text": "MARIANGEL"}, "value": "MARIANGEL"},
                            {"text": {"type": "plain_text", "text": "LUISMAR"}, "value": "LUISMAR"},
                            {"text": {"type": "plain_text", "text": "ANGELY"}, "value": "ANGELY"},
                            {"text": {"type": "plain_text", "text": "DANIEL"}, "value": "DANIEL"},
                            {"text": {"type": "plain_text", "text": "BARBARA"}, "value": "BARBARA"}
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "observaciones",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Observaciones"},
                    "element": {"type": "plain_text_input", "action_id": "valor", "multiline": True}
                }
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

    # Calcular diferencia y estado automáticamente
    try:
        rep_num = parse_numero(monto_reportado_str)
        banco_num = parse_numero(monto_banco_str)
        diferencia_num = banco_num - rep_num
        monto_reportado_fmt = f"Bs. {rep_num:,.2f}"
        monto_banco_fmt = f"Bs. {banco_num:,.2f}"
        diferencia_fmt = f"Bs. {diferencia_num:,.2f}"
        # Tolerancia de 1 céntimo para evitar problemas de redondeo
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
            metadata={
                "event_type": "conciliacion_reportada",
                "event_payload": {
                    "fecha": fecha,
                    "cliente": cliente_nombre,
                    "cedula": cedula,
                    "referencia": referencia,
                    "banco": banco,
                    "monto_reportado": monto_reportado_fmt,
                    "monto_banco": monto_banco_fmt,
                    "diferencia": diferencia_fmt,
                    "estado": estado,
                    "fecha_movimiento": fecha_movimiento,
                    "conciliador": conciliador,
                    "observaciones": observaciones
                }
            },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_conciliacion(
            meta.get("fecha", fecha_revision),
            meta.get("cliente", ""),
            meta.get("cedula", ""),
            meta.get("referencia", ""),
            meta.get("banco", ""),
            meta.get("monto_reportado", ""),
            meta.get("monto_banco", ""),
            meta.get("diferencia", ""),
            meta.get("estado", ""),
            meta.get("fecha_movimiento", ""),
            meta.get("conciliador", ""),
            meta.get("observaciones", "")
        )
    except Exception as e:
        print(f"Error: {e}")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Conciliación APROBADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.action("rechazar_conciliacion")
def rechazar_conciliacion(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Conciliación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDO /conciliar ============


# ============ COMANDOS DE LIQUIDACIONES (Lista VIP) ============
# Sheet aparte "Liquidaciones - Lista VIP"
SHEET_ID_LIQUIDACIONES = "1MYKQ-CnyMQBTEZcSBIXt-KDsBbfJt-tUmG-k5aZvDI0"
CANAL_LIQUIDACIONES = "C0BE1HLRV1R"

# Lista de estatus disponibles (se usa en los dos comandos)
ESTATUS_LIQUIDACION = [
    "Pending",
    "In validation",
    "Template contract",
    "Waiting contract",
    "Contract in validation",
    "Fecha primer pago",
    "Pending deposit"
]

# Opciones de Base (se usa en el comando de nueva)
BASES_LIQUIDACION = ["Base 1", "Base 2", "Base 3", "Base 4"]


def _abrir_hoja_liquidaciones():
    """Abre la hoja de Liquidaciones y devuelve (worksheet, spreadsheet)."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_LIQUIDACIONES)
    try:
        sheet = spreadsheet.worksheet("Hoja1")
    except Exception:
        sheet = spreadsheet.sheet1
    return sheet


# Construye las opciones de un static_select a partir de una lista de textos
def _opciones(lista):
    return [{"text": {"type": "plain_text", "text": x}, "value": x} for x in lista]


# ---------- Guardar persona nueva ----------
# Columnas: Fecha registro, Nombre, Cédula, Cliente/Empresa, Base, Estatus, Última actualización
def guardar_liquidacion_nueva(fecha, nombre, cedula, cliente_empresa, base, estatus):
    try:
        sheet = _abrir_hoja_liquidaciones()
        sheet.append_row([fecha, nombre, cedula, cliente_empresa, base, estatus, fecha])
        print(f"✅ Liquidación nueva guardada: {nombre} ({cedula})")
        return True
    except Exception as e:
        print(f"❌ Error guardando liquidación nueva: {type(e).__name__}: {e}")
        return False


# ---------- Actualizar estatus por cédula ----------
def actualizar_estatus_liquidacion(cedula, nuevo_estatus, fecha_actualizacion):
    """Busca la cédula (columna C) y actualiza Estatus (col F) y Última actualización (col G).
    Devuelve True si la encontró y actualizó, False si no existe."""
    try:
        sheet = _abrir_hoja_liquidaciones()
        valores = sheet.get_all_values()  # lista de filas; fila 0 = encabezados
        cedula_buscada = str(cedula).strip()
        for i, fila in enumerate(valores):
            if i == 0:
                continue  # saltar encabezados
            # Columna C (índice 2) = Cédula
            if len(fila) > 2 and fila[2].strip() == cedula_buscada:
                num_fila = i + 1  # gspread cuenta desde 1
                sheet.update_cell(num_fila, 6, nuevo_estatus)        # Columna F = Estatus
                sheet.update_cell(num_fila, 7, fecha_actualizacion)  # Columna G = Última actualización
                print(f"✅ Estatus actualizado para cédula {cedula_buscada}: {nuevo_estatus}")
                return True
        print(f"⚠️ No se encontró la cédula {cedula_buscada} en Liquidaciones")
        return False
    except Exception as e:
        print(f"❌ Error actualizando estatus: {type(e).__name__}: {e}")
        return False


# ============ COMANDO /liquidacion-nueva ============
@app.command("/liquidacion-nueva")
def reportar_liquidacion_nueva(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_liquidacion_nueva",
            "title": {"type": "plain_text", "text": "Nueva Liquidación"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "nombre",
                    "label": {"type": "plain_text", "text": "Nombre completo"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "cliente",
                    "label": {"type": "plain_text", "text": "Cliente / Empresa"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "base",
                    "label": {"type": "plain_text", "text": "Base"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": _opciones(BASES_LIQUIDACION)
                    }
                },
                {
                    "type": "input",
                    "block_id": "estatus",
                    "label": {"type": "plain_text", "text": "Estatus inicial"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": _opciones(ESTATUS_LIQUIDACION)
                    }
                }
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
            metadata={
                "event_type": "liquidacion_nueva",
                "event_payload": {
                    "fecha": fecha,
                    "nombre": nombre,
                    "cedula": cedula,
                    "cliente": cliente_empresa,
                    "base": base,
                    "estatus": estatus
                }
            },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    meta = body["message"].get("metadata", {}).get("event_payload", {})
    guardado = guardar_liquidacion_nueva(
        meta.get("fecha", fecha_revision),
        meta.get("nombre", ""),
        meta.get("cedula", ""),
        meta.get("cliente", ""),
        meta.get("base", ""),
        meta.get("estatus", "")
    )
    estado = "✅ *APROBADO*" if guardado else "⚠️ *APROBADO pero hubo error guardando (revisar logs)*"
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Liquidación APROBADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"{estado} por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


@app.action("rechazar_liquidacion_nueva")
def rechazar_liquidacion_nueva(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Liquidación RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


# ============ COMANDO /liquidacion-estatus ============
@app.command("/liquidacion-estatus")
def reportar_liquidacion_estatus(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "form_liquidacion_estatus",
            "title": {"type": "plain_text", "text": "Cambiar Estatus"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "cedula",
                    "label": {"type": "plain_text", "text": "Cédula de la persona"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "nombre",
                    "label": {"type": "plain_text", "text": "Nombre (referencia)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "estatus",
                    "label": {"type": "plain_text", "text": "Nuevo estatus"},
                    "element": {
                        "type": "static_select",
                        "action_id": "valor",
                        "placeholder": {"type": "plain_text", "text": "Selecciona"},
                        "options": _opciones(ESTATUS_LIQUIDACION)
                    }
                }
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
            metadata={
                "event_type": "liquidacion_estatus",
                "event_payload": {
                    "fecha": fecha,
                    "nombre": nombre,
                    "cedula": cedula,
                    "estatus": estatus
                }
            },
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
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    meta = body["message"].get("metadata", {}).get("event_payload", {})
    encontrado = actualizar_estatus_liquidacion(
        meta.get("cedula", ""),
        meta.get("estatus", ""),
        fecha_revision
    )
    if encontrado:
        estado = f"✅ *ESTATUS ACTUALIZADO* por <@{body['user']['id']}> el {fecha_revision}"
    else:
        estado = f"⚠️ *NO SE ENCONTRÓ ESA CÉDULA EN LA LISTA* (revisado por <@{body['user']['id']}> el {fecha_revision}). No se actualizó nada."
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cambio de estatus procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"{estado}\n\n{texto_original}"}}]
    )


@app.action("rechazar_liquidacion_estatus")
def rechazar_liquidacion_estatus(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cambio de estatus RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN COMANDOS DE LIQUIDACIONES ============

if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
