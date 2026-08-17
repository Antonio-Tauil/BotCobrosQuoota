"""
cobros.py — Todos los comandos de cobranza: /contactar, /cobro, /domiciliar,
/cobro-callcenter, /conciliar, liquidaciones, /cobro-comercial, /contacto-legal,
/listar-ids, /clientes-escalados, /buscar-cliente, /tasa-hoy e /incidencia-fullcode.
"""
import os
import json
import gspread
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

from config import (
    app, SHEET_ID_COBRO2, SHEET_ID_LIQUIDACIONES, SHEET_ID_COMERCIAL, SHEET_ID_LEGAL,
    SHEET_ID_ESCALADOS, CANAL_LIQUIDACIONES, CANAL_COMERCIAL, CANAL_LEGAL, CANAL_ESCALADOS,
    PESTANA_INDICADORES, PESTANA_HISTORIAL_TASAS, _opciones_cobradores, get_cliente_busqueda,
)
from validaciones import (
    _normalizar_encabezado, _guardar_fila_por_encabezado, _columna_por_nombre,
    _registro_ya_guardado, _id_amigable, _ya_procesado, _solo_digitos, _quitar_acentos,
    parse_numero, _es_fecha_valida, _reservar_mensaje, _buscar_duplicado_reciente,
)
from motor_formularios import (
    FORM_SPECS, _construir_blocks_formulario, _abrir_formulario_generico,
    _validar_formulario_generico, _extraer_valores_formulario, _guardar_generico,
    _construir_texto_mensaje, _ejecutar_formulario_generico, _publicar_para_aprobacion,
    _aprobar_generico, _rechazar_generico, _editar_generico, _valor_actual_bloque,
    _construir_handler_historial, _registrar_metrica, _construir_handler_autocompletar,
    _valores_view_desde_metadata, _registrar_aprobacion_para_deshacer, _ejecutar_deshacer,
    _ultima_aprobacion_deshacible, _ULTIMAS_APROBACIONES, _DESHACER_VENTANA_MINUTOS,
)


# ============ LLENADO AUTOMÁTICO DEL FORMULARIO POR CÉDULA (compartido entre comandos) ============
# Cuando alguien escribe una cédula que ya está registrada en 'Pagos Recibidos' o
# 'Contactados' (las dos hojas donde más clientes quedan con Nombre y Teléfono guardados),
# el botón "Ver historial"/"Buscar cliente" también rellena esos dos campos automáticamente —
# así no hay que volver a escribirlos a mano, y se reduce el riesgo de un typo en el nombre.
# A propósito NO es una búsqueda exhaustiva como /buscar-cliente (que revisa 8 hojas): aquí
# basta con las 2 fuentes más completas, para que el botón responda rápido. Nunca lanza error
# — si algo falla, simplemente no rellena nada y el formulario sigue funcionando como siempre.
# Nota: esta función se define ANTES que _abrir_hoja_pagos_recibidos_cobro/_abrir_hoja_contactados
# a propósito, para que los comandos que la usan (/contactar, /contacto-legal, etc., que
# aparecen en el archivo antes que /cobro) puedan pasarla al registrar sus botones sin
# problema — Python solo necesita que esas funciones existan cuando esta SE EJECUTE, no
# cuando se defina.
def _autocompletar_cliente(cedula_digitos):
    """Busca 'cedula_digitos' en 'Pagos Recibidos' y 'Contactados'. Devuelve
    {"nombre": ..., "telefono": ...} del primer registro que encuentre (con al menos uno de
    los dos datos), o None si no aparece en ninguna de las dos."""
    if not cedula_digitos:
        return None
    fuentes = [
        (_abrir_hoja_pagos_recibidos_cobro, "Cedula", "Nombre", "Telefono"),
        (_abrir_hoja_contactados, "Cedula", "Nombre", "Telefono"),
    ]
    for abrir_hoja, col_ced, col_nombre, col_telefono in fuentes:
        try:
            sheet = abrir_hoja()
        except Exception as e:
            print(f"⚠️ [autocompletar_cliente] No se pudo abrir una hoja: {e}")
            continue
        if sheet is None:
            continue
        try:
            idx_ced = _columna_por_nombre(sheet, col_ced)
            if idx_ced is None:
                continue
            idx_nombre = _columna_por_nombre(sheet, col_nombre)
            idx_telefono = _columna_por_nombre(sheet, col_telefono)
            for fila in sheet.get_all_values()[1:]:
                if len(fila) > idx_ced - 1 and _solo_digitos(fila[idx_ced - 1]) == cedula_digitos:
                    nombre = fila[idx_nombre - 1].strip() if idx_nombre and len(fila) > idx_nombre - 1 else ""
                    telefono = fila[idx_telefono - 1].strip() if idx_telefono and len(fila) > idx_telefono - 1 else ""
                    if nombre or telefono:
                        return {"nombre": nombre, "telefono": telefono}
        except Exception as e:
            print(f"⚠️ [autocompletar_cliente] Error buscando en una hoja: {e}")
            continue
    return None
# ============ FIN LLENADO AUTOMÁTICO DEL FORMULARIO POR CÉDULA ============


