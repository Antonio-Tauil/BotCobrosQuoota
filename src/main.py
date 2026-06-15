import os
import json
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials

app = App(token=os.environ["SLACK_BOT_TOKEN"])


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
                            {"text": {"type": "plain_text", "text": "DANIEL"}, "value": "DANIEL"}
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
        monto_bs_num = float(monto_bs_str.replace(".", "").replace(",", "."))
        tasa_bcv_num = float(tasa_bcv_str.replace(".", "").replace(",", "."))
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
                            {"text": {"type": "plain_text", "text": "DANIEL"}, "value": "DANIEL"}
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
        monto_bs_num = float(monto_bs_str.replace(".", "").replace(",", "."))
        tasa_bcv_num = float(tasa_bcv_str.replace(".", "").replace(",", "."))
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    # Formatear "Cuenta por cobrar" como bolívares
    try:
        cuenta_num = float(cuenta.replace(".", "").replace(",", "."))
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

if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
