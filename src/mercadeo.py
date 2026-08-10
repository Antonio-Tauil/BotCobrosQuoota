"""
mercadeo.py — Módulo de Mercadeo: /merca-reporte (Conciliación de Pagos e Incidencias
Técnicas, con modales armados a mano) y /incidencia-fullcode (Tickets Internos por
Departamento). Los dos comandos viven en el mismo archivo porque los dos guardan en el
MISMO Google Sheet de Mercadeo y comparten _abrir_hoja_mercadeo() para abrir sus pestañas.
"""
import os
import json
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

from config import app, SHEET_ID_MERCADEO, CANAL_MERCADEO_PAGOS, CANAL_MERCADEO_INCIDENCIAS
from validaciones import (
    _normalizar_encabezado, _guardar_fila_por_encabezado, _registro_ya_guardado,
    _id_amigable, _ya_procesado, parse_numero, _validar_view,
)
from motor_formularios import FORM_SPECS, _abrir_formulario_generico, _validar_formulario_generico, _ejecutar_formulario_generico


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
    try:
        return spreadsheet.worksheet(nombre_pestana)
    except gspread.exceptions.WorksheetNotFound:
        # Busca de nuevo ignorando mayúsculas/tildes/espacios de más, por si el nombre de la
        # pestaña en el Sheet no coincide EXACTO (ej. un espacio invisible al final del título).
        objetivo = _normalizar_encabezado(nombre_pestana)
        for pestana in spreadsheet.worksheets():
            if _normalizar_encabezado(pestana.title) == objetivo:
                return pestana
        nombres_disponibles = ", ".join(f"'{p.title}'" for p in spreadsheet.worksheets())
        print(f"❌ No se encontró la pestaña '{nombre_pestana}' en el Sheet. "
              f"Pestañas disponibles: {nombres_disponibles}")
        raise


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
    {"text": {"type": "plain_text", "text": "Banco Digital de los Trabajadores"}, "value": "Banco Digital de los Trabajadores"},
    {"text": {"type": "plain_text", "text": "Banco de Destino"}, "value": "Banco de Destino"},
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
    tasa_bcv_str = valores["tasa_bcv"]["valor"]["value"]
    referencia = valores["referencia"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha_reporte = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    # El Monto en USD ya NO se escribe a mano: se calcula solo a partir de Monto en Bs
    # y Tasa BCV, igual que en /cobro-callcenter y /cobro-comercial.
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, AttributeError):
        monto_bs_num = None
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    try:
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        tasa_bcv_fmt = f"Bs. {tasa_bcv_num:,.4f}"
    except (ValueError, AttributeError):
        tasa_bcv_num = None
        tasa_bcv_fmt = f"Bs. {tasa_bcv_str}"
    try:
        monto_usd_fmt = f"$ {monto_bs_num/tasa_bcv_num:,.2f}"
    except (TypeError, ZeroDivisionError):
        monto_usd_fmt = "(No calculable)"

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


# ============ COMANDO /incidencia-fullcode (Tickets Internos por Departamento) ============
# Reporta incidencias operativas a nivel de departamento/área (sin datos individuales de
# clientes ni empleados). Se guarda directo al enviar el formulario (sin aprobación) y se
# publica en el canal de Incidencias, para que el equipo las revise y las resuelva.
FORM_SPECS["reportar_incidencia"] = {
    "callback_id": "form_reportar_incidencia",
    "titulo": "Reportar Incidencia",
    "campos": [
        {"id": "departamento", "label": "Departamento que Reporta", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Comercial"}, "value": "Comercial"},
            {"text": {"type": "plain_text", "text": "Tesorería"}, "value": "Tesorería"},
            {"text": {"type": "plain_text", "text": "Legal"}, "value": "Legal"},
            {"text": {"type": "plain_text", "text": "Administración"}, "value": "Administración"},
            {"text": {"type": "plain_text", "text": "Operaciones"}, "value": "Operaciones"},
        ]},
        {"id": "tipo_incidencia", "label": "Tipo de Incidencia", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Acceso"}, "value": "Acceso"},
            {"text": {"type": "plain_text", "text": "Tecnología"}, "value": "Tecnología"},
            {"text": {"type": "plain_text", "text": "Solicitud pendiente"}, "value": "Solicitud pendiente"},
            {"text": {"type": "plain_text", "text": "Fallo de App"}, "value": "Fallo de App"},
            {"text": {"type": "plain_text", "text": "Reporte de Pago"}, "value": "Reporte de Pago"},
            {"text": {"type": "plain_text", "text": "Anulación de Solicitud"}, "value": "Anulación de Solicitud"},
            {"text": {"type": "plain_text", "text": "Fallo con Correo"}, "value": "Fallo con Correo"},
            {"text": {"type": "plain_text", "text": "Actualización de Datos"}, "value": "Actualización de Datos"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "usuario_reporte", "label": "Usuario del Reporte", "tipo": "texto"},
        {"id": "descripcion", "label": "Descripción", "tipo": "texto", "multiline": True},
    ],
    "abrir_hoja": lambda: _abrir_hoja_mercadeo("Incidencias full code"),
    "agregar_fecha": "Fecha",
    "columnas": {
        "departamento": "Departamento", "tipo_incidencia": "Tipo de Incidencia",
        "usuario_reporte": "Usuario del Reporte", "descripcion": "Descripción",
    },
    "canal": "C0BNT56M79U",
    "titulo_mensaje": "Nuevo ticket de incidencia interna",
    "emoji_mensaje": "🛠️",
    "campos_mensaje": [
        ("Departamento", "departamento"), ("Tipo de Incidencia", "tipo_incidencia"),
        ("Usuario del Reporte", "usuario_reporte"), ("Descripción", "descripcion"),
    ],
}


@app.command("/incidencia-fullcode")
def reportar_incidencia_fullcode(ack, body, client):
    ack()
    _abrir_formulario_generico("reportar_incidencia", body["trigger_id"], client)


@app.view("form_reportar_incidencia")
def recibir_incidencia_fullcode(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("reportar_incidencia", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _ejecutar_formulario_generico("reportar_incidencia", body, client)
