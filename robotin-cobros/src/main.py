import subprocess
import sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "gspread", "google-auth", "-q"])

import os
import json
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Función para guardar en Google Sheets
def guardar_en_sheet(fecha, cobrador, descripcion, numero, monto_bs, forma_pago, banco, tasa_bcv, monto_usd):
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
        sheet.append_row([fecha, descripcion, numero, "", monto_bs, forma_pago, banco, monto_usd, tasa_bcv, cobrador])
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
                {"type": "input", "block_id": "descripcion", "label": {"type": "plain_text", "text": "Descripción (Nombre del cliente)"}, "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "numero", "label": {"type": "plain_text", "text": "Número (teléfono o referencia)"}, "element": {"type": "plain_text_input", "action_id": "valor"}, "optional": True},
                {"type": "input", "block_id": "monto_bs", "label": {"type": "plain_text", "text": "Monto en Bs"}, "element": {"type": "plain_text_input", "action_id": "valor"}},
                {"type": "input", "block_id": "forma_pago", "label": {"type": "plain_text", "text": "Forma de Pago"}, "element": {"type": "static_select", "action_id": "valor", "placeholder": {"type": "plain_text", "text": "Selecciona"}, "options": [{"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"}, {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"}, {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"}, {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"}, {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}]}},
                {"type": "input", "block_id": "banco", "label": {"type": "plain_text", "text": "Banco"}, "element": {"type": "static_select", "action_id": "valor", "placeholder": {"type": "plain_text", "text": "Selecciona"}, "options": [{"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"}, {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"}, {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"}, {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"}, {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"}, {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"}, {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"}, {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"}]}},
                {"type": "input", "block_id": "tasa_bcv", "label": {"type": "plain_text", "text": "Tasa BCV (Bs por USD)"}, "element": {"type": "plain_text_input", "action_id": "valor"}}
            ]
        }
    )

@app.view("form_cobro")
def recibir_cobro(ack, body, client):
    ack()
    valores = body["view"]["state"]["values"]
    descripcion = valores["descripcion"]["valor"]["value"]
    numero_raw = valores["numero"]["valor"]["value"]
    numero = numero_raw if numero_raw else "—"
    monto_bs_str = valores["monto_bs"]["valor"]["value"]
    forma_pago = valores["forma_pago"]["valor"]["selected_option"]["value"]
    banco = valores["banco"]["valor"]["selected_option"]["value"]
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    cobrador = body["user"]["id"]
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        monto_bs_num = float(monto_bs_str.replace(".", "").replace(",", "."))
        tasa_bcv_num = float(tasa_bcv_str.replace(".", "").replace(",", "."))
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    texto = (f"*Nuevo cobro reportado* 💰\n*Fecha:* {fecha}\n*Cobrador:* <@{cobrador}>\n*Descripción:* {descripcion}\n*Número:* {numero}\n*Monto Bs:* {monto_bs_fmt}\n*Forma de Pago:* {forma_pago}\n*Banco:* {banco}\n*Tasa BCV:* {tasa_bcv_str}\n*Monto USD:* {monto_usd_str}")
    client.chat_postMessage(channel="#cobranzas-log", text="Nuevo cobro reportado", metadata={"event_type": "cobro_reportado", "event_payload": {"fecha": fecha, "cobrador": cobrador, "descripcion": descripcion, "numero": numero, "monto_bs": monto_bs_fmt, "forma_pago": forma_pago, "banco": banco, "tasa_bcv": tasa_bcv_str, "monto_usd": monto_usd_str}}, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": texto}}, {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar"}, {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar"}]}])

@app.action("aprobar")
def aprobar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_sheet(meta.get("fecha", fecha_revision), meta.get("cobrador", body["user"]["id"]), meta.get("descripcion", ""), meta.get("numero", ""), meta.get("monto_bs", ""), meta.get("forma_pago", ""), meta.get("banco", ""), meta.get("tasa_bcv", ""), meta.get("monto_usd", ""))
    except Exception as e:
        print(f"Error: {e}")
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro APROBADO", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}])

@app.action("rechazar")
def rechazar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}])

if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
