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

# Cuando alguien escribe /cobro
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
                    "block_id": "descripcion",
                    "label": {"type": "plain_text", "text": "Descripción (Nombre del cliente)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"}
                },
                {
                    "type": "input",
                    "block_id": "numero",
                    "label": {"type": "plain_text", "text": "Número (teléfono o referencia)"},
                    "element": {"type": "plain_text_input", "action_id": "valor"},
                    "optional": True
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

# Cuando el cobrador llena el formulario y le da Enviar
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

    # Fecha y hora en zona horaria de Venezuela (Caracas)
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")

    # Calcular monto en USD
    try:
        monto_bs_num = float(monto_bs_str.replace(".", "").replace(",", "."))
        tasa_bcv_num = float(tasa_bcv_str.replace(".", "").replace(",", "."))
        monto_usd = monto_bs_num / tasa_bcv_num
        monto_usd_str = f"${monto_usd:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"

    texto_reporte = (
        f"*Nuevo cobro reportado* 💰\n"
        f"*Fecha:* {fecha}\n"
        f"*Cobrador:* <@{cobrador}>\n"
        f"*Descripción:* {descripcion}\n"
        f"*Número:* {numero}\n"
        f"*Monto Bs:* {monto_bs_fmt}\n"
        f"*Forma de Pago:* {forma_pago}\n"
        f"*Banco:* {banco}\n"
        f"*Tasa BCV:* {tasa_bcv_str}\n"
        f"*Monto USD:* {monto_usd_str}"
    )

    # Guardar datos en el mensaje para usarlos al aprobar
    client.chat_postMessage(
        channel="#cobranzas-log",
        text="Nuevo cobro reportado",
        metadata={
            "event_type": "cobro_reportado",
            "event_payload": {
                "fecha": fecha,
                "cobrador": cobrador,
                "descripcion": descripcion,
                "numero": numero,
                "monto_bs": monto_bs_fmt,
                "forma_pago": forma_pago,
                "banco": banco,
                "tasa_bcv": tasa_bcv_str,
                "monto_usd": monto_usd_str
            }
        },
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": texto_reporte}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"},
                 "style": "primary", "action_id": "aprobar"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"},
                 "style": "danger", "action_id": "rechazar"}
            ]}
        ]
    )

# Botón Aprobar
@app.action("aprobar")
def aprobar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M")

    # Obtener datos del metadata para guardar en sheet
    try:
        metadata = body["message"].get("metadata", {}).get("event_payload", {})
        guardar_en_sheet(
            metadata.get("fecha", fecha_revision),
            metadata.get("cobrador", body["user"]["id"]),
            metadata.get("descripcion", ""),
            metadata.get("numero", ""),
            metadata.get("monto_bs", ""),
            metadata.get("forma_pago", ""),
            metadata.get("banco", ""),
            metadata.get("tasa_bcv", ""),
            metadata.get("monto_usd", "")
        )
    except Exception as e:
        print(f"Error al guardar en sheet: {e}")

    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Cobro APROBADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )

# Botón Rechazar
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

# Encender Robotín usando Socket Mode (sin URL pública)
if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()