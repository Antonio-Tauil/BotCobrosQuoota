"""
motor_formularios.py — El "Motor Genérico de Formularios" (Fase 3). FORM_SPECS es un
diccionario compartido: cada archivo de comandos (cobros.py, etc.) agrega su propia
"ficha" con FORM_SPECS["nombre"] = {...} al importarse. Estas funciones saben leer
cualquier ficha y armar el modal, validar, guardar y publicar — sin repetir código.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from validaciones import (
    _validar_view, _registro_ya_guardado, _guardar_fila_por_encabezado, _id_amigable,
    _ya_procesado, _reservar_mensaje, _buscar_duplicado_reciente,
)

FORM_SPECS = {}


def _valor_actual_bloque(valores_view, block_id):
    """Lee el valor RAW que tiene AHORA MISMO un campo del modal (tal como está en Slack en
    este instante) sin pasar por 'calcular' de la ficha. Se usa para repoblar un modal con lo
    que la persona ya había escrito, cuando el modal se reconstruye (ej. al apretar un botón
    dentro del formulario, como 'Ver historial'). Para un select devuelve el dict completo
    {"text": {...}, "value": ...} (listo para usar como 'initial_option'); para texto,
    el string tal cual (listo para 'initial_value'). Devuelve None si el campo está vacío."""
    if not valores_view:
        return None
    try:
        estado = valores_view[block_id]["valor"]
    except (KeyError, TypeError):
        return None
    if "selected_option" in estado:
        return estado.get("selected_option")
    return estado.get("value") or None


def _construir_blocks_formulario(spec, valores_view=None, texto_extra=None):
    """Arma los blocks del modal a partir de la ficha. 'valores_view' es opcional: si se pasa
    (el 'state.values' actual del modal), cada campo se repuebla con lo que la persona ya había
    escrito/seleccionado — necesario cuando el modal se reconstruye con views_update (ej. al
    apretar 'Ver historial' dentro de /cobro), para no borrarle lo que ya llevaba llenado.
    'texto_extra' es opcional: si se pasa, se agrega como un bloque de texto al final (ej. el
    resultado del historial del cliente)."""
    blocks = []
    for campo in spec["campos"]:
        elemento = {"action_id": "valor"}
        valor_actual = _valor_actual_bloque(valores_view, campo["id"])
        if campo["tipo"] == "select":
            elemento["type"] = "static_select"
            elemento["placeholder"] = {"type": "plain_text", "text": "Selecciona"}
            opciones = campo["opciones"]
            elemento["options"] = opciones() if callable(opciones) else opciones
            if valor_actual:
                elemento["initial_option"] = valor_actual
        else:
            elemento["type"] = "plain_text_input"
            if campo.get("multiline"):
                elemento["multiline"] = True
            if valor_actual:
                elemento["initial_value"] = valor_actual
        blocks.append({
            "type": "input", "block_id": campo["id"],
            "optional": campo.get("opcional", False),
            "label": {"type": "plain_text", "text": campo["label"]},
            "element": elemento,
        })
    if spec.get("boton_historial"):
        blocks.append({
            "type": "actions", "block_id": "acciones_historial",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "🔍 Ver historial del cliente"},
                "action_id": spec["boton_historial"],
            }],
        })
    if texto_extra:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_extra}})
    return blocks


def _abrir_formulario_generico(nombre_spec, trigger_id, client):
    spec = FORM_SPECS[nombre_spec]
    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": spec["callback_id"],
            "title": {"type": "plain_text", "text": spec["titulo"]},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "blocks": _construir_blocks_formulario(spec),
        }
    )


def _validar_formulario_generico(nombre_spec, valores_view):
    spec = FORM_SPECS[nombre_spec]
    specs_validacion = [(c["id"], c["validar"]) for c in spec["campos"] if c.get("validar")]
    return _validar_view(valores_view, specs_validacion)


def _extraer_valores_formulario(spec, valores_view):
    datos = {}
    for campo in spec["campos"]:
        bid = campo["id"]
        try:
            estado = valores_view[bid]["valor"]
        except (KeyError, TypeError):
            datos[bid] = ""
            continue
        if campo["tipo"] == "select":
            datos[bid] = (estado.get("selected_option") or {}).get("value", "")
        else:
            datos[bid] = estado.get("value") or ""
    if spec.get("calcular"):
        # Algunos comandos (montos en Bs/USD, tasas) necesitan calcular/formatear campos
        # antes de guardar y de publicar el mensaje — la ficha aporta esa función, y el
        # resultado se calcula UNA sola vez aquí (no se vuelve a calcular al aprobar).
        datos.update(spec["calcular"](datos))
    return datos


def _guardar_generico(nombre_spec, datos_campos, fecha, registro_id=""):
    """Guarda una fila en el Sheet de la ficha 'nombre_spec', por nombre de columna.
    Si la ficha pide anti-duplicado, revisa primero que ese registro_id no exista ya.
    Devuelve 'OK', 'DUPLICADO' o 'ERROR' (igual que las funciones guardar_en_* de antes)."""
    spec = FORM_SPECS[nombre_spec]
    sheet = spec["abrir_hoja"]()
    if sheet is None:
        print(f"❌ [{nombre_spec}] No se pudo guardar: no se encontró la hoja de destino.")
        return "ERROR"
    if spec.get("anti_duplicado") and _registro_ya_guardado(sheet, registro_id):
        print(f"⚠️ [{nombre_spec}] duplicado (ya guardado), se omite.")
        return "DUPLICADO"
    datos_sheet = {}
    if spec.get("agregar_fecha"):
        # Acepta un solo nombre de columna o una lista (algunos comandos, como
        # Liquidaciones, ponen la fecha de hoy en más de una columna a la vez).
        columnas_fecha = spec["agregar_fecha"]
        if isinstance(columnas_fecha, str):
            columnas_fecha = [columnas_fecha]
        for columna_fecha in columnas_fecha:
            datos_sheet[columna_fecha] = fecha
    for block_id, columna in spec["columnas"].items():
        datos_sheet[columna] = datos_campos.get(block_id, "")
    if spec.get("columna_id_registro"):
        datos_sheet[spec["columna_id_registro"]] = registro_id
    _guardar_fila_por_encabezado(sheet, datos_sheet)
    print(f"✅ [{nombre_spec}] guardado en hoja '{sheet.title}'")
    return "OK"


def _construir_texto_mensaje(spec, datos_campos, fecha, usuario_slack):
    lineas = [
        f"*{spec['titulo_mensaje']}* {spec.get('emoji_mensaje', '')}",
        f"*Fecha:* {fecha}",
        f"*Reportado por:* <@{usuario_slack}>",
    ]
    for etiqueta, block_id in spec["campos_mensaje"]:
        lineas.append(f"*{etiqueta}:* {datos_campos.get(block_id, '')}")
    return "\n".join(lineas)


def _ejecutar_formulario_generico(nombre_spec, body, client):
    """Para comandos SIN aprobación: guarda de una vez y publica en el canal. Se llama
    DESPUÉS de ack() — igual que hacía cada comando antes de esta migración."""
    spec = FORM_SPECS[nombre_spec]
    valores_view = body["view"]["state"]["values"]
    datos_campos = _extraer_valores_formulario(spec, valores_view)
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

    _guardar_generico(nombre_spec, datos_campos, fecha)

    if spec.get("canal"):
        usuario_slack = body["user"]["id"]
        texto = _construir_texto_mensaje(spec, datos_campos, fecha, usuario_slack)
        client.chat_postMessage(
            channel=spec["canal"], text=spec["titulo_mensaje"],
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": texto}}]
        )


def _publicar_para_aprobacion(nombre_spec, body, client):
    """Para comandos CON aprobación: no guarda todavía — publica el reporte en el canal
    con botones Aprobar/Rechazar, y guarda los datos en los 'metadata' del mensaje para
    poder leerlos de nuevo cuando alguien apruebe. Se llama DESPUÉS de ack()."""
    spec = FORM_SPECS[nombre_spec]
    valores_view = body["view"]["state"]["values"]
    datos_campos = _extraer_valores_formulario(spec, valores_view)
    fecha = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    usuario_slack = body["user"]["id"]
    texto = _construir_texto_mensaje(spec, datos_campos, fecha, usuario_slack)
    metadata_payload = dict(datos_campos)
    metadata_payload["_fecha"] = fecha
    # El sufijo del action_id de los botones puede ser distinto al nombre de la ficha
    # (algunos comandos ya tenían un action_id propio en Slack antes de esta migración,
    # p.ej. "domiciliar" usa botones "aprobar_domiciliacion"). "accion_id" en la ficha
    # permite fijar ese sufijo; si no está, se usa el nombre de la ficha tal cual.
    sufijo_accion = spec.get("accion_id", nombre_spec)

    # ============ AVISO DE POSIBLE DUPLICADO (mismo cliente, misma semana) ============
    # Si la ficha pide "verificar_duplicado", revisa si ya hay un registro reciente del
    # mismo cliente (por cédula, o por empresa en /domiciliar) ANTES de publicar. Si lo hay,
    # se agrega un aviso al mensaje y el botón Aprobar pide una confirmación extra (ventana
    # emergente nativa de Slack) — no bloquea el guardado, solo obliga a confirmar a
    # propósito. Importante: el aviso usa el emoji 🔁 (no ✅/❌/⚠), porque _ya_procesado()
    # usa esos tres para saber si un mensaje ya fue aprobado/rechazado — si el aviso
    # empezara con ⚠, el mensaje se vería como "ya procesado" y nadie podría aprobarlo.
    confirmar_aprobar = None
    dup_spec = spec.get("verificar_duplicado")
    if dup_spec:
        valor_identificador = datos_campos.get(dup_spec["campo"], "")
        try:
            sheet_dup = spec["abrir_hoja"]()
        except Exception as e:
            sheet_dup = None
            print(f"⚠️ [{nombre_spec}] No se pudo abrir la hoja para revisar duplicado: {e}")
        fecha_duplicado = _buscar_duplicado_reciente(
            sheet_dup, dup_spec["columna"], dup_spec["columna_fecha"],
            valor_identificador, dup_spec.get("modo", "cedula")
        )
        if fecha_duplicado:
            texto = (f"🔁 *POSIBLE DUPLICADO* — ya hay un registro con esta/e {dup_spec['etiqueta']} "
                     f"esta semana (fecha: {fecha_duplicado}).\n\n" + texto)
            confirmar_aprobar = {
                "title": {"type": "plain_text", "text": "Confirmar posible duplicado"},
                "text": {"type": "mrkdwn", "text": (
                    f"Ya hay otro registro con esta/e {dup_spec['etiqueta']} esta semana "
                    f"(fecha: {fecha_duplicado}). ¿Aprobar de todas formas?")},
                "confirm": {"type": "plain_text", "text": "Sí, aprobar de todas formas"},
                "deny": {"type": "plain_text", "text": "Cancelar"},
            }
    # ============ FIN AVISO DE POSIBLE DUPLICADO ============

    boton_aprobar = {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary",
                     "action_id": f"aprobar_{sufijo_accion}"}
    if confirmar_aprobar:
        boton_aprobar["confirm"] = confirmar_aprobar
    try:
        client.chat_postMessage(
            channel=spec["canal"], text=spec["titulo_mensaje"],
            metadata={"event_type": f"{nombre_spec}_reportado", "event_payload": metadata_payload},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto}},
                {"type": "actions", "elements": [
                    boton_aprobar,
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger",
                     "action_id": f"rechazar_{sufijo_accion}"},
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ [{nombre_spec}] No se pudo enviar mensaje al canal: {e}")


def _aprobar_generico(nombre_spec, body, client):
    """Handler compartido para el botón '✅ Aprobar' de cualquier comando con aprobación.
    Se llama DESPUÉS de ack()."""
    spec = FORM_SPECS[nombre_spec]
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    meta = dict(body["message"].get("metadata", {}).get("event_payload", {}))
    fecha_original = meta.pop("_fecha", fecha_revision)
    registro_id = ""
    if spec.get("anti_duplicado"):
        registro_id = _id_amigable(spec.get("prefijo_id", nombre_spec.upper()), body["message"]["ts"])
    resultado = _guardar_generico(nombre_spec, meta, fecha_original, registro_id)
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    elif resultado == "OK":
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
    else:
        encabezado = f"⚠️ *APROBADO pero hubo error guardando (revisar logs)* por <@{body['user']['id']}> el {fecha_revision}"
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=f"{spec['titulo_mensaje']} procesado",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"{encabezado}\n\n{texto_original}"}}]
    )


def _rechazar_generico(nombre_spec, body, client):
    """Handler compartido para el botón '❌ Rechazar' de cualquier comando con aprobación.
    Se llama DESPUÉS de ack()."""
    spec = FORM_SPECS[nombre_spec]
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return
    if not _reservar_mensaje(body["message"]["ts"]):
        return  # alguien más ya está procesando este mismo clic (doble clic o dos personas a la vez)
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=f"{spec['titulo_mensaje']} RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN MOTOR GENÉRICO DE FORMULARIOS ============
