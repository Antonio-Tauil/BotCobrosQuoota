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
    _normalizar_para_comparar, _columna_por_nombre,
)

FORM_SPECS = {}


# ============ MÉTRICAS DE ACTIVIDAD DEL DÍA (para el resumen del Cierre Diario) ============
# Cuenta, en memoria, cuántos formularios se ENVÍAN, APRUEBAN y RECHAZAN por comando cada
# día — para poder mostrarle a gerencia un resumen de actividad (cuánto se registró vs.
# cuánto quedó pendiente de revisar) sin tener que llevar la cuenta a mano. Se resetea solo
# al cambiar de fecha (no hace falta borrar nada) y, si el bot se reinicia a mitad del día
# (ej. un redeploy en Railway), el conteo de ese día vuelve a cero — es una limitación
# conocida y aceptable para un contador de "actividad de hoy", no un registro contable
# (para eso ya están las hojas de Google Sheets, que sí son la fuente de verdad del dinero).
_METRICAS = {}

# Nombres más amigables para comandos que no tienen "titulo_mensaje" en su ficha (ej. /cobro,
# que es un formulario híbrido — ver cobros.py). Para el resto se usa spec["titulo_mensaje"].
_ETIQUETAS_METRICAS = {"cobro": "Cobro"}


def _registrar_metrica(nombre_spec, tipo):
    """Suma 1 al contador de 'tipo' ('enviado'/'aprobado'/'rechazado') de 'nombre_spec' para
    el día de hoy (hora Venezuela). Nunca lanza error: si algo falla, simplemente no cuenta
    esa vez — no debe interrumpir el flujo real de aprobar/rechazar/publicar."""
    try:
        hoy = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
        dia = _METRICAS.setdefault(hoy, {})
        contador = dia.setdefault(nombre_spec, {"enviado": 0, "aprobado": 0, "rechazado": 0})
        contador[tipo] = contador.get(tipo, 0) + 1
    except Exception as e:
        print(f"⚠️ No se pudo registrar la métrica ({nombre_spec}/{tipo}): {e}")


def _resumen_metricas_hoy():
    """Arma el texto del resumen de actividad de HOY (enviados/aprobados/rechazados/
    pendientes, total y por comando) para agregar al Cierre Diario. Devuelve None si no hubo
    ninguna actividad registrada hoy (ej. el bot se reinició después de la última actividad,
    o de verdad no se usó ningún formulario)."""
    hoy = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    dia = _METRICAS.get(hoy, {})
    if not dia:
        return None
    total_enviados = sum(c.get("enviado", 0) for c in dia.values())
    total_aprobados = sum(c.get("aprobado", 0) for c in dia.values())
    total_rechazados = sum(c.get("rechazado", 0) for c in dia.values())
    if total_enviados == 0 and total_aprobados == 0 and total_rechazados == 0:
        return None
    total_pendientes = max(0, total_enviados - total_aprobados - total_rechazados)
    lineas = [
        "📈 *Actividad de hoy:* "
        f"{total_enviados} enviado(s) · {total_aprobados} aprobado(s) · "
        f"{total_rechazados} rechazado(s) · {total_pendientes} pendiente(s) por revisar"
    ]
    comandos_activos = sorted(
        ((nombre, c) for nombre, c in dia.items() if c.get("enviado", 0) > 0),
        key=lambda kv: kv[1]["enviado"], reverse=True,
    )
    for nombre_spec, c in comandos_activos:
        etiqueta = _ETIQUETAS_METRICAS.get(
            nombre_spec, FORM_SPECS.get(nombre_spec, {}).get("titulo_mensaje", nombre_spec))
        pendientes = max(0, c["enviado"] - c.get("aprobado", 0) - c.get("rechazado", 0))
        lineas.append(
            f"   • {etiqueta}: {c['enviado']} enviado(s), {c.get('aprobado', 0)} aprobado(s), "
            f"{c.get('rechazado', 0)} rechazado(s), {pendientes} pendiente(s)"
        )
    return "\n".join(lineas)
# ============ FIN MÉTRICAS DE ACTIVIDAD DEL DÍA ============


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
    # Si la ficha trae "construir_texto" (una función propia), se usa esa en vez del formato
    # genérico de abajo — así cada comando se puede rediseñar UNO POR UNO (Fase: mensajes más
    # amigables) sin tocar el formato de los demás comandos que todavía no se han rediseñado.
    if spec.get("construir_texto"):
        return spec["construir_texto"](datos_campos, fecha, usuario_slack)
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
        _registrar_metrica(nombre_spec, "enviado")
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
    _registrar_metrica(nombre_spec, "aprobado")
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
    _registrar_metrica(nombre_spec, "rechazado")
    fecha_revision = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=f"{spec['titulo_mensaje']} RECHAZADO",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                 "text": f"❌ *RECHAZADO* por <@{body['user']['id']}> el {fecha_revision}\n\n{texto_original}"}}]
    )
