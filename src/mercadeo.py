"""
mercadeo.py — Módulo de Mercadeo: /merca-reporte (Conciliación de Pagos e Incidencias
Técnicas, con modales armados a mano) y /incidencia-fullcode (Tickets Internos por
Departamento). Los dos comandos viven en el mismo archivo porque los dos guardan en el
MISMO Google Sheet de Mercadeo y comparten _abrir_hoja_mercadeo() para abrir sus pestañas.
"""
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    app, SHEET_ID_MERCADEO, CANAL_MERCADEO_PAGOS, CANAL_MERCADEO_INCIDENCIAS,
    abrir_pestana_cacheada,
)
from validaciones import (
    _normalizar_encabezado, _guardar_fila_por_encabezado, _registro_ya_guardado,
    _id_amigable, _ya_procesado, parse_numero, _validar_view, _reservar_mensaje,
)
from motor_formularios import (
    FORM_SPECS, _abrir_formulario_generico, _validar_formulario_generico, _ejecutar_formulario_generico,
    _registrar_aprobacion_para_deshacer, _notificar_resultado_al_reportante,
)
# _tasa_de_pago: para comparar la Tasa BCV que la persona escribe a mano en este formulario
# contra la tasa OFICIAL de la fecha del pago (ver el aviso de typo de tasa, más abajo).
from cobros import _tasa_de_pago, TASA_CAMBIO_ALERTA


# ============ MÓDULO DE MERCADEO (Conciliación de Pagos e Incidencias Técnicas) ============
def _abrir_hoja_mercadeo(nombre_pestana):
    # Pestaña cacheada (ver config.py) — ya no le pregunta a Google "cuáles son tus
    # pestañas" en cada llamada, solo la primera vez. Ya compara ignorando mayúsculas/
    # tildes/espacios de más por su cuenta, así que no hace falta ningún reintento manual
    # acá. Devuelve None si no la encuentra (antes lanzaba una excepción) — el resto del
    # código (guardar_conciliacion_mercadeo, guardar_incidencia_mercadeo, y el motor
    # genérico vía "abrir_hoja") ya sabe manejar un None sin explotar.
    ws = abrir_pestana_cacheada(SHEET_ID_MERCADEO, nombre_pestana)
    if ws is None:
        print(f"❌ No se encontró la pestaña '{nombre_pestana}' en el Sheet de Mercadeo.")
    return ws