# ============ NUEVO COMANDO /contactar (migrado al Motor Genérico - Fase 3) ============
def _abrir_hoja_contactados():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "contactados":
            return ws
    print(f"❌ No se encontró la hoja 'Contactados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
    return None


# ============ SCORE DE RIESGO DEL CLIENTE (basado en promesas cumplidas/falladas) ============
# Lee 'Contactados' (misma hoja donde /promesa-cumplida y /promesa-fallida marcan la
# columna H "Estado de promesa") y calcula qué tan cumplidor ha sido un cliente con sus
# compromisos de pago anteriores. Es un dato de contexto para quien va a aprobar o
# gestionar un cobro — NO bloquea ni condiciona nada, solo informa. Por eso, igual que
# el resto de los helpers de "contexto" de este archivo, nunca lanza error: si algo
# falla, simplemente no se muestra score (mejor sin dato que romper el flujo).
def _score_riesgo_cliente(cedula_digitos, minimo_promesas=1):
    """Devuelve un texto tipo '🟢 *Bajo riesgo* — 3/3 promesas cumplidas' según el
    historial de promesas de esta cédula en 'Contactados', o None si no hay al menos
    'minimo_promesas' promesas ya resueltas (Cumplida/Fallida) para dar un score confiable.
    Promesas sin resultado todavía (columna vacía) no cuentan ni a favor ni en contra."""
    if not cedula_digitos:
        return None
    try:
        sheet = _abrir_hoja_contactados()
        if sheet is None:
            return None
        col_cedula = _columna_por_nombre(sheet, "Cedula")
        col_estado = _columna_por_nombre(sheet, "Estado de promesa")
        if col_cedula is None or col_estado is None:
            return None
        idx_cedula, idx_estado = col_cedula - 1, col_estado - 1
        cumplidas = fallidas = 0
        for fila in sheet.get_all_values()[1:]:
            if len(fila) > idx_cedula and _solo_digitos(fila[idx_cedula]) == cedula_digitos:
                estado = fila[idx_estado].strip() if len(fila) > idx_estado else ""
                if estado == "Cumplida":
                    cumplidas += 1
                elif estado == "Fallida":
                    fallidas += 1
        total = cumplidas + fallidas
        if total < minimo_promesas:
            return None
        ratio = cumplidas / total
        if ratio >= 0.8:
            emoji, etiqueta = "🟢", "Bajo riesgo"
        elif ratio >= 0.5:
            emoji, etiqueta = "🟡", "Riesgo medio"
        else:
            emoji, etiqueta = "🔴", "Alto riesgo"
        return f"{emoji} *{etiqueta}* — {cumplidas}/{total} promesas cumplidas"
    except Exception as e:
        print(f"⚠️ No se pudo calcular el score de riesgo del cliente: {e}")
        return None
# ============ FIN SCORE DE RIESGO DEL CLIENTE ============


# ============ MENSAJE REDISEÑADO (mismo estilo que /cobro y /merca-reporte) ============
# El dato que más le importa a quien revisa /contactar es CUÁNDO se comprometió a pagar el
# cliente (para hacerle seguimiento) — por eso va arriba en el resumen, junto con el nombre y
# la cédula en negrita. El resto del detalle queda agrupado debajo de la línea divisoria.
def _texto_contacto_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    telefono = datos_campos.get("telefono", "")
    cedula = datos_campos.get("cedula", "")
    compromiso = datos_campos.get("compromiso", "")
    cobrador = datos_campos.get("cobrador", "")
    comentario = datos_campos.get("comentario", "")
    return (
        f"📞 *Contacto registrado — Compromiso: {compromiso}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Cobrador:* {cobrador} (<@{usuario_slack}>)\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"💬 *Comentario:* {comentario}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["contactar"] = {
    "callback_id": "form_contactar",
    "titulo": "Reportar Contacto",
    "campos": [
        {"id": "nombre", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "telefono", "label": "Teléfono", "tipo": "texto", "validar": "telefono"},
        {"id": "cedula", "label": "Cédula", "tipo": "texto", "validar": "cedula"},
        {"id": "compromiso", "label": "Compromiso de pago (DD/MM/YYYY)", "tipo": "texto", "validar": "fecha"},
        {"id": "cobrador", "label": "Cobrador", "tipo": "select", "opciones": _opciones_cobradores},
        {"id": "comentario", "label": "Comentario", "tipo": "texto", "multiline": True},
    ],
    "abrir_hoja": _abrir_hoja_contactados,
    "agregar_fecha": "Fecha",
    "columnas": {
        "nombre": "Nombre", "telefono": "Telefono", "cedula": "Cedula",
        "compromiso": "Compromiso de pago", "cobrador": "Cobrador", "comentario": "COMENTARIO",
    },
    "canal": "#cobranzas-contactados",
    "titulo_mensaje": "Nuevo contacto registrado",
    "emoji_mensaje": "📞",
    "campos_mensaje": [
        ("Cliente", "nombre"), ("Teléfono", "telefono"), ("Cédula", "cedula"),
        ("Compromiso de pago", "compromiso"), ("Cobrador", "cobrador"), ("Comentario", "comentario"),
    ],
    "construir_texto": _texto_contacto_v2,
    "boton_historial": "ver_historial_contactar",
    "boton_historial_label": "🔍 Buscar cliente",
}



# ============ COMANDO /contactar (usando el Motor Genérico) ============
@app.command("/contactar")
def reportar_contacto(ack, body, client):
    ack()
    _abrir_formulario_generico("contactar", body["trigger_id"], client)


@app.view("form_contactar")
def recibir_contacto(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("contactar", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _ejecutar_formulario_generico("contactar", body, client)


# Botón "Buscar cliente" (rellena nombre/teléfono si ya está registrado en 'Pagos Recibidos'
# o 'Contactados' — ver motor_formularios.py). No lleva historial porque /contactar no tiene
# "verificar_duplicado" (varios contactos legítimos con el mismo cliente en la semana son
# normales acá, a diferencia de un cobro).
_handler_autocompletar_contactar = _construir_handler_autocompletar(
    "contactar", "cedula", _autocompletar_cliente, calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_contactar")
def ver_historial_contactar(ack, body, client):
    _handler_autocompletar_contactar(ack, body, client)
# ============ FIN COMANDO /contactar ============



# ============ COMANDO /cobro ============
def _abrir_hoja_pagos_recibidos_cobro():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    return cliente.open_by_key(os.environ["SHEET_ID"]).worksheet("Pagos Recibidos")


# ============ HISTORIAL RECIENTE DEL CLIENTE (contexto para quien aprueba) ============
def _historial_reciente_cliente(sheet, cedula_digitos, maximo=3):
    """Devuelve los últimos 'maximo' cobros anteriores de esta cédula en 'Pagos Recibidos'
    (fecha y monto), para que quien aprueba tenga contexto sin tener que usar /buscar-cliente
    por separado. Solo revisa esta hoja (no las 8 que revisa /buscar-cliente) a propósito:
    es la que usa /cobro, y así no se agregan lecturas extra a Google Sheets en cada envío
    del formulario. Nunca lanza error — si algo falla, simplemente no muestra historial."""
    if not cedula_digitos or sheet is None:
        return []
    try:
        col_cedula = _columna_por_nombre(sheet, "Cedula")
        col_fecha = _columna_por_nombre(sheet, "Fecha")
        col_monto_bs = _columna_por_nombre(sheet, "MontoBs")
        col_monto_usd = _columna_por_nombre(sheet, "MontoUsd")
        if col_cedula is None or col_fecha is None:
            return []
        idx_cedula, idx_fecha = col_cedula - 1, col_fecha - 1
        idx_bs = (col_monto_bs - 1) if col_monto_bs else None
        idx_usd = (col_monto_usd - 1) if col_monto_usd else None
        coincidencias = []
        for fila in sheet.get_all_values()[1:]:
            if len(fila) > idx_cedula and _solo_digitos(fila[idx_cedula]) == cedula_digitos:
                fecha = fila[idx_fecha] if len(fila) > idx_fecha else ""
                montos = []
                if idx_bs is not None and len(fila) > idx_bs and fila[idx_bs].strip():
                    montos.append(fila[idx_bs].strip())
                if idx_usd is not None and len(fila) > idx_usd and fila[idx_usd].strip():
                    montos.append(fila[idx_usd].strip())
                coincidencias.append((fecha, " / ".join(montos) if montos else "(sin monto)"))
        return coincidencias[-maximo:]
    except Exception as e:
        print(f"⚠️ No se pudo obtener el historial del cliente: {e}")
        return []
# ============ FIN HISTORIAL RECIENTE DEL CLIENTE ============


def guardar_en_sheet(fecha, cobrador, descripcion, numero, cedula, monto_bs, forma_pago, banco, tasa_bcv, monto_usd, registro_id=""):
    try:
        sheet = _abrir_hoja_pagos_recibidos_cobro()
        if _registro_ya_guardado(sheet, registro_id):
            print("⚠️ Cobro duplicado (ya guardado), se omite.")
            return "DUPLICADO"
        datos = {
            "Fecha": fecha,
            "Nombre": descripcion,
            "Telefono": numero,
            "Cedula": cedula,
            "MontoBs": monto_bs,
            "FormaPago": forma_pago,
            "Banco": banco,
            "MontoUsd": monto_usd,
            "TasaBCV": tasa_bcv,
            "Cobrador": cobrador,
            "ID Registro": registro_id,
        }
        _guardar_fila_por_encabezado(sheet, datos)
        print("✅ Cobro guardado en Google Sheets")
        return "OK"
    except Exception as e:
        print(f"❌ Error guardando en sheet: {e}")
        return "ERROR"


# Ficha del formulario de /cobro (Fase 3 — Motor Genérico). Solo se usa para CONSTRUIR el
# modal (con _construir_blocks_formulario, igual que los demás comandos migrados). El guardado,
# la aprobación, el cálculo de la tasa por fecha y la validación cruzada de "fecha + tasa"
# se quedan TAL CUAL estaban (a propósito): son la parte más delicada de este comando, y
# el motor genérico de hoy todavía no sabe expresar "buscar la tasa en otra pestaña según la
# fecha" ni "un error de un campo se muestra en otro campo". Por eso esta migración es HÍBRIDA
# (mismo patrón que ya usamos con /liquidacion-estatus): se reduce la parte repetitiva y segura
# (armar el modal), sin tocar la lógica de negocio de la que depende el flujo de caja diario.
FORM_SPECS["cobro"] = {
    "callback_id": "form_cobro",
    "titulo": "Reportar Cobro",
    "campos": [
        {"id": "fecha_pago", "label": "Fecha del Pago (DD/MM/AAAA) — déjalo vacío si es de hoy",
         "tipo": "texto", "opcional": True},
        {"id": "nombre_cobrador", "label": "Nombre del Cobrador", "tipo": "select",
         "opciones": _opciones_cobradores},
        {"id": "descripcion", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "cedula", "label": "Cédula del Cliente", "tipo": "texto", "validar": "cedula"},
        {"id": "numero", "label": "Teléfono o Referencia", "tipo": "texto"},
        {"id": "monto_bs", "label": "Monto en Bs", "tipo": "texto", "validar": "monto"},
        {"id": "forma_pago", "label": "Forma de Pago", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
            {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
            {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
            {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "banco", "label": "Banco", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
            {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
            {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
            {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
            {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
            {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
            {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
            {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
            {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
            {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
            {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
    ],
    "boton_historial": "ver_historial_cobro",
    "boton_historial_label": "🔍 Buscar cliente / ver historial",
}


@app.command("/cobro")
def reportar_cobro(ack, body, client):
    ack()
    _abrir_formulario_generico("cobro", body["trigger_id"], client)


# ============ BOTÓN "Ver historial del cliente" DENTRO DEL MODAL DE /cobro ============
# Al apretar el botón (sin cerrar el formulario), busca los últimos 3 cobros de la cédula
# escrita y actualiza el MISMO modal para mostrarlos arriba, sin perder lo que la persona ya
# había llenado en los demás campos (por eso _construir_blocks_formulario recibe el
# 'state.values' actual: repuebla cada campo con su valor de antes).
@app.action("ver_historial_cobro")
def ver_historial_cobro(ack, body, client):
    ack()
    valores_view = dict(body["view"]["state"]["values"])
    cedula_input = (_valor_actual_bloque(valores_view, "cedula") or "").strip()
    cedula_digitos = _solo_digitos(cedula_input)
    if not cedula_digitos:
        texto_extra = "⚠️ Escribe primero la cédula del cliente arriba, y vuelve a apretar el botón."
    else:
        partes_texto = []
        # ---- Llenado automático: nombre/teléfono ya conocidos de esta cédula ----
        try:
            datos_cliente = _autocompletar_cliente(cedula_digitos)
        except Exception as e:
            datos_cliente = None
            print(f"⚠️ [ver_historial_cobro] No se pudo autocompletar los datos del cliente: {e}")
        if datos_cliente:
            if datos_cliente.get("nombre"):
                valores_view["descripcion"] = {"valor": {"value": datos_cliente["nombre"]}}
            if datos_cliente.get("telefono"):
                valores_view["numero"] = {"valor": {"value": datos_cliente["telefono"]}}
            if datos_cliente.get("nombre") or datos_cliente.get("telefono"):
                partes_texto.append("✅ *Cliente encontrado:* se rellenaron los datos conocidos "
                                     "(puedes corregirlos si hace falta).")
        # ---- Historial reciente (como ya funcionaba) ----
        try:
            sheet = _abrir_hoja_pagos_recibidos_cobro()
        except Exception as e:
            sheet = None
            print(f"⚠️ [ver_historial_cobro] No se pudo abrir la hoja: {e}")
        historial = _historial_reciente_cliente(sheet, cedula_digitos)
        if historial:
            lineas = "\n".join(f"• {f} — {m}" for f, m in historial)
            partes_texto.append(f"📜 *Historial reciente de la cédula {cedula_input}:*\n{lineas}")
        else:
            partes_texto.append(f"📜 No encontré cobros anteriores de la cédula {cedula_input} en 'Pagos Recibidos'.")
        # ---- Score de riesgo (cumplimiento de promesas anteriores) ----
        try:
            score = _score_riesgo_cliente(cedula_digitos)
        except Exception as e:
            score = None
            print(f"⚠️ [ver_historial_cobro] No se pudo calcular el score de riesgo: {e}")
        if score:
            partes_texto.append(f"📊 *Score de riesgo:* {score}")
        texto_extra = "\n\n".join(partes_texto)
    spec = FORM_SPECS["cobro"]
    try:
        client.views_update(
            view_id=body["view"]["id"],
            hash=body["view"]["hash"],
            view={
                "type": "modal",
                "callback_id": spec["callback_id"],
                "title": {"type": "plain_text", "text": spec["titulo"]},
                "submit": {"type": "plain_text", "text": "Enviar"},
                "blocks": _construir_blocks_formulario(spec, valores_view, texto_extra),
            }
        )
    except Exception as e:
        print(f"⚠️ [ver_historial_cobro] No se pudo actualizar el modal: {e}")
# ============ FIN BOTÓN "Ver historial del cliente" ============


# Extraído de 'recibir_cobro' para poder reusarlo tal cual desde el handler de edición
# (ver "editar_cobro" / "editar_cobro_formulario" más abajo) — así el mensaje corregido se
# ve exactamente igual de armado que el original, sin duplicar el formato en dos lugares.
def _texto_cobro_base(fecha, nombre_cobrador, cobrador_slack, descripcion, cedula, numero,
                       forma_pago, banco, monto_bs_fmt, monto_usd_str, tasa_bcv_str):
    return (
        f"💰 *Nuevo cobro — {monto_usd_str} USD*\n"
        f"*{descripcion} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Cobrador:* {nombre_cobrador} (<@{cobrador_slack}>)\n"
        f"📱 *Teléfono:* {numero}\n"
        f"🏦 *Pago:* {forma_pago} · {banco}\n"
        f"💵 *Monto:* {monto_bs_fmt}  (≈ {monto_usd_str})\n"
        f"📊 *Tasa BCV:* {tasa_bcv_str}"
    )


@app.view("form_cobro")
def recibir_cobro(ack, body, client):
    # ---- Todo lo de acá abajo va en un try/except a propósito: es la única parte del bot
    # donde se hace un trabajo "pesado" (buscar la tasa BCV en Google Sheets, con su propia
    # consulta en vivo si el caché venció) ANTES de llamar a ack(). Slack le da al bot solo
    # 3 segundos para confirmar que recibió el envío del formulario — si Google Sheets se
    # demora, o si cualquiera de estos pasos lanza un error inesperado, sin este try/except
    # el código nunca llegaría a la línea de ack() y el formulario del usuario se quedaría
    # "colgado" (con el aviso de Slack de 'didn't call ack()' en los logs), sin haberse
    # guardado el cobro y sin que la persona sepa qué pasó. Con el try/except, pase lo que
    # pase, siempre se le responde algo a Slack a tiempo.
    try:
        _v = body["view"]["state"]["values"]
        _err = _validar_formulario_generico("cobro", _v)

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
    except Exception as e:
        print(f"❌ [recibir_cobro] Error inesperado antes de confirmar el envío: {type(e).__name__}: {e}")
        ack(response_action="errors", errors={
            "monto_bs": "⚠️ Hubo un error inesperado procesando el formulario. Cierra el formulario e intenta de nuevo en unos segundos."
        })
        return
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
    # ============ MENSAJE REDISEÑADO (Fase: mensajes más amigables) ============
    # Formato "resumen arriba, detalle abajo": lo primero que se lee es el dato que más le
    # importa a quien aprueba (cuánto es, en $), luego el cliente, y el resto del detalle
    # agrupado debajo de una línea divisoria — en vez de diez líneas de "*Etiqueta:* valor"
    # seguidas. El monto se muestra en Bs. Y en $ juntos en la misma línea (pedido explícito),
    # junto con la Tasa BCV usada para ese cálculo.
    texto = _texto_cobro_base(fecha, nombre_cobrador, cobrador_slack, descripcion, cedula,
                               numero, forma_pago, banco, monto_bs_fmt, monto_usd_str, tasa_bcv_str)
    # ============ FIN MENSAJE REDISEÑADO ============

    # ============ AVISO DE POSIBLE DUPLICADO (mismo cliente, misma semana) ============
    # Igual que en el resto de los comandos de cobro (motor_formularios.py): usa 🔁, no ⚠,
    # para no confundir a _ya_procesado() (que usa ✅/❌/⚠ para saber si un mensaje ya fue
    # aprobado o rechazado).
    boton_aprobar = {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar"}
    try:
        sheet_dup = _abrir_hoja_pagos_recibidos_cobro()
    except Exception as e:
        sheet_dup = None
        print(f"⚠️ [cobro] No se pudo abrir la hoja para revisar duplicado: {e}")
    fecha_duplicado = _buscar_duplicado_reciente(sheet_dup, "Cedula", "Fecha", cedula, "cedula")
    if fecha_duplicado:
        texto = (f"🔁 *POSIBLE DUPLICADO* — ya hay un registro con esta cédula esta semana "
                 f"(fecha: {fecha_duplicado}).\n\n" + texto)
        boton_aprobar["confirm"] = {
            "title": {"type": "plain_text", "text": "Confirmar posible duplicado"},
            "text": {"type": "mrkdwn", "text": (
                f"Ya hay otro registro con esta cédula esta semana (fecha: {fecha_duplicado}). "
                f"¿Aprobar de todas formas?")},
            "confirm": {"type": "plain_text", "text": "Sí, aprobar de todas formas"},
            "deny": {"type": "plain_text", "text": "Cancelar"},
        }
    # ============ FIN AVISO DE POSIBLE DUPLICADO ============

    # ============ HISTORIAL RECIENTE DEL CLIENTE ============
    # Se agrega abajo del mensaje, como contexto extra para quien aprueba — no cambia nada
    # de lo que ya se guarda ni de cómo se aprueba/rechaza el cobro.
    historial = _historial_reciente_cliente(sheet_dup, _solo_digitos(cedula))
    if historial:
        lineas_historial = "\n".join(f"   • {f} — {m}" for f, m in historial)
        texto += f"\n\n📜 *Historial reciente de este cliente ({len(historial)}):*\n{lineas_historial}"
    # ============ FIN HISTORIAL RECIENTE DEL CLIENTE ============

    client.chat_postMessage(
        channel="#cobranzas-log",
        text="Nuevo cobro reportado",
        metadata={"event_type": "cobro_reportado", "event_payload": {
            "fecha": fecha, "cobrador": nombre_cobrador, "descripcion": descripcion,
            "numero": numero, "cedula": cedula, "monto_bs": monto_bs_fmt,
            "forma_pago": forma_pago, "banco": banco, "tasa_bcv": tasa_bcv_str, "monto_usd": monto_usd_str,
            # Quién lo reportó de verdad en Slack (distinto de "cobrador", que es el nombre
            # elegido en el desplegable) — así, si alguien lo edita después, el mensaje sigue
            # diciendo "Cobrador: ... (@quien lo reportó)" y no se le atribuye a quien corrigió.
            "_reportado_por": cobrador_slack}},
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
            {"type": "actions", "elements": [
                boton_aprobar,
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Editar"}, "action_id": "editar_cobro"},
            ]}
        ]
    )
    _registrar_metrica("cobro", "enviado")


@app.action("aprobar")
def aprobar(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    _registrar_metrica("cobro", "aprobado")
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
        if resultado == "OK":
            _blocks_msg = body["message"].get("blocks", [])
            _registrar_aprobacion_para_deshacer({
                "abrir_hoja": _abrir_hoja_pagos_recibidos_cobro,
                "columna_id_registro": "ID Registro",
                "registro_id": registro_id,
                "canal": body["channel"]["id"],
                "ts": body["message"]["ts"],
                "texto_original": texto_original,
                "blocks_accion_original": _blocks_msg[1] if len(_blocks_msg) > 1
                    else {"type": "actions", "elements": []},
                "aprobado_por": body["user"]["id"],
                "resumen": "Cobro",
            })
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
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    _registrar_metrica("cobro", "rechazado")
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text="Cobro RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )


# ============ BOTÓN "✏️ Editar" ANTES DE APROBAR — versión propia de /cobro ============
# /cobro no usa el motor genérico para aprobar/rechazar (guarda con 'guardar_en_sheet' y
# arma su propio texto a mano), así que reusa el mecanismo genérico de edición SOLO para
# armar y prellenar el modal (_valores_view_desde_metadata) — el guardado de la corrección
# está escrito a mano aquí, siguiendo exactamente la misma lógica que 'recibir_cobro'
# (recalcula la Tasa BCV y el Monto USD para la fecha elegida, valida lo mismo).
@app.action("editar_cobro")
def editar_cobro(ack, body, client):
    ack()
    spec = FORM_SPECS["cobro"]
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return  # ya fue aprobado/rechazado — no tiene caso editar algo que ya se resolvió
    meta = dict(body["message"].get("metadata", {}).get("event_payload", {}))
    reportado_por = meta.get("_reportado_por") or body["user"]["id"]
    # Mapeo: el campo del modal se llama 'nombre_cobrador'/'fecha_pago', pero en la metadata
    # quedaron guardados como 'cobrador'/'fecha' (ver 'recibir_cobro' más arriba).
    valores_view = _valores_view_desde_metadata(
        spec, meta, mapeo={"nombre_cobrador": "cobrador", "fecha_pago": "fecha"})
    privado = json.dumps({
        "canal": body["channel"]["id"],
        "ts": body["message"]["ts"],
        "reportado_por": reportado_por,
    })
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "editar_cobro_formulario",
                "private_metadata": privado,
                "title": {"type": "plain_text", "text": "Editar antes de aprobar"},
                "submit": {"type": "plain_text", "text": "Guardar cambios"},
                "blocks": _construir_blocks_formulario(spec, valores_view),
            }
        )
    except Exception as e:
        print(f"⚠️ [cobro] No se pudo abrir el formulario de edición: {e}")


@app.view("editar_cobro_formulario")
def recibir_edicion_cobro(ack, body, client):
    _v = body["view"]["state"]["values"]
    _err = _validar_formulario_generico("cobro", _v)

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

    try:
        privado = json.loads(body["view"]["private_metadata"])
    except Exception:
        return
    canal = privado.get("canal")
    ts = privado.get("ts")
    reportado_por = privado.get("reportado_por") or body["user"]["id"]
    editor = body["user"]["id"]

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
    fecha = fecha_pago_final
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"

    # 'reportado_por' (no 'editor') va en el lugar del cobrador_slack, para que el mensaje
    # corregido siga diciendo quién lo reportó ORIGINALMENTE — igual que hace el mecanismo
    # genérico de edición para el resto de los comandos.
    texto = _texto_cobro_base(fecha, nombre_cobrador, reportado_por, descripcion, cedula,
                               numero, forma_pago, banco, monto_bs_fmt, monto_usd_str, tasa_bcv_str)
    texto_con_aviso = f"✏️ *Editado por <@{editor}> antes de aprobar*\n\n{texto}"

    try:
        client.chat_update(
            channel=canal, ts=ts, text="Cobro corregido",
            metadata={"event_type": "cobro_reportado", "event_payload": {
                "fecha": fecha, "cobrador": nombre_cobrador, "descripcion": descripcion,
                "numero": numero, "cedula": cedula, "monto_bs": monto_bs_fmt,
                "forma_pago": forma_pago, "banco": banco, "tasa_bcv": tasa_bcv_str, "monto_usd": monto_usd_str,
                "_reportado_por": reportado_por}},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto_con_aviso}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary", "action_id": "aprobar"},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger", "action_id": "rechazar"},
                    {"type": "button", "text": {"type": "plain_text", "text": "✏️ Editar"}, "action_id": "editar_cobro"},
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ [cobro] Error guardando la corrección: {e}")
# ============ FIN BOTÓN "✏️ Editar" ANTES DE APROBAR (/cobro) ============
# ============ FIN COMANDO /cobro ============



# ============ COMANDO /domiciliar (usando el Motor Genérico) ============
def _abrir_hoja_domiciliacion():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() in ("domiciliación", "domiciliacion"):
            return ws
    print(f"❌ No se encontró la hoja 'Domiciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
    return None


def _calcular_domiciliacion(datos):
    """Calcula y formatea Monto Bs / Monto USD / Cuenta por cobrar, igual que hacía
    el comando antes de esta migración (mismas cuentas, mismo formato Bs./$)."""
    monto_bs_str = datos.get("monto_bs", "")
    tasa_bcv_str = datos.get("tasa_bcv", "")
    cuenta_str = datos.get("cuenta", "")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    try:
        cuenta_num = parse_numero(cuenta_str)
        cuenta_fmt = f"Bs. {cuenta_num:,.2f}"
    except (ValueError, AttributeError):
        cuenta_fmt = f"Bs. {cuenta_str}"
    return {"monto_bs": monto_bs_fmt, "monto_usd": monto_usd_str, "cuenta": cuenta_fmt}


# ============ MENSAJE REDISEÑADO (mismo estilo que /cobro, /merca-reporte y /contactar) ============
# Lo que más le importa a quien aprueba una domiciliación es cuánto se recuperó, en $ — igual
# que en /cobro — así que va arriba en el resumen, junto con el nombre de la empresa en negrita.
def _texto_domiciliar_v2(datos_campos, fecha, usuario_slack):
    empresa = datos_campos.get("empresa", "")
    cuenta = datos_campos.get("cuenta", "")
    monto_bs = datos_campos.get("monto_bs", "")
    banco = datos_campos.get("banco", "")
    tasa_bcv = datos_campos.get("tasa_bcv", "")
    monto_usd = datos_campos.get("monto_usd", "")
    cobrador = datos_campos.get("cobrador", "")
    return (
        f"🏦 *Nueva domiciliación — {monto_usd}*\n"
        f"*{empresa}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"🧑‍💼 *Cobrador:* {cobrador}\n"
        f"🧾 *Cuenta por cobrar:* {cuenta}\n"
        f"🏛️ *Banco:* {banco}\n"
        f"💵 *Monto:* {monto_bs}  (≈ {monto_usd})\n"
        f"📊 *Tasa BCV:* {tasa_bcv}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["domiciliar"] = {
    "callback_id": "form_domiciliar",
    "titulo": "Registrar Domiciliación",
    "campos": [
        {"id": "empresa", "label": "Empresa", "tipo": "texto"},
        {"id": "cuenta", "label": "Cuenta por cobrar", "tipo": "texto"},
        {"id": "monto_bs", "label": "Monto en Bs", "tipo": "texto", "validar": "monto"},
        {"id": "banco", "label": "Banco", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
            {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
            {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
            {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
            {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
            {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
            {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
            {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
            {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
            {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
            {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "tasa_bcv", "label": "Tasa BCV (Bs por USD)", "tipo": "texto"},
        {"id": "cobrador", "label": "Cobrador", "tipo": "select", "opciones": _opciones_cobradores},
    ],
    "calcular": _calcular_domiciliacion,
    "abrir_hoja": _abrir_hoja_domiciliacion,
    "agregar_fecha": "Fecha",
    "columnas": {
        "empresa": "Empresa", "cuenta": "Cuenta por cobrar Bs", "monto_bs": "Monto recuperado Bs",
        "banco": "Banco", "monto_usd": "MontoUsd", "tasa_bcv": "TasaBCV", "cobrador": "Cobrador",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "DOMIC",
    "accion_id": "domiciliacion",
    "verificar_duplicado": {
        "campo": "empresa", "columna": "Empresa", "columna_fecha": "Fecha",
        "modo": "texto", "etiqueta": "empresa",
    },
    # La Tasa BCV se escribe a mano aquí (no viene de /tasa-hoy) — se compara contra la
    # tasa oficial de hoy para atajar typos (ver motor_formularios.py, _publicar_para_aprobacion).
    "verificar_tasa": {"campo_tasa": "tasa_bcv", "obtener_oficial": lambda datos: _tasa_de_hoy()},
    "boton_historial": "ver_historial_domiciliar",
    "canal": "#cobranzas-domiciliacion",
    "titulo_mensaje": "Nueva domiciliación reportada",
    "emoji_mensaje": "🏦",
    "campos_mensaje": [
        ("Empresa", "empresa"), ("Cuenta por cobrar", "cuenta"), ("Monto Bs", "monto_bs"),
        ("Banco", "banco"), ("Tasa BCV", "tasa_bcv"), ("Monto USD", "monto_usd"), ("Cobrador", "cobrador"),
    ],
    "construir_texto": _texto_domiciliar_v2,
}


@app.command("/domiciliar")
def reportar_domiciliacion(ack, body, client):
    ack()
    _abrir_formulario_generico("domiciliar", body["trigger_id"], client)


@app.view("form_domiciliar")
def recibir_domiciliacion(ack, body, client):
    ack()
    _publicar_para_aprobacion("domiciliar", body, client)


@app.action("aprobar_domiciliacion")
def aprobar_domiciliacion(ack, body, client):
    ack()
    _aprobar_generico("domiciliar", body, client)


@app.action("rechazar_domiciliacion")
def rechazar_domiciliacion(ack, body, client):
    ack()
    _rechazar_generico("domiciliar", body, client)


@app.action("editar_domiciliacion")
def editar_domiciliacion(ack, body, client):
    ack()
    _editar_generico("domiciliar", body, client)


# Botón "Ver historial" (genérico — ver motor_formularios.py). Aquí busca por empresa
# (no por cédula, ya que /domiciliar no maneja clientes individuales).
_handler_historial_domiciliar = _construir_handler_historial(
    "domiciliar", ["Monto recuperado Bs", "MontoUsd"])


@app.action("ver_historial_domiciliar")
def ver_historial_domiciliar(ack, body, client):
    _handler_historial_domiciliar(ack, body, client)
# ============ FIN COMANDO /domiciliar ============



# ============ COMANDO /cobro-callcenter (Call Center Seguros) ============
def _abrir_hoja_cobro2():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_COBRO2)
    try:
        return spreadsheet.worksheet("Hoja1")
    except Exception:
        return spreadsheet.sheet1


def _calcular_monto_usd(datos):
    """Calcula y formatea Monto Bs / Monto USD a partir de Monto Bs y Tasa BCV — mismo
    cálculo que usan /domiciliar, /cobro-callcenter, /cobro-comercial y /cobro."""
    monto_bs_str = datos.get("monto_bs", "")
    tasa_bcv_str = datos.get("tasa_bcv", "")
    try:
        monto_bs_num = parse_numero(monto_bs_str)
        tasa_bcv_num = parse_numero(tasa_bcv_str)
        monto_usd_str = f"${monto_bs_num/tasa_bcv_num:,.2f}"
        monto_bs_fmt = f"Bs. {monto_bs_num:,.2f}"
    except (ValueError, ZeroDivisionError):
        monto_usd_str = "(No calculable)"
        monto_bs_fmt = f"Bs. {monto_bs_str}"
    return {"monto_bs": monto_bs_fmt, "monto_usd": monto_usd_str}


# ============ MENSAJE REDISEÑADO (mismo estilo que /cobro) ============
def _texto_cobro_callcenter_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    cedula = datos_campos.get("cedula", "")
    telefono = datos_campos.get("telefono", "")
    monto_bs = datos_campos.get("monto_bs", "")
    forma_pago = datos_campos.get("forma_pago", "")
    banco = datos_campos.get("banco", "")
    tasa_bcv = datos_campos.get("tasa_bcv", "")
    monto_usd = datos_campos.get("monto_usd", "")
    referencia = datos_campos.get("referencia", "")
    return (
        f"📞💰 *Cobro Call Center — {monto_usd}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"🏦 *Pago:* {forma_pago} · {banco}\n"
        f"💵 *Monto:* {monto_bs}  (≈ {monto_usd})\n"
        f"📊 *Tasa BCV:* {tasa_bcv}\n"
        f"🔖 *N° Referencia:* {referencia}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["cobro_callcenter"] = {
    "callback_id": "form_cobro2",
    "titulo": "Cobro Call Center",
    "campos": [
        {"id": "nombre", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "cedula", "label": "Cédula del Cliente", "tipo": "texto", "validar": "cedula"},
        {"id": "telefono", "label": "Teléfono", "tipo": "texto", "validar": "telefono"},
        {"id": "monto_bs", "label": "Monto en Bs", "tipo": "texto", "validar": "monto"},
        {"id": "forma_pago", "label": "Forma de Pago", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
            {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
            {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
            {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "banco", "label": "Banco", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
            {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
            {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
            {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
            {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
            {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
            {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
            {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
            {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
            {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
            {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "tasa_bcv", "label": "Tasa BCV (Bs por USD)", "tipo": "texto"},
        {"id": "referencia", "label": "N° de referencia del pago", "tipo": "texto"},
    ],
    "calcular": _calcular_monto_usd,
    "abrir_hoja": _abrir_hoja_cobro2,
    "agregar_fecha": "Fecha",
    "columnas": {
        "nombre": "Nombre", "telefono": "Telefono", "cedula": "Cedula",
        "monto_bs": "MontoBs", "forma_pago": "FormaPago", "banco": "Banco",
        "monto_usd": "MontoUsd", "tasa_bcv": "TasaBCV", "referencia": "Nº referencia pago",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "CALLCENTER",
    "accion_id": "cobro2",
    "verificar_duplicado": {
        "campo": "cedula", "columna": "Cedula", "columna_fecha": "Fecha",
        "modo": "cedula", "etiqueta": "cédula",
    },
    # La Tasa BCV se escribe a mano aquí (no viene de /tasa-hoy) — se compara contra la
    # tasa oficial de hoy para atajar typos (ver motor_formularios.py, _publicar_para_aprobacion).
    "verificar_tasa": {"campo_tasa": "tasa_bcv", "obtener_oficial": lambda datos: _tasa_de_hoy()},
    "boton_historial": "ver_historial_callcenter",
    "boton_historial_label": "🔍 Buscar cliente / ver historial",
    "canal": "C0BAS4M970S",
    "titulo_mensaje": "Nuevo cobro reportado (Call Center)",
    "emoji_mensaje": "📞💰",
    "campos_mensaje": [
        ("Cliente", "nombre"), ("Cédula", "cedula"), ("Teléfono", "telefono"),
        ("Monto Bs", "monto_bs"), ("Forma de Pago", "forma_pago"), ("Banco", "banco"),
        ("Tasa BCV", "tasa_bcv"), ("Monto USD", "monto_usd"), ("N° referencia pago", "referencia"),
    ],
    "construir_texto": _texto_cobro_callcenter_v2,
}


@app.command("/cobro-callcenter")
def reportar_cobro2(ack, body, client):
    ack()
    _abrir_formulario_generico("cobro_callcenter", body["trigger_id"], client)


@app.view("form_cobro2")
def recibir_cobro2(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("cobro_callcenter", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("cobro_callcenter", body, client)


@app.action("aprobar_cobro2")
def aprobar_cobro2(ack, body, client):
    ack()
    _aprobar_generico("cobro_callcenter", body, client)


@app.action("rechazar_cobro2")
def rechazar_cobro2(ack, body, client):
    ack()
    _rechazar_generico("cobro_callcenter", body, client)


@app.action("editar_cobro2")
def editar_cobro2(ack, body, client):
    ack()
    _editar_generico("cobro_callcenter", body, client)


# Botón "Ver historial" (genérico — ver motor_formularios.py). Busca por cédula, y de paso
# rellena nombre/teléfono si ya se conoce al cliente (campos "nombre"/"telefono" coinciden
# con el mapeo por defecto, no hace falta indicar mapeo_autocompletar).
_handler_historial_callcenter = _construir_handler_historial(
    "cobro_callcenter", ["MontoBs", "MontoUsd", "Nº referencia pago"],
    autocompletar=_autocompletar_cliente, calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_callcenter")
def ver_historial_callcenter(ack, body, client):
    _handler_historial_callcenter(ack, body, client)
# ============ FIN COMANDO /cobro-callcenter ============



# ============ COMANDO /conciliar (usando el Motor Genérico) ============
def _abrir_hoja_conciliacion():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() in ("conciliación", "conciliacion"):
            return ws
    print(f"❌ No se encontró la hoja 'Conciliación'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
    return None


def _calcular_conciliacion(datos):
    """Calcula la diferencia entre lo reportado y lo que dice el banco, y decide el
    Estado (Conciliado / Con diferencia / Revisar manualmente) — mismo cálculo y mismos
    umbrales que usaba el comando antes de esta migración."""
    monto_reportado_str = datos.get("monto_reportado", "")
    monto_banco_str = datos.get("monto_banco", "")
    try:
        rep_num = parse_numero(monto_reportado_str)
        banco_num = parse_numero(monto_banco_str)
        diferencia_num = banco_num - rep_num
        monto_reportado_fmt = f"Bs. {rep_num:,.2f}"
        monto_banco_fmt = f"Bs. {banco_num:,.2f}"
        diferencia_fmt = f"Bs. {diferencia_num:,.2f}"
        if abs(diferencia_num) < 0.01:
            estado, emoji_estado = "Conciliado", "✅"
        else:
            estado, emoji_estado = "Con diferencia", "⚠️"
    except (ValueError, AttributeError):
        monto_reportado_fmt = f"Bs. {monto_reportado_str}"
        monto_banco_fmt = f"Bs. {monto_banco_str}"
        diferencia_fmt = "(No calculable)"
        estado, emoji_estado = "Revisar manualmente", "❓"
    return {
        "monto_reportado": monto_reportado_fmt,
        "monto_banco": monto_banco_fmt,
        "diferencia": diferencia_fmt,
        "estado": estado,  # se guarda en el Sheet, sin emoji (igual que antes)
        "estado_mostrado": f"{emoji_estado} {estado}",  # solo para el mensaje en Slack
    }


# ============ MENSAJE REDISEÑADO (mismo estilo que /cobro, /merca-reporte, /contactar y
# /domiciliar). Aquí no hay un monto en $ que resaltar — lo que más le importa a quien revisa
# es el ESTADO de la conciliación (Conciliado / Con diferencia / Revisar manualmente), así que
# ese va arriba en el resumen, junto con el cliente y la cédula en negrita. ============
def _texto_conciliar_v2(datos_campos, fecha, usuario_slack):
    cliente = datos_campos.get("cliente", "")
    cedula = datos_campos.get("cedula", "")
    referencia = datos_campos.get("referencia", "")
    banco = datos_campos.get("banco", "")
    monto_reportado = datos_campos.get("monto_reportado", "")
    monto_banco = datos_campos.get("monto_banco", "")
    diferencia = datos_campos.get("diferencia", "")
    estado_mostrado = datos_campos.get("estado_mostrado", "")
    fecha_movimiento = datos_campos.get("fecha_movimiento", "")
    conciliador = datos_campos.get("conciliador", "")
    observaciones = datos_campos.get("observaciones", "")
    texto = (
        f"🧾 *Conciliación — {estado_mostrado}*\n"
        f"*{cliente} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha movimiento banco:* {fecha_movimiento}\n"
        f"👤 *Conciliador:* {conciliador} (<@{usuario_slack}>)\n"
        f"🏦 *Banco:* {banco}\n"
        f"🔖 *N° Referencia:* {referencia}\n"
        f"💵 *Reportado:* {monto_reportado}  ·  *Según banco:* {monto_banco}\n"
        f"⚖️ *Diferencia:* {diferencia}"
    )
    if observaciones:
        texto += f"\n📝 *Observaciones:* {observaciones}"
    return texto
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["conciliar"] = {
    "callback_id": "form_conciliar",
    "titulo": "Conciliar Pago",
    "campos": [
        {"id": "cliente", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "cedula", "label": "Cédula del Cliente", "tipo": "texto", "validar": "cedula"},
        {"id": "referencia", "label": "N° de referencia del pago", "tipo": "texto"},
        {"id": "banco", "label": "Banco", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
            {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
            {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
            {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
            {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
            {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
            {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
            {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
            {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
            {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
            {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "monto_reportado", "label": "Monto reportado (Bs)", "tipo": "texto", "validar": "monto"},
        {"id": "monto_banco", "label": "Monto según el banco (Bs)", "tipo": "texto", "validar": "monto"},
        {"id": "fecha_movimiento", "label": "Fecha del movimiento bancario (DD/MM/YYYY)", "tipo": "texto", "validar": "fecha"},
        {"id": "conciliador", "label": "Conciliador", "tipo": "select", "opciones": _opciones_cobradores},
        {"id": "observaciones", "label": "Observaciones", "tipo": "texto", "multiline": True, "opcional": True},
    ],
    "calcular": _calcular_conciliacion,
    "abrir_hoja": _abrir_hoja_conciliacion,
    "agregar_fecha": "Fecha conciliación",
    "columnas": {
        "cliente": "Cliente", "cedula": "Cédula", "referencia": "Referencia", "banco": "Banco",
        "monto_reportado": "Monto reportado", "monto_banco": "Monto banco", "diferencia": "Diferencia",
        "estado": "Estado", "fecha_movimiento": "Fecha movimiento", "conciliador": "Conciliador",
        "observaciones": "Observaciones",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "CONC",
    "accion_id": "conciliacion",
    "verificar_duplicado": {
        "campo": "cedula", "columna": "Cédula", "columna_fecha": "Fecha conciliación",
        "modo": "cedula", "etiqueta": "cédula",
    },
    "boton_historial": "ver_historial_conciliar",
    "boton_historial_label": "🔍 Buscar cliente / ver historial",
    "canal": "#cobranzas-conciliar",
    "titulo_mensaje": "Nueva conciliación reportada",
    "emoji_mensaje": "🧾",
    "campos_mensaje": [
        ("Cliente", "cliente"), ("Cédula", "cedula"), ("N° referencia pago", "referencia"),
        ("Banco", "banco"), ("Monto reportado", "monto_reportado"), ("Monto según banco", "monto_banco"),
        ("Diferencia", "diferencia"), ("Estado", "estado_mostrado"),
        ("Fecha movimiento banco", "fecha_movimiento"), ("Conciliador", "conciliador"),
        ("Observaciones", "observaciones"),
    ],
    "construir_texto": _texto_conciliar_v2,
}


@app.command("/conciliar")
def reportar_conciliacion(ack, body, client):
    ack()
    _abrir_formulario_generico("conciliar", body["trigger_id"], client)


@app.view("form_conciliar")
def recibir_conciliacion(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("conciliar", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("conciliar", body, client)


@app.action("aprobar_conciliacion")
def aprobar_conciliacion(ack, body, client):
    ack()
    _aprobar_generico("conciliar", body, client)


@app.action("rechazar_conciliacion")
def rechazar_conciliacion(ack, body, client):
    ack()
    _rechazar_generico("conciliar", body, client)


@app.action("editar_conciliacion")
def editar_conciliacion(ack, body, client):
    ack()
    _editar_generico("conciliar", body, client)


# Botón "Ver historial" (genérico — ver motor_formularios.py). Busca por cédula, y de paso
# rellena el nombre si ya se conoce al cliente (aquí el campo se llama "cliente", no "nombre",
# y /conciliar no tiene campo de teléfono — de ahí el mapeo_autocompletar).
_handler_historial_conciliar = _construir_handler_historial(
    "conciliar", ["Monto reportado", "Monto banco", "Estado"],
    autocompletar=_autocompletar_cliente, mapeo_autocompletar={"nombre": "cliente"},
    calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_conciliar")
def ver_historial_conciliar(ack, body, client):
    _handler_historial_conciliar(ack, body, client)
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
    for ws in spreadsheet.worksheets():
        if _normalizar_encabezado(ws.title) == _normalizar_encabezado("Liquidacion VIP"):
            return ws
    try:
        return spreadsheet.worksheet("Hoja1")
    except Exception:
        return spreadsheet.sheet1


def _opciones_lista(lista):
    return [{"text": {"type": "plain_text", "text": x}, "value": x} for x in lista]


# ============ MENSAJE REDISEÑADO (mismo estilo que el resto de comandos rediseñados) ============
def _texto_liquidacion_nueva_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    cedula = datos_campos.get("cedula", "")
    cliente = datos_campos.get("cliente", "")
    base = datos_campos.get("base", "")
    estatus = datos_campos.get("estatus", "")
    return (
        f"🌟 *Nueva persona en Lista VIP — {estatus}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"🏢 *Cliente/Empresa:* {cliente}\n"
        f"📂 *Base:* {base}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["liquidacion_nueva"] = {
    "callback_id": "form_liquidacion_nueva",
    "titulo": "Nueva Liquidación",
    "campos": [
        {"id": "nombre", "label": "Nombre completo", "tipo": "texto"},
        {"id": "cedula", "label": "Cédula", "tipo": "texto", "validar": "cedula"},
        {"id": "cliente", "label": "Cliente / Empresa", "tipo": "texto"},
        {"id": "base", "label": "Base", "tipo": "select", "opciones": lambda: _opciones_lista(BASES_LIQUIDACION)},
        {"id": "estatus", "label": "Estatus inicial", "tipo": "select", "opciones": lambda: _opciones_lista(ESTATUS_LIQUIDACION)},
    ],
    "abrir_hoja": _abrir_hoja_liquidaciones,
    "agregar_fecha": ["Fecha de Registro", "Ultima actualizacion"],
    "columnas": {
        "nombre": "Nombre", "cedula": "Cedula", "cliente": "Clientes/Empresas",
        "base": "Base", "estatus": "Estatus",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "LIQNUEVA",
    "verificar_duplicado": {
        "campo": "cedula", "columna": "Cedula", "columna_fecha": "Fecha de Registro",
        "modo": "cedula", "etiqueta": "cédula",
    },
    "canal": CANAL_LIQUIDACIONES,
    "titulo_mensaje": "Nueva persona en Lista VIP",
    "emoji_mensaje": "🌟",
    "campos_mensaje": [
        ("Nombre", "nombre"), ("Cédula", "cedula"), ("Cliente/Empresa", "cliente"),
        ("Base", "base"), ("Estatus", "estatus"),
    ],
    "construir_texto": _texto_liquidacion_nueva_v2,
    "boton_historial": "ver_historial_liquidacion_nueva",
    "boton_historial_label": "🔍 Buscar cliente / ver historial",
}


def actualizar_estatus_liquidacion(cedula, nuevo_estatus, fecha_actualizacion):
    try:
        sheet = _abrir_hoja_liquidaciones()
        col_cedula = _columna_por_nombre(sheet, "Cedula")
        col_estatus = _columna_por_nombre(sheet, "Estatus")
        col_actualizacion = _columna_por_nombre(sheet, "Ultima actualizacion")
        if col_cedula is None or col_estatus is None or col_actualizacion is None:
            print("❌ Error actualizando estatus: faltan las columnas 'Cedula', 'Estatus' y/o "
                  "'Ultima actualizacion' en la pestaña de Liquidaciones.")
            return False
        valores = sheet.get_all_values()
        cedula_buscada = str(cedula).strip()
        idx_cedula = col_cedula - 1
        for i, fila in enumerate(valores):
            if i == 0:
                continue
            if len(fila) > idx_cedula and fila[idx_cedula].strip() == cedula_buscada:
                num_fila = i + 1
                sheet.update_cell(num_fila, col_estatus, nuevo_estatus)
                sheet.update_cell(num_fila, col_actualizacion, fecha_actualizacion)
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
    _abrir_formulario_generico("liquidacion_nueva", body["trigger_id"], client)


@app.view("form_liquidacion_nueva")
def recibir_liquidacion_nueva(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("liquidacion_nueva", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("liquidacion_nueva", body, client)


@app.action("aprobar_liquidacion_nueva")
def aprobar_liquidacion_nueva(ack, body, client):
    ack()
    _aprobar_generico("liquidacion_nueva", body, client)


@app.action("rechazar_liquidacion_nueva")
def rechazar_liquidacion_nueva(ack, body, client):
    ack()
    _rechazar_generico("liquidacion_nueva", body, client)


@app.action("editar_liquidacion_nueva")
def editar_liquidacion_nueva(ack, body, client):
    ack()
    _editar_generico("liquidacion_nueva", body, client)


# Botón "Ver historial" (genérico — ver motor_formularios.py). Busca por cédula; rellena
# solo "nombre" (este formulario no tiene campo de teléfono).
_handler_historial_liquidacion_nueva = _construir_handler_historial(
    "liquidacion_nueva", ["Clientes/Empresas", "Estatus"],
    autocompletar=_autocompletar_cliente, mapeo_autocompletar={"nombre": "nombre"},
    calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_liquidacion_nueva")
def ver_historial_liquidacion_nueva(ack, body, client):
    _handler_historial_liquidacion_nueva(ack, body, client)


# Este comando ACTUALIZA una fila que ya existe (busca por cédula), no crea una fila
# nueva — por eso usa el motor solo para el formulario y la publicación con
# Aprobar/Rechazar (mismas piezas compartidas que el resto), pero el guardado real
# sigue siendo la función propia actualizar_estatus_liquidacion (arriba), porque
# "actualizar" es una operación distinta a "guardar_generico" (que siempre agrega
# una fila nueva).
# ============ MENSAJE REDISEÑADO (mismo estilo que el resto de comandos rediseñados) ============
def _texto_liquidacion_estatus_v2(datos_campos, fecha, usuario_slack):
    cedula = datos_campos.get("cedula", "")
    nombre = datos_campos.get("nombre", "")
    estatus = datos_campos.get("estatus", "")
    return (
        f"🔄 *Cambio de estatus — {estatus}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Solicitado por:* <@{usuario_slack}>"
    )
# ============ FIN MENSAJE REDISEÑADO ============


# ============ LLENADO AUTOMÁTICO PARA /liquidacion-estatus ============
# Distinto del resto: aquí lo que importa es encontrar a la persona ya existente en la
# Lista VIP (no en 'Pagos Recibidos'/'Contactados'), porque el campo "Nombre (referencia)"
# es solo para confirmar que se está cambiando el estatus de quien corresponde.
def _autocompletar_liquidacion(cedula_digitos):
    """Busca 'cedula_digitos' en la hoja de Liquidaciones (Lista VIP) y devuelve
    {"nombre": ...} si la encuentra, o None si no está en la lista."""
    if not cedula_digitos:
        return None
    try:
        sheet = _abrir_hoja_liquidaciones()
    except Exception as e:
        print(f"⚠️ [autocompletar_liquidacion] No se pudo abrir la hoja: {e}")
        return None
    if sheet is None:
        return None
    try:
        idx_ced = _columna_por_nombre(sheet, "Cedula")
        idx_nombre = _columna_por_nombre(sheet, "Nombre")
        if idx_ced is None:
            return None
        for fila in sheet.get_all_values()[1:]:
            if len(fila) > idx_ced - 1 and _solo_digitos(fila[idx_ced - 1]) == cedula_digitos:
                nombre = fila[idx_nombre - 1].strip() if idx_nombre and len(fila) > idx_nombre - 1 else ""
                if nombre:
                    return {"nombre": nombre}
    except Exception as e:
        print(f"⚠️ [autocompletar_liquidacion] Error buscando: {e}")
    return None
# ============ FIN LLENADO AUTOMÁTICO PARA /liquidacion-estatus ============


FORM_SPECS["liquidacion_estatus"] = {
    "callback_id": "form_liquidacion_estatus",
    "titulo": "Cambiar Estatus",
    "campos": [
        {"id": "cedula", "label": "Cédula de la persona", "tipo": "texto", "validar": "cedula"},
        {"id": "nombre", "label": "Nombre (referencia)", "tipo": "texto"},
        {"id": "estatus", "label": "Nuevo estatus", "tipo": "select", "opciones": lambda: _opciones_lista(ESTATUS_LIQUIDACION)},
    ],
    "accion_id": "liquidacion_estatus",
    "canal": CANAL_LIQUIDACIONES,
    "titulo_mensaje": "Cambio de estatus solicitado",
    "emoji_mensaje": "🔄",
    "campos_mensaje": [("Nombre", "nombre"), ("Cédula", "cedula"), ("Nuevo estatus", "estatus")],
    "construir_texto": _texto_liquidacion_estatus_v2,
    "boton_historial": "ver_historial_liquidacion_estatus",
    "boton_historial_label": "🔍 Buscar en Lista VIP",
}


@app.command("/liquidacion-estatus")
def reportar_liquidacion_estatus(ack, body, client):
    ack()
    _abrir_formulario_generico("liquidacion_estatus", body["trigger_id"], client)


@app.view("form_liquidacion_estatus")
def recibir_liquidacion_estatus(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("liquidacion_estatus", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("liquidacion_estatus", body, client)


@app.action("aprobar_liquidacion_estatus")
def aprobar_liquidacion_estatus(ack, body, client):
    ack()
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
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
    _rechazar_generico("liquidacion_estatus", body, client)


@app.action("editar_liquidacion_estatus")
def editar_liquidacion_estatus(ack, body, client):
    ack()
    _editar_generico("liquidacion_estatus", body, client)


# Botón "Buscar en Lista VIP" (rellena el nombre de referencia si la cédula ya está en la
# Lista VIP — sin historial, ya que este comando no crea registros nuevos).
_handler_autocompletar_liquidacion_estatus = _construir_handler_autocompletar(
    "liquidacion_estatus", "cedula", _autocompletar_liquidacion)


@app.action("ver_historial_liquidacion_estatus")
def ver_historial_liquidacion_estatus(ack, body, client):
    _handler_autocompletar_liquidacion_estatus(ack, body, client)
# ============ FIN COMANDOS DE LIQUIDACIONES ============



# ============ COMANDO /cobro-comercial (Equipo Comercial) ============
def _abrir_hoja_comercial():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_COMERCIAL)
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "pagos":
            return ws
    try:
        return spreadsheet.worksheet("Sheet1")
    except Exception:
        return spreadsheet.sheet1  # respaldo: la primera pestaña, por si cambia el nombre


# ============ MENSAJE REDISEÑADO (mismo estilo que /cobro) ============
def _texto_cobro_comercial_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    cedula = datos_campos.get("cedula", "")
    telefono = datos_campos.get("telefono", "")
    monto_bs = datos_campos.get("monto_bs", "")
    forma_pago = datos_campos.get("forma_pago", "")
    banco = datos_campos.get("banco", "")
    tasa_bcv = datos_campos.get("tasa_bcv", "")
    monto_usd = datos_campos.get("monto_usd", "")
    empresa = datos_campos.get("empresa", "")
    return (
        f"🤝💰 *Cobro Comercial — {monto_usd}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportado por:* <@{usuario_slack}>\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"🏢 *Empresa:* {empresa}\n"
        f"🏦 *Pago:* {forma_pago} · {banco}\n"
        f"💵 *Monto:* {monto_bs}  (≈ {monto_usd})\n"
        f"📊 *Tasa BCV:* {tasa_bcv}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["cobro_comercial"] = {
    "callback_id": "form_cobro_comercial",
    "titulo": "Cobro Comercial",
    "campos": [
        {"id": "nombre", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "cedula", "label": "Cédula del Cliente", "tipo": "texto", "validar": "cedula"},
        {"id": "telefono", "label": "Teléfono", "tipo": "texto", "validar": "telefono"},
        {"id": "monto_bs", "label": "Monto en Bs", "tipo": "texto", "validar": "monto"},
        {"id": "forma_pago", "label": "Forma de Pago", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Pago Móvil"}, "value": "Pago Movil"},
            {"text": {"type": "plain_text", "text": "Transferencia"}, "value": "Transferencia"},
            {"text": {"type": "plain_text", "text": "Efectivo"}, "value": "Efectivo"},
            {"text": {"type": "plain_text", "text": "Zelle"}, "value": "Zelle"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "banco", "label": "Banco", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "BDV - Banco de Venezuela"}, "value": "BDV"},
            {"text": {"type": "plain_text", "text": "BNC - Banco Nacional de Crédito"}, "value": "BNC"},
            {"text": {"type": "plain_text", "text": "BOD"}, "value": "BOD"},
            {"text": {"type": "plain_text", "text": "Mercantil"}, "value": "Mercantil"},
            {"text": {"type": "plain_text", "text": "Provincial"}, "value": "Provincial"},
            {"text": {"type": "plain_text", "text": "Bicentenario"}, "value": "Bicentenario"},
            {"text": {"type": "plain_text", "text": "Banesco"}, "value": "Banesco"},
            {"text": {"type": "plain_text", "text": "Banco Exterior"}, "value": "Banco Exterior"},
            {"text": {"type": "plain_text", "text": "Banco Plaza"}, "value": "Banco Plaza"},
            {"text": {"type": "plain_text", "text": "Banca Amiga"}, "value": "Banca Amiga"},
            {"text": {"type": "plain_text", "text": "Venezolano de Crédito"}, "value": "Venezolano de Credito"},
            {"text": {"type": "plain_text", "text": "Otro"}, "value": "Otro"},
        ]},
        {"id": "tasa_bcv", "label": "Tasa BCV (Bs por USD)", "tipo": "texto"},
        {"id": "empresa", "label": "Empresa", "tipo": "texto"},
    ],
    "calcular": _calcular_monto_usd,
    "abrir_hoja": _abrir_hoja_comercial,
    "agregar_fecha": "Fecha",
    "columnas": {
        "nombre": "Nombre Cliente", "telefono": "Telefono", "cedula": "Cedula",
        "monto_bs": "MontoBs", "forma_pago": "FormaPago", "banco": "Banco",
        "monto_usd": "MontoUsd", "tasa_bcv": "TasaBCV", "empresa": "Empresa",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "COMERCIAL",
    "accion_id": "comercial",
    "verificar_duplicado": {
        "campo": "cedula", "columna": "Cedula", "columna_fecha": "Fecha",
        "modo": "cedula", "etiqueta": "cédula",
    },
    # La Tasa BCV se escribe a mano aquí (no viene de /tasa-hoy) — se compara contra la
    # tasa oficial de hoy para atajar typos (ver motor_formularios.py, _publicar_para_aprobacion).
    "verificar_tasa": {"campo_tasa": "tasa_bcv", "obtener_oficial": lambda datos: _tasa_de_hoy()},
    "boton_historial": "ver_historial_comercial",
    "boton_historial_label": "🔍 Buscar cliente / ver historial",
    "canal": CANAL_COMERCIAL,
    "titulo_mensaje": "Nuevo cobro reportado (Comercial)",
    "emoji_mensaje": "🤝💰",
    "campos_mensaje": [
        ("Cliente", "nombre"), ("Cédula", "cedula"), ("Teléfono", "telefono"),
        ("Monto Bs", "monto_bs"), ("Forma de Pago", "forma_pago"), ("Banco", "banco"),
        ("Tasa BCV", "tasa_bcv"), ("Monto USD", "monto_usd"), ("Empresa", "empresa"),
    ],
    "construir_texto": _texto_cobro_comercial_v2,
}


@app.command("/cobro-comercial")
def reportar_cobro_comercial(ack, body, client):
    ack()
    _abrir_formulario_generico("cobro_comercial", body["trigger_id"], client)


@app.view("form_cobro_comercial")
def recibir_cobro_comercial(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("cobro_comercial", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("cobro_comercial", body, client)


@app.action("aprobar_comercial")
def aprobar_comercial(ack, body, client):
    ack()
    _aprobar_generico("cobro_comercial", body, client)


@app.action("rechazar_comercial")
def rechazar_comercial(ack, body, client):
    ack()
    _rechazar_generico("cobro_comercial", body, client)


@app.action("editar_comercial")
def editar_comercial(ack, body, client):
    ack()
    _editar_generico("cobro_comercial", body, client)


# Botón "Ver historial" (genérico — ver motor_formularios.py). Busca por cédula, y de paso
# rellena nombre/teléfono si ya se conoce al cliente.
_handler_historial_comercial = _construir_handler_historial(
    "cobro_comercial", ["MontoBs", "MontoUsd"], autocompletar=_autocompletar_cliente,
    calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_comercial")
def ver_historial_comercial(ack, body, client):
    _handler_historial_comercial(ack, body, client)
# ============ FIN COMANDO /cobro-comercial ============



# ============ COMANDO /contacto-legal (Equipo Legal) — usando el Motor Genérico ============
def _abrir_hoja_contactados_legal():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_LEGAL)
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "contactados":
            return ws
    print(f"❌ No se encontró la hoja 'Contactados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
    return None


# ============ MENSAJE REDISEÑADO (mismo estilo que /contactar) ============
def _texto_contacto_legal_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    telefono = datos_campos.get("telefono", "")
    cedula = datos_campos.get("cedula", "")
    compromiso = datos_campos.get("compromiso", "")
    cobrador = datos_campos.get("cobrador", "")
    comentario = datos_campos.get("comentario", "")
    return (
        f"⚖️ *Contacto Legal — Compromiso: {compromiso}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Cobrador:* {cobrador} (<@{usuario_slack}>)\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"💬 *Comentario:* {comentario}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["contacto_legal"] = {
    "callback_id": "form_contacto_legal",
    "titulo": "Contacto Legal",
    "campos": [
        {"id": "nombre", "label": "Nombre del Cliente", "tipo": "texto"},
        {"id": "telefono", "label": "Teléfono", "tipo": "texto", "validar": "telefono"},
        {"id": "cedula", "label": "Cédula", "tipo": "texto", "validar": "cedula"},
        {"id": "compromiso", "label": "Compromiso de pago (DD/MM/YYYY)", "tipo": "texto", "validar": "fecha"},
        {"id": "cobrador", "label": "Cobrador", "tipo": "select", "opciones": [
            {"text": {"type": "plain_text", "text": "Maria"}, "value": "Maria"},
            {"text": {"type": "plain_text", "text": "Gabriela"}, "value": "Gabriela"},
            {"text": {"type": "plain_text", "text": "Karolay"}, "value": "Karolay"},
        ]},
        {"id": "comentario", "label": "Comentario", "tipo": "texto", "multiline": True},
    ],
    "abrir_hoja": _abrir_hoja_contactados_legal,
    "agregar_fecha": "Fecha",
    "columnas": {
        "nombre": "Nombre", "telefono": "Telefono", "cedula": "Cedula",
        "compromiso": "Compromiso de pago", "cobrador": "Cobrador", "comentario": "COMENTARIO",
    },
    "columna_id_registro": "ID Registro",
    "anti_duplicado": True,
    "prefijo_id": "LEGAL",
    "canal": CANAL_LEGAL,
    "titulo_mensaje": "Nuevo contacto Legal",
    "emoji_mensaje": "⚖️",
    "campos_mensaje": [
        ("Cliente", "nombre"), ("Teléfono", "telefono"), ("Cédula", "cedula"),
        ("Compromiso de pago", "compromiso"), ("Cobrador", "cobrador"), ("Comentario", "comentario"),
    ],
    "construir_texto": _texto_contacto_legal_v2,
    "boton_historial": "ver_historial_contacto_legal",
    "boton_historial_label": "🔍 Buscar cliente",
}


@app.command("/contacto-legal")
def reportar_contacto_legal(ack, body, client):
    ack()
    _abrir_formulario_generico("contacto_legal", body["trigger_id"], client)


@app.view("form_contacto_legal")
def recibir_contacto_legal(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("contacto_legal", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _publicar_para_aprobacion("contacto_legal", body, client)


@app.action("aprobar_contacto_legal")
def aprobar_contacto_legal(ack, body, client):
    ack()
    _aprobar_generico("contacto_legal", body, client)


@app.action("rechazar_contacto_legal")
def rechazar_contacto_legal(ack, body, client):
    ack()
    _rechazar_generico("contacto_legal", body, client)


@app.action("editar_contacto_legal")
def editar_contacto_legal(ack, body, client):
    ack()
    _editar_generico("contacto_legal", body, client)


# Botón "Buscar cliente" (rellena nombre/teléfono si ya está registrado — sin historial,
# igual que /contactar, ya que este comando tampoco lleva "verificar_duplicado").
_handler_autocompletar_contacto_legal = _construir_handler_autocompletar(
    "contacto_legal", "cedula", _autocompletar_cliente, calcular_score=_score_riesgo_cliente)


@app.action("ver_historial_contacto_legal")
def ver_historial_contacto_legal(ack, body, client):
    _handler_autocompletar_contacto_legal(ack, body, client)
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




# ============ COMANDO /clientes-escalados (usando el Motor Genérico) ============
# Columnas: Fecha, Nombre del cliente, Teléfono, Cédula, Empresa, Incidencia, Reportada por
def _abrir_hoja_escalados():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    spreadsheet = cliente.open_by_key(SHEET_ID_ESCALADOS)
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == "clientes escalados":
            return ws
    print(f"❌ No se encontró la hoja 'Clientes escalados'. Hojas disponibles: {[ws.title for ws in spreadsheet.worksheets()]}")
    return None


# ============ MENSAJE REDISEÑADO (mismo estilo que el resto de comandos rediseñados) ============
def _texto_cliente_escalado_v2(datos_campos, fecha, usuario_slack):
    nombre = datos_campos.get("nombre", "")
    cedula = datos_campos.get("cedula", "")
    telefono = datos_campos.get("telefono", "")
    empresa = datos_campos.get("empresa", "")
    incidencia = datos_campos.get("incidencia", "")
    reportada_por = datos_campos.get("reportada_por", "")
    return (
        f"🚩 *Cliente escalado — {empresa}*\n"
        f"*{nombre} · Cédula {cedula}*\n"
        f"\n"
        f"──────────────────────────\n"
        f"📅 *Fecha:* {fecha}\n"
        f"👤 *Reportada por:* {reportada_por} (<@{usuario_slack}>)\n"
        f"📱 *Teléfono:* {telefono}\n"
        f"⚠️ *Incidencia:* {incidencia}"
    )
# ============ FIN MENSAJE REDISEÑADO ============


FORM_SPECS["clientes_escalados"] = {
    "callback_id": "form_cliente_escalado",
    "titulo": "Cliente Escalado",
    "campos": [
        {"id": "nombre", "label": "Nombre del cliente", "tipo": "texto"},
        {"id": "telefono", "label": "Teléfono del cliente", "tipo": "texto", "validar": "telefono"},
        {"id": "cedula", "label": "Cédula del cliente", "tipo": "texto", "validar": "cedula"},
        {"id": "empresa", "label": "Empresa", "tipo": "texto"},
        {"id": "incidencia", "label": "Incidencia (describe el problema)", "tipo": "texto", "multiline": True},
        {"id": "reportada_por", "label": "Reportada por", "tipo": "select", "opciones": _opciones_cobradores},
    ],
    "abrir_hoja": _abrir_hoja_escalados,
    "agregar_fecha": "Fecha",
    "columnas": {
        "nombre": "Nombre", "telefono": "Telefono", "cedula": "Cedula",
        "empresa": "Empresa", "incidencia": "Incidencia", "reportada_por": "Reportada por",
    },
    "canal": CANAL_ESCALADOS,
    "titulo_mensaje": "Nuevo cliente escalado",
    "emoji_mensaje": "🚩",
    "campos_mensaje": [
        ("Cliente", "nombre"), ("Teléfono", "telefono"), ("Cédula", "cedula"),
        ("Empresa", "empresa"), ("Incidencia", "incidencia"),
    ],
    "construir_texto": _texto_cliente_escalado_v2,
    "boton_historial": "ver_historial_escalados",
    "boton_historial_label": "🔍 Buscar cliente",
}


@app.command("/clientes-escalados")
def reportar_cliente_escalado(ack, body, client):
    ack()
    _abrir_formulario_generico("clientes_escalados", body["trigger_id"], client)


@app.view("form_cliente_escalado")
def recibir_cliente_escalado(ack, body, client):
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico("clientes_escalados", valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    _ejecutar_formulario_generico("clientes_escalados", body, client)


# Botón "Buscar cliente" (rellena nombre/teléfono si ya está registrado — sin historial).
_handler_autocompletar_escalados = _construir_handler_autocompletar(
    "clientes_escalados", "cedula", _autocompletar_cliente)


@app.action("ver_historial_escalados")
def ver_historial_escalados(ack, body, client):
    _handler_autocompletar_escalados(ack, body, client)
# ============ FIN COMANDO /clientes-escalados ============





def _buscar_columnas_monto(encabezados):
    """Ubica por NOMBRE las columnas de monto en Bs y en USD de una hoja (si existen),
    tolerante a variaciones ('MontoBs', 'Monto en Bs', 'Monto reportado', 'MontoUsd',
    'Monto en USD', etc. — 'encabezados' ya viene sin tildes y en minúsculas). Devuelve
    (idx_bs, idx_usd) en índices 0-based; cualquiera de los dos puede ser None si esa
    hoja no tiene esa columna (ej. Liquidaciones no maneja montos)."""
    idx_bs, idx_usd = None, None
    for idx, h in enumerate(encabezados):
        if "monto" not in h:
            continue
        if "usd" in h or "dolar" in h:
            if idx_usd is None:
                idx_usd = idx
        elif idx_bs is None:
            idx_bs = idx
    return idx_bs, idx_usd


def _buscar_en_hoja(cliente, sheet_id, nombre_hoja, etiqueta, cedula_digitos):
    """Devuelve lista de líneas (strings) con las coincidencias en una hoja: fecha, nombre
    y — si la hoja maneja montos — el monto en Bs/USD de ese registro, para ver de un
    vistazo cuánto ha pagado el cliente sin tener que abrir el Sheet."""
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
        idx_bs, idx_usd = _buscar_columnas_monto(encabezados)
        for fila in valores[1:]:
            if len(fila) > col_ced and _solo_digitos(fila[col_ced]) == cedula_digitos and cedula_digitos:
                fecha = fila[0] if len(fila) > 0 else ""
                nombre = fila[1] if len(fila) > 1 else ""
                montos = []
                if idx_bs is not None and len(fila) > idx_bs and fila[idx_bs].strip():
                    montos.append(fila[idx_bs].strip())
                if idx_usd is not None and len(fila) > idx_usd and fila[idx_usd].strip():
                    montos.append(fila[idx_usd].strip())
                texto_monto = f" — {' / '.join(montos)}" if montos else ""
                resultados.append(f"   • {fecha} — {nombre}{texto_monto}")
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


# ============ COMANDO "/deshacer" (revertir la ÚLTIMA aprobación) ============
# Ver motor_formularios.py para la lógica compartida (_ejecutar_deshacer, etc.) — este
# comando solo arma la confirmación (con botones) y la muestra, para que nadie deshaga algo
# sin querer con un solo clic. La confirmación se manda como mensaje efímero (solo la ve quien
# corrió el comando) y se actualiza con 'respond' (vía 'response_url'), que es la forma
# correcta de editar un mensaje efímero — a diferencia de los mensajes normales del canal,
# 'chat_update' no funciona con estos.
@app.command("/deshacer")
def deshacer_comando(ack, body, client):
    ack()
    canal = body["channel_id"]
    usuario = body["user_id"]
    entry, minutos = _ultima_aprobacion_deshacible()
    if entry is None:
        if _ULTIMAS_APROBACIONES:
            client.chat_postEphemeral(
                channel=canal, user=usuario,
                text=(f"⚠️ La última aprobación ya pasó de los {_DESHACER_VENTANA_MINUTOS} minutos — "
                      "ya no se puede deshacer automáticamente. Corrígelo directamente en el Sheet si hace falta."))
        else:
            client.chat_postEphemeral(channel=canal, user=usuario,
                text="No hay ninguna aprobación reciente para deshacer.")
        return
    client.chat_postEphemeral(
        channel=canal, user=usuario,
        text="¿Deshacer la última aprobación?",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"↩️ *¿Deshacer la última aprobación?*\n\n"
                f"*{entry.get('resumen', '')}* — aprobado hace {minutos} min por <@{entry['aprobado_por']}>.\n\n"
                "Esto anula el registro en el Sheet (sin borrar la fila — solo se marca como "
                "anulado) y deja el mensaje pendiente de nuevo, con sus botones de Aprobar/"
                "Rechazar/Editar de vuelta.")}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "↩️ Sí, deshacer"}, "style": "danger",
                 "action_id": "confirmar_deshacer", "value": json.dumps({"registro_id": entry.get("registro_id", "")})},
                {"type": "button", "text": {"type": "plain_text", "text": "Cancelar"}, "action_id": "cancelar_deshacer"},
            ]}
        ]
    )


@app.action("confirmar_deshacer")
def confirmar_deshacer(ack, body, client, respond):
    ack()
    usuario = body["user"]["id"]
    try:
        registro_id_esperado = json.loads(body["actions"][0].get("value") or "{}").get("registro_id") or None
    except Exception:
        registro_id_esperado = None
    resultado_texto = _ejecutar_deshacer(client, usuario, registro_id_esperado)
    respond(text=resultado_texto, replace_original=True)


@app.action("cancelar_deshacer")
def cancelar_deshacer(ack, body, client, respond):
    ack()
    respond(text="Cancelado — no se deshizo nada.", replace_original=True)
# ============ FIN COMANDO "/deshacer" ============


# ============ TASA DEL DÍA (una vez al día) — BLINDADO ============
# La tasa vigente y su historial viven SOLO en la pestaña "Historial Tasas" (columnas por
# nombre: Fecha, Tasa). La pestaña "Indicadores" ya NO se toca — se dejó libre para el
# tablero de rendimiento. _abrir_indicadores()/FILA_TASA quedan sin usar por si se necesitan
# más adelante, pero ninguna función de la tasa los llama ya.
FILA_TASA = 20
TASA_MIN = 1            # una tasa por debajo de 1 Bs/USD es imposible
TASA_MAX = 100_000_000  # tope de seguridad para atrapar tipeos absurdos
TASA_CAMBIO_ALERTA = 0.5  # avisa si la nueva tasa cambia más de 50% vs la anterior

# Copia en memoria de la tasa de HOY, para que /cobro no tenga que esperar a Google Sheets
# en cada envío del formulario (la tasa casi nunca cambia de un minuto a otro). Se refresca
# sola cada TASA_CACHE_SEGUNDOS, y también al instante en cuanto alguien fija una tasa nueva
# con /tasa-hoy (ver _guardar_tasa_dia) — nunca se le entrega a alguien un valor "viejo" a
# propósito, solo se evita consultar el Sheet de más.
TASA_CACHE_SEGUNDOS = 120
_CACHE_TASA_HOY = {"valor": None, "fecha": None, "expira": None}


def _abrir_indicadores():
    """Abre la hoja 'Indicadores'. Devuelve None si hay cualquier problema (nunca lanza error).
    Ya no la usa ninguna función de la tasa del día (ver comentario arriba)."""
    try:
        cliente = get_cliente_busqueda()
        spreadsheet = cliente.open_by_key(os.environ["SHEET_ID"])
        pestanas = spreadsheet.worksheets()
        objetivo = _normalizar_encabezado(PESTANA_INDICADORES)
        for ws in pestanas:
            if _normalizar_encabezado(ws.title) == objetivo:
                return ws
        # No la encontró ni siquiera ignorando mayúsculas/tildes/espacios de más — se imprime
        # repr() de cada nombre real para poder detectar caracteres invisibles (ej. un espacio
        # de no separación) que no se ven en Google Sheets pero sí rompen la comparación.
        print(f"⚠️ Tasa: no se encontró la hoja '{PESTANA_INDICADORES}'. "
              f"Pestañas encontradas en el Sheet: {[repr(ws.title) for ws in pestanas]}")
        return None
    except Exception as e:
        print(f"⚠️ Tasa: error abriendo 'Indicadores': {type(e).__name__}: {e}")
        return None


def _guardar_tasa_dia(valor_num):
    """Guarda la tasa de HOY en la pestaña 'Historial Tasas' (una fila por fecha). Ya NO se
    guarda en 'Indicadores' — esa pestaña se dejó solo para el tablero de rendimiento, sin
    mezclar ahí los datos de la tasa. Devuelve True/False."""
    ahora = datetime.now(ZoneInfo("America/Caracas"))
    hoy_txt = ahora.strftime("%d/%m/%Y")
    ok = _guardar_en_historial_tasas(hoy_txt, valor_num)
    if ok:
        # Actualiza la copia en memoria AL INSTANTE — así el próximo /cobro ya ve la tasa
        # recién puesta, sin tener que esperar a que se venza el caché.
        _CACHE_TASA_HOY["valor"] = str(valor_num)
        _CACHE_TASA_HOY["fecha"] = hoy_txt
        _CACHE_TASA_HOY["expira"] = ahora + timedelta(seconds=TASA_CACHE_SEGUNDOS)
    return ok


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
    Si es de hoy, usa la tasa de hoy (Historial Tasas). Si es de otro día, también la
    busca en el historial. Devuelve None si no hay ninguna tasa válida para esa fecha."""
    if fecha_pago == hoy_txt:
        return _tasa_de_hoy()
    return _tasa_de_fecha(fecha_pago)


def _leer_tasa_dia():
    """Devuelve (valor_str, fecha_str) de la tasa de HOY. Primero mira la copia en memoria
    (ver TASA_CACHE_SEGUNDOS) — si está fresca y es de hoy, la devuelve sin tocar el Sheet.
    Si no, consulta 'Historial Tasas' de verdad y refresca la copia. Si hoy todavía no tiene
    tasa registrada, devuelve (None, None). Nunca lanza error."""
    ahora = datetime.now(ZoneInfo("America/Caracas"))
    hoy_txt = ahora.strftime("%d/%m/%Y")
    cache = _CACHE_TASA_HOY
    if cache["fecha"] == hoy_txt and cache["expira"] is not None and ahora < cache["expira"]:
        return (cache["valor"], hoy_txt) if cache["valor"] else (None, None)

    ws = _abrir_historial_tasas()
    if ws is None:
        return None, None
    try:
        col_fecha, col_tasa = _buscar_columnas_historial_tasas(ws)
        if col_fecha is None or col_tasa is None:
            return None, None
        valores = ws.get_all_values()
        valor_encontrado = None
        for fila in valores[1:]:
            if len(fila) > col_fecha and str(fila[col_fecha]).strip() == hoy_txt:
                valor_encontrado = fila[col_tasa] if len(fila) > col_tasa else None
                break
        valor_final = valor_encontrado or None
        _CACHE_TASA_HOY["valor"] = valor_final
        _CACHE_TASA_HOY["fecha"] = hoy_txt
        _CACHE_TASA_HOY["expira"] = ahora + timedelta(seconds=TASA_CACHE_SEGUNDOS)
        return (valor_final, hoy_txt) if valor_final else (None, None)
    except Exception as e:
        print(f"⚠️ Tasa: error leyendo de Historial Tasas: {type(e).__name__}: {e}")
        return None, None


def _ultima_tasa_registrada():
    """Para el aviso de 'cambio grande' de /tasa-hoy: devuelve el valor de la ÚLTIMA fila del
    historial (normalmente la de ayer), sin importar si hoy ya tiene tasa o no. Solo se usa
    para comparar y avisar si el nuevo valor cambia demasiado (posible error de tipeo)."""
    ws = _abrir_historial_tasas()
    if ws is None:
        return None
    try:
        col_fecha, col_tasa = _buscar_columnas_historial_tasas(ws)
        if col_fecha is None or col_tasa is None:
            return None
        filas = ws.get_all_values()[1:]
        if not filas:
            return None
        ultima = filas[-1]
        if len(ultima) <= col_tasa or not ultima[col_tasa]:
            return None
        return ultima[col_tasa]
    except Exception:
        return None


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
        if not valor_ant:
            valor_ant = _ultima_tasa_registrada()  # si hoy no tenía tasa aún, compara contra la de ayer
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
            text=(f"❌ No pude guardar la tasa (revisa que exista la pestaña "
                  f"'{PESTANA_HISTORIAL_TASAS}' con columnas 'Fecha' y 'Tasa'). Intenta de nuevo."))
        return

    try:
        client.chat_postMessage(channel="#cobranzas-log",
            text=f"💱 <@{usuario}> fijó la *tasa del día*: Bs. {valor_num:,.4f} por USD ({hoy_txt}). Ya pueden reportar cobros.")
    except Exception as e:
        print(f"⚠️ No se pudo avisar la tasa en el canal: {e}")
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"✅ Tasa del día fijada: Bs. {valor_num:,.4f} por USD.{aviso_cambio}")
# ============ FIN TASA DEL DÍA ============