# ============ FIN MOTOR GENÉRICO DE FORMULARIOS ============


# ============ BOTÓN "Ver historial del cliente" (genérico, para cualquier ficha) ============
# Antes esto solo existía en /cobro (con _historial_reciente_cliente y ver_historial_cobro
# escritos a mano). Para no repetir prácticamente el mismo código otras 4 veces (/domiciliar,
# /conciliar, /cobro-callcenter, /cobro-comercial), se generalizó en dos piezas:
#   1. _historial_reciente_generico(): busca las últimas coincidencias en la hoja (reutiliza
#      el mismo criterio de comparación que "verificar_duplicado" — por cédula o por texto).
#   2. _construir_handler_historial(): arma el handler del botón para una ficha dada, usando
#      la info que la ficha YA tiene en "verificar_duplicado" (campo, columna, columna_fecha,
#      modo) — así cada comando solo necesita indicar qué columnas de monto/detalle mostrar.

def _historial_reciente_generico(sheet, columna_busqueda, valor_busqueda, modo, columna_fecha,
                                  columnas_valores, maximo=3):
    """Devuelve los últimos 'maximo' registros de 'sheet' que coincidan con 'valor_busqueda'
    en 'columna_busqueda' (mismo criterio de comparación de _buscar_duplicado_reciente: por
    cédula si modo="cedula", por texto normalizado si modo="texto"), como una lista de tuplas
    (fecha, resumen). 'columnas_valores' es la lista de columnas a mostrar junto a la fecha
    (ej. montos). Nunca lanza error: si algo falla, simplemente no hay historial que mostrar."""
    if not valor_busqueda or sheet is None:
        return []
    try:
        col_busqueda = _columna_por_nombre(sheet, columna_busqueda)
        col_fecha = _columna_por_nombre(sheet, columna_fecha)
        if col_busqueda is None or col_fecha is None:
            return []
        idx_busqueda, idx_fecha = col_busqueda - 1, col_fecha - 1
        idx_valores = []
        for nombre_col in columnas_valores:
            col = _columna_por_nombre(sheet, nombre_col)
            idx_valores.append((col - 1) if col else None)
        objetivo = _normalizar_para_comparar(valor_busqueda, modo)
        if not objetivo:
            return []
        coincidencias = []
        for fila in sheet.get_all_values()[1:]:
            if len(fila) <= idx_busqueda:
                continue
            if _normalizar_para_comparar(fila[idx_busqueda], modo) != objetivo:
                continue
            fecha = fila[idx_fecha] if len(fila) > idx_fecha else ""
            valores = []
            for idx in idx_valores:
                if idx is not None and len(fila) > idx and fila[idx].strip():
                    valores.append(fila[idx].strip())
            coincidencias.append((fecha, " / ".join(valores) if valores else "(sin datos)"))
        return coincidencias[-maximo:]
    except Exception as e:
        print(f"⚠️ No se pudo obtener el historial: {e}")
        return []


def _construir_handler_historial(nombre_spec, columnas_valores):
    """Arma el handler del botón 'Ver historial' para la ficha 'nombre_spec' (que debe tener
    'boton_historial' y 'verificar_duplicado' definidos). Se registra en el archivo del
    comando así:
        _handler_historial_x = _construir_handler_historial("x", ["Columna A", "Columna B"])
        @app.action(FORM_SPECS["x"]["boton_historial"])
        def ver_historial_x(ack, body, client):
            _handler_historial_x(ack, body, client)
    """
    def _handler(ack, body, client):
        ack()
        spec = FORM_SPECS[nombre_spec]
        dup_spec = spec["verificar_duplicado"]
        valores_view = body["view"]["state"]["values"]
        valor_input = (_valor_actual_bloque(valores_view, dup_spec["campo"]) or "").strip()
        action_id = spec["boton_historial"]
        if not valor_input:
            texto_extra = f"⚠️ Escribe primero la/el {dup_spec['etiqueta']} arriba, y vuelve a apretar el botón."
        else:
            try:
                sheet = spec["abrir_hoja"]()
            except Exception as e:
                sheet = None
                print(f"⚠️ [{action_id}] No se pudo abrir la hoja: {e}")
            historial = _historial_reciente_generico(
                sheet, dup_spec["columna"], valor_input, dup_spec.get("modo", "cedula"),
                dup_spec["columna_fecha"], columnas_valores
            )
            if historial:
                lineas = "\n".join(f"• {f} — {m}" for f, m in historial)
                texto_extra = f"📜 *Historial reciente de {dup_spec['etiqueta']} {valor_input}:*\n{lineas}"
            else:
                texto_extra = f"📜 No encontré registros anteriores de {dup_spec['etiqueta']} {valor_input}."
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
            print(f"⚠️ [{action_id}] No se pudo actualizar el modal: {e}")
    return _handler
# ============ FIN BOTÓN "Ver historial del cliente" (genérico) ============