def guardar_conciliacion_mercadeo(fecha_reporte, nombre_colaborador, numero_quoota, telefono, cedula, monto_bs,
                                   forma_pago, banco, fecha_pago, monto_usd, tasa_bcv, referencia, estado, revisor,
                                   fecha_revision, registro_id):
    try:
        sheet = _abrir_hoja_mercadeo("Conciliacion")
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Conciliación de Mercadeo duplicada (ya guardada), se omite.")
            return "DUPLICADO"
        datos = {
            "Fecha de Reporte": fecha_reporte,
            "Nombre de Colaborador": nombre_colaborador,
            "Numero de Quoota": numero_quoota,
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
    {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
    {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
    {"text": {"type": "plain_text", "text": "Banco Digital de los Trabajadores"}, "value": "Banco Digital de los Trabajadores"},
    {"text": {"type": "plain_text", "text": "Banco de Destino"}, "value": "Banco de Destino"},
    {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
    {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
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
            {"type": "input", "block_id": "numero_quoota",
             "label": {"type": "plain_text", "text": "Número de Quoota"},
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
    _err = _validar_view(_v, [('nombre_colaborador', 'requerido'), ('numero_quoota', 'requerido'),
                               ('telefono', 'telefono'), ('cedula', 'cedula'),
                               ('fecha_pago', 'fecha'), ('monto_bs', 'monto'), ('tasa_bcv', 'monto')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre_colaborador = valores["nombre_colaborador"]["valor"]["value"].strip()
    numero_quoota = valores["numero_quoota"]["valor"]["value"].strip()
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

    # ============ MENSAJE REDISEÑADO (mismo estilo que /cobro: resumen arriba, detalle
    # abajo con divisor, nombre+cédula en negrita, monto en Bs. y $ juntos) ============
    texto = (
        f"🧾 *Conciliación de pago — {monto_usd_fmt}*\n"
        f"*{nombre_colaborador} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha de Reporte:* {fecha_reporte}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"🔢 *Número de Quoota:* {numero_quoota}\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"🏦 *Pago:* {forma_pago} · {banco}\n"
        f"💵 *Monto:* {monto_bs_fmt}  (≈ {monto_usd_fmt})\n"
        f"📊 *Tasa BCV Aplicada:* {tasa_bcv_fmt}\n"
        f"📅 *Fecha de Pago:* {fecha_pago}\n"
        f"🔖 *Número de Referencia:* {referencia}"
    )

    # ============ AVISO DE POSIBLE ERROR EN LA TASA BCV (dato tecleado a mano) ============
    # Este campo se escribe a mano (no viene de /tasa-hoy), así que un typo (un cero de más,
    # una coma corrida) no lo agarra ninguna validación de formato — el Monto en USD calculado
    # queda con un valor absurdo (ej. $0.03 en vez de $26) sin que nadie se dé cuenta hasta
    # revisar el Sheet a mano. Se compara contra la tasa OFICIAL de la Fecha de Pago (no la de
    # hoy — la conciliación casi siempre se hace días después del pago real). Usa el emoji 💱
    # (no ✅/❌/⚠), por la misma razón que el aviso de duplicado usa 🔁 en el resto del bot.
    boton_aprobar = {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary",
                     "action_id": "aprobar_merca_conciliacion"}
    if tasa_bcv_num:
        try:
            tasa_oficial_num = _tasa_de_pago(fecha_pago, fecha_reporte)
            tasa_oficial_num = float(tasa_oficial_num) if tasa_oficial_num else None
        except Exception as e:
            tasa_oficial_num = None
            print(f"⚠️ [merca-conciliacion] No se pudo comparar contra la tasa oficial: {e}")
        if tasa_oficial_num:
            cambio = abs(tasa_bcv_num - tasa_oficial_num) / tasa_oficial_num
            if cambio > TASA_CAMBIO_ALERTA:
                texto = (f"💱 *POSIBLE ERROR EN LA TASA BCV* — escribiste Bs. {tasa_bcv_num:,.4f} pero la tasa "
                         f"oficial para el {fecha_pago} es Bs. {tasa_oficial_num:,.4f} (una diferencia de "
                         f"{cambio * 100:,.0f}%). Revisa que no se te haya colado un dígito de más o de menos.\n\n"
                         + texto)
                boton_aprobar["confirm"] = {
                    "title": {"type": "plain_text", "text": "Confirmar tasa fuera de rango"},
                    "text": {"type": "mrkdwn", "text": (
                        f"La tasa escrita (Bs. {tasa_bcv_num:,.4f}) difiere mucho de la oficial del "
                        f"{fecha_pago} (Bs. {tasa_oficial_num:,.4f}). ¿Aprobar de todas formas?")},
                    "confirm": {"type": "plain_text", "text": "Sí, aprobar de todas formas"},
                    "deny": {"type": "plain_text", "text": "Cancelar"},
                }
    # ============ FIN AVISO DE POSIBLE ERROR EN LA TASA BCV ============
    # ============ FIN MENSAJE REDISEÑADO ============
    try:
        client.chat_postMessage(
            channel=CANAL_MERCADEO_PAGOS,
            text="Nueva conciliación de pago (Mercadeo)",
            metadata={"event_type": "merca_conciliacion", "event_payload": {
                "fecha_reporte": fecha_reporte, "nombre_colaborador": nombre_colaborador,
                "numero_quoota": numero_quoota, "telefono": telefono,
                "cedula": cedula, "monto_bs": monto_bs_fmt, "forma_pago": forma_pago, "banco": banco,
                "fecha_pago": fecha_pago, "monto_usd": monto_usd_fmt, "tasa_bcv": tasa_bcv_fmt,
                "referencia": referencia, "_reportado_por": usuario_slack}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    boton_aprobar,
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
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("MERCACONC", body["message"]["ts"])
    resultado = "ERROR"
    meta = {}
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_conciliacion_mercadeo(
            meta.get("fecha_reporte", fecha_revision), meta.get("nombre_colaborador", ""),
            meta.get("numero_quoota", ""), meta.get("telefono", ""),
            meta.get("cedula", ""), meta.get("monto_bs", ""), meta.get("forma_pago", ""), meta.get("banco", ""),
            meta.get("fecha_pago", ""), meta.get("monto_usd", ""), meta.get("tasa_bcv", ""), meta.get("referencia", ""),
            "Aprobado", body["user"]["id"], fecha_revision, registro_id)
    except Exception as e:
        print(f"Error: {e}")
    reportado_por = meta.get("_reportado_por")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta conciliación ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
        _notificar_resultado_al_reportante(client, reportado_por, "Conciliación de Mercadeo", "⚠️",
                                            "ya estaba registrada (no se duplicó)", body["user"]["id"], fecha_revision)
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
        if resultado == "OK":
            _blocks_msg = body["message"].get("blocks", [])
            _registrar_aprobacion_para_deshacer({
                "abrir_hoja": lambda: _abrir_hoja_mercadeo("Conciliacion"),
                "columna_id_registro": "ID Registro",
                "registro_id": registro_id,
                "canal": body["channel"]["id"],
                "ts": body["message"]["ts"],
                "texto_original": texto_original,
                "blocks_accion_original": _blocks_msg[1] if len(_blocks_msg) > 1
                    else {"type": "actions", "elements": []},
                "aprobado_por": body["user"]["id"],
                "resumen": "Conciliación de Mercadeo",
            })
            _notificar_resultado_al_reportante(client, reportado_por, "Conciliación de Mercadeo", "✅",
                                                "fue aprobada", body["user"]["id"], fecha_revision)
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
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    reportado_por = body["message"].get("metadata", {}).get("event_payload", {}).get("_reportado_por")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Conciliación de Mercadeo RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
    _notificar_resultado_al_reportante(client, reportado_por, "Conciliación de Mercadeo", "❌",
                                        "fue rechazada", body["user"]["id"], fecha_revision)


@app.view("form_merca_incidencia")
def recibir_incidencia_mercadeo(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_view(_v, [('nombre', 'requerido'), ('cedula', 'cedula')])
    if _err:
        ack(response_action="errors", errors=_err)
        return
    ack()
    valores = body["view"]["state"]["values"]
    nombre = valores["nombre"]["valor"]["value"].strip()
    cedula = valores["cedula"]["valor"]["value"]
    empresa = valores["empresa"]["valor"]["value"]
    incidencia = valores["incidencia"]["valor"]["selected_option"]["value"]
    descripcion = valores["descripcion"]["valor"]["value"]
    usuario_slack = body["user"]["id"]
    fecha_reporte = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    # ============ MENSAJE REDISEÑADO (mismo estilo que /cobro y conciliación de pago) ============
    texto = (
        f"🛠️ *Incidencia técnica — {incidencia}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha de Reporte:* {fecha_reporte}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"🏢 *Empresa:* {empresa}\n"
        f"📝 *Descripción:* {descripcion}"
    )
    # ============ FIN MENSAJE REDISEÑADO ============
    try:
        client.chat_postMessage(
            channel=CANAL_MERCADEO_INCIDENCIAS,
            text="Nueva incidencia técnica (Mercadeo)",
            metadata={"event_type": "merca_incidencia", "event_payload": {
                "fecha_reporte": fecha_reporte, "nombre": nombre, "cedula": cedula, "empresa": empresa,
                "incidencia": incidencia, "descripcion": descripcion, "_reportado_por": usuario_slack}},
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
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    registro_id = _id_amigable("MERCAINC", body["message"]["ts"])
    resultado = "ERROR"
    meta = {}
    try:
        meta = body["message"].get("metadata", {}).get("event_payload", {})
        resultado = guardar_incidencia_mercadeo(
            meta.get("fecha_reporte", fecha_revision), meta.get("nombre", ""), meta.get("cedula", ""),
            meta.get("empresa", ""), meta.get("incidencia", ""), meta.get("descripcion", ""),
            "Aprobado", body["user"]["id"], fecha_revision, registro_id)
    except Exception as e:
        print(f"Error: {e}")
    reportado_por = meta.get("_reportado_por")
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADA* — esta incidencia ya estaba guardada, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
        _notificar_resultado_al_reportante(client, reportado_por, "Incidencia Técnica", "⚠️",
                                            "ya estaba registrada (no se duplicó)", body["user"]["id"], fecha_revision)
    else:
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
        _notificar_resultado_al_reportante(client, reportado_por, "Incidencia Técnica", "✅",
                                            "fue aprobada", body["user"]["id"], fecha_revision)
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
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    reportado_por = body["message"].get("metadata", {}).get("event_payload", {}).get("_reportado_por")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Incidencia técnica RECHAZADA",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
    _notificar_resultado_al_reportante(client, reportado_por, "Incidencia Técnica", "❌",
                                        "fue rechazada", body["user"]["id"], fecha_revision)
# ============ FIN MÓDULO DE MERCADEO ============


# ============ COMANDO /incidencia-fullcode (Tickets Internos por Departamento) ============
# Reporta incidencias operativas a nivel de departamento/área (sin datos individuales de
# clientes ni empleados). Se guarda directo al enviar el formulario (sin aprobación) y se
# publica en el canal de Incidencias, para que el equipo las revise y las resuelva.

# ============ MENSAJE REDISEÑADO (mismo estilo que el resto de comandos rediseñados) ============
def _texto_reportar_incidencia_v2(datos_campos, fecha, usuario_slack):
    departamento = datos_campos.get("departamento", "")
    tipo_incidencia = datos_campos.get("tipo_incidencia", "")
    usuario_reporte = datos_campos.get("usuario_reporte", "")
    descripcion = datos_campos.get("descripcion", "")
    return (
        f"🛠️ *Ticket interno — {tipo_incidencia}*\n"
        f"*Departamento: {departamento}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"🧑‍💻 *Usuario del Reporte:* {usuario_reporte}\n"
        f"📝 *Descripción:* {descripcion}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


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
    "construir_texto": _texto_reportar_incidencia_v2,
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
