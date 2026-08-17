"""
motor_formularios.py — El "Motor Genérico de Formularios" (Fase 3). FORM_SPECS es un
diccionario compartido: cada archivo de comandos (cobros.py, etc.) agrega su propia
"ficha" con FORM_SPECS["nombre"] = {...} al importarse. Estas funciones saben leer
cualquier ficha y armar el modal, validar, guardar y publicar — sin repetir código.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from config import app
from validaciones import (
    _validar_view, _registro_ya_guardado, _guardar_fila_por_encabezado, _id_amigable,
    _ya_procesado, _reservar_mensaje, _buscar_duplicado_reciente,
    _normalizar_para_comparar, _columna_por_nombre, _solo_digitos, parse_numero,
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


# ============ VIGILANTE DE REPORTES COLGADOS ============
# Los 4 reportes automáticos (Radar 4PM, Cierre 6PM, Semanal lunes, Mensual día 1) ya
# avisan por DM al supervisor si terminan con un ERROR (_avisar_falla_reporte). Pero si
# uno se queda "colgado" (ej. Google Sheets nunca responde y el hilo se queda esperando
# para siempre), no hay excepción que atrapar — el reporte simplemente nunca llega y nadie
# se entera hasta que alguien pregunta "¿y el cierre de hoy?". Este vigilante lleva un
# reloj en memoria de cuándo empezó y cuándo terminó cada reporte; un job aparte (en
# scheduler.py) lo revisa cada cierto tiempo y avisa UNA SOLA VEZ si detecta que uno lleva
# corriendo más de la cuenta. No puede "arreglar" el reporte colgado (Python no puede matar
# de forma segura un hilo esperando por red) — es solo una alarma para que alguien revise
# Railway y, si hace falta, reinicie el bot.
_VIGILANCIA_REPORTES = {}


def _marcar_inicio_reporte(nombre_reporte):
    """Se llama al ARRANCAR un reporte automático. Nunca lanza error."""
    try:
        _VIGILANCIA_REPORTES[nombre_reporte] = {
            "inicio": datetime.now(ZoneInfo("America/Caracas")),
            "fin": None,
            "alertado": False,
        }
    except Exception as e:
        print(f"⚠️ No se pudo marcar el inicio de '{nombre_reporte}': {e}")


def _marcar_fin_reporte(nombre_reporte):
    """Se llama al TERMINAR un reporte automático (con éxito o con un error ya atrapado).
    Si no se llega a llamar esto (el reporte se quedó colgado), el vigilante lo detecta."""
    try:
        estado = _VIGILANCIA_REPORTES.get(nombre_reporte)
        if estado is not None:
            estado["fin"] = datetime.now(ZoneInfo("America/Caracas"))
            estado["alertado"] = False  # ya terminó bien: si vuelve a correr, puede alertar de nuevo
    except Exception as e:
        print(f"⚠️ No se pudo marcar el fin de '{nombre_reporte}': {e}")


def _reportes_colgados(minutos_umbral=10):
    """Devuelve la lista de nombres de reportes que empezaron y llevan más de
    'minutos_umbral' minutos sin terminar, y que todavía no se les avisó al supervisor
    (para no mandar el mismo aviso una y otra vez mientras sigue colgado). Marca esos
    reportes como 'ya alertados' antes de devolverlos."""
    colgados = []
    try:
        ahora = datetime.now(ZoneInfo("America/Caracas"))
        for nombre_reporte, estado in _VIGILANCIA_REPORTES.items():
            if estado.get("fin") is not None:
                continue  # ya terminó
            if estado.get("alertado"):
                continue  # ya se avisó de este episodio colgado, no repetir
            inicio = estado.get("inicio")
            if inicio is None:
                continue
            minutos = (ahora - inicio).total_seconds() / 60
            if minutos >= minutos_umbral:
                estado["alertado"] = True
                colgados.append((nombre_reporte, round(minutos)))
    except Exception as e:
        print(f"⚠️ El vigilante de reportes tuvo un problema revisando: {e}")
    return colgados
# ============ FIN VIGILANTE DE REPORTES COLGADOS ============


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
                "text": {"type": "plain_text",
                         "text": spec.get("boton_historial_label", "🔍 Ver historial del cliente")},
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
    # Guardamos quién lo reportó ORIGINALMENTE (no solo aparece en el texto del mensaje):
    # así, si alguien usa el botón "✏️ Editar" para corregir un dato antes de aprobar, el
    # mensaje corregido sigue diciendo "Reportado por" la persona correcta, no quien editó.
    metadata_payload["_reportado_por"] = usuario_slack
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

    # ============ AVISO DE POSIBLE ERROR EN LA TASA BCV (dato tecleado a mano) ============
    # Algunos comandos (domiciliar, cobro_callcenter, cobro_comercial) piden la Tasa BCV
    # escrita a mano, en vez de tomarla de /tasa-hoy como hace /cobro — así que un typo (un
    # cero de más, una coma corrida) no lo agarra ninguna validación de formato, y el Monto en
    # USD calculado con esa tasa queda con un valor absurdo (ej. $0.03 en vez de $26) sin que
    # nadie se dé cuenta hasta revisar el Sheet a mano. Si la ficha pide "verificar_tasa", se
    # compara lo escrito contra la tasa oficial (la que devuelva 'obtener_oficial', normalmente
    # la de /tasa-hoy) y, si se aleja demasiado (más del umbral), se avisa y se pide confirmar
    # antes de aprobar — igual que el aviso de posible duplicado. Usa el emoji 💱 (no
    # ✅/❌/⚠), por la misma razón que el aviso de duplicado usa 🔁.
    tasa_spec = spec.get("verificar_tasa")
    if tasa_spec:
        try:
            tasa_escrita = parse_numero(datos_campos.get(tasa_spec["campo_tasa"], ""))
        except (ValueError, TypeError):
            tasa_escrita = None
        try:
            tasa_oficial = tasa_spec["obtener_oficial"](datos_campos)
            tasa_oficial = float(tasa_oficial) if tasa_oficial else None
        except Exception as e:
            tasa_oficial = None
            print(f"⚠️ [{nombre_spec}] No se pudo comparar contra la tasa oficial: {e}")
        if tasa_escrita and tasa_oficial:
            cambio = abs(tasa_escrita - tasa_oficial) / tasa_oficial
            umbral = tasa_spec.get("umbral", 0.5)
            if cambio > umbral:
                texto = (f"💱 *POSIBLE ERROR EN LA TASA BCV* — escribiste Bs. {tasa_escrita:,.4f} pero la tasa "
                         f"oficial es Bs. {tasa_oficial:,.4f} (una diferencia de {cambio * 100:,.0f}%). Revisa "
                         f"que no se te haya colado un dígito de más o de menos.\n\n" + texto)
                if confirmar_aprobar:
                    confirmar_aprobar["text"]["text"] += ("\n\nAdemás, la tasa BCV escrita parece fuera de "
                                                           "rango — revísala antes de confirmar.")
                else:
                    confirmar_aprobar = {
                        "title": {"type": "plain_text", "text": "Confirmar tasa fuera de rango"},
                        "text": {"type": "mrkdwn", "text": (
                            f"La tasa escrita (Bs. {tasa_escrita:,.4f}) difiere mucho de la oficial "
                            f"(Bs. {tasa_oficial:,.4f}). ¿Aprobar de todas formas?")},
                        "confirm": {"type": "plain_text", "text": "Sí, aprobar de todas formas"},
                        "deny": {"type": "plain_text", "text": "Cancelar"},
                    }
    # ============ FIN AVISO DE POSIBLE ERROR EN LA TASA BCV ============

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
                    {"type": "button", "text": {"type": "plain_text", "text": "✏️ Editar"},
                     "action_id": f"editar_{sufijo_accion}"},
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
    meta.pop("_reportado_por", None)
    registro_id = ""
    if spec.get("anti_duplicado"):
        registro_id = _id_amigable(spec.get("prefijo_id", nombre_spec.upper()), body["message"]["ts"])
    resultado = _guardar_generico(nombre_spec, meta, fecha_original, registro_id)
    if resultado == "DUPLICADO":
        encabezado = f"⚠️ *YA REGISTRADO* — ya estaba guardado, no se duplicó. Revisado por <@{body['user']['id']}> el {fecha_revision}"
    elif resultado == "OK":
        encabezado = f"✅ *APROBADO* por <@{body['user']['id']}> el {fecha_revision}"
        # Solo si de verdad se guardó una fila nueva (y tiene un "ID Registro" con el que
        # encontrarla después) queda algo que "/deshacer" pueda anular.
        if registro_id:
            _blocks_msg = body["message"].get("blocks", [])
            _registrar_aprobacion_para_deshacer({
                "abrir_hoja": spec["abrir_hoja"],
                "columna_id_registro": spec.get("columna_id_registro", "ID Registro"),
                "registro_id": registro_id,
                "canal": body["channel"]["id"],
                "ts": body["message"]["ts"],
                "texto_original": texto_original,
                "blocks_accion_original": _blocks_msg[1] if len(_blocks_msg) > 1
                    else {"type": "actions", "elements": []},
                "aprobado_por": body["user"]["id"],
                "resumen": spec.get("titulo_mensaje", nombre_spec),
            })
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


# ============ BOTÓN "✏️ Editar" ANTES DE APROBAR (genérico, para cualquier ficha) ============
# Cuando el cobrador se equivoca en un dato (un monto, una cédula mal tipeada), antes había que
# RECHAZAR todo el formulario y hacer que la persona lo volviera a llenar desde cero. Con este
# botón, cualquiera que vea el mensaje pendiente puede abrir el mismo formulario ya prellenado
# con lo que se reportó, corregir solo el campo que hacía falta, y guardar — el mensaje se
# actualiza con los datos corregidos y sigue esperando Aprobar/Rechazar, como si nada. No toca
# el Sheet todavía (eso solo pasa al Aprobar) — es una corrección "en el aire", antes de guardar.
def _valores_view_desde_metadata(spec, meta, mapeo=None):
    """Convierte los valores guardados en la 'metadata' del mensaje (un dict simple
    block_id -> valor final) a la forma que espera '_construir_blocks_formulario' para
    repoblar el modal (la misma forma que produce 'state.values' de Slack). Para los
    campos tipo 'select', busca cuál opción de la ficha corresponde al valor guardado
    (para poder marcarla como 'initial_option'); si no la encuentra (ej. la opción ya no
    existe en la lista), simplemente deja ese campo vacío — no rompe nada, la persona solo
    tiene que volver a elegirlo. 'mapeo' es opcional: {block_id: nombre_en_metadata}, para
    comandos donde el nombre guardado en la metadata no coincide con el id del campo del
    modal (ej. /cobro guarda 'cobrador' en la metadata pero el campo del modal se llama
    'nombre_cobrador') — si un campo no aparece en 'mapeo', se busca con su propio id."""
    mapeo = mapeo or {}
    valores_view = {}
    for campo in spec["campos"]:
        bid = campo["id"]
        clave_metadata = mapeo.get(bid, bid)
        valor = meta.get(clave_metadata, "")
        if not valor:
            continue
        if campo["tipo"] == "select":
            opciones = campo["opciones"]
            opciones = opciones() if callable(opciones) else opciones
            opcion = next((o for o in opciones if o.get("value") == valor), None)
            if opcion:
                valores_view[bid] = {"valor": {"selected_option": opcion}}
        else:
            valores_view[bid] = {"valor": {"value": valor}}
    return valores_view


def _editar_generico(nombre_spec, body, client):
    """Handler compartido para el botón '✏️ Editar' de cualquier comando con aprobación.
    Se llama DESPUÉS de ack(). Abre el mismo modal del formulario, prellenado con lo que ya
    se había reportado, para corregir antes de aprobar."""
    spec = FORM_SPECS[nombre_spec]
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if _ya_procesado(texto_original):
        return  # ya fue aprobado/rechazado — no tiene caso editar algo que ya se resolvió
    meta = dict(body["message"].get("metadata", {}).get("event_payload", {}))
    fecha_original = meta.get("_fecha") or datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    reportado_por = meta.get("_reportado_por") or body["user"]["id"]
    valores_view = _valores_view_desde_metadata(spec, meta)
    privado = json.dumps({
        "nombre_spec": nombre_spec,
        "canal": body["channel"]["id"],
        "ts": body["message"]["ts"],
        "fecha": fecha_original,
        "reportado_por": reportado_por,
    })
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "editar_generico_formulario",
                "private_metadata": privado,
                "title": {"type": "plain_text", "text": "Editar antes de aprobar"},
                "submit": {"type": "plain_text", "text": "Guardar cambios"},
                "blocks": _construir_blocks_formulario(spec, valores_view),
            }
        )
    except Exception as e:
        print(f"⚠️ [{nombre_spec}] No se pudo abrir el formulario de edición: {e}")


@app.view("editar_generico_formulario")
def _recibir_edicion_generico(ack, body, client):
    """Un solo handler para TODOS los comandos: sabe qué ficha y qué mensaje corregir por lo
    que se guardó en 'private_metadata' al abrir el modal (ver _editar_generico)."""
    try:
        privado = json.loads(body["view"]["private_metadata"])
    except Exception:
        ack()
        return
    nombre_spec = privado.get("nombre_spec")
    spec = FORM_SPECS.get(nombre_spec)
    if spec is None:
        ack()
        return
    valores_view = body["view"]["state"]["values"]
    errores = _validar_formulario_generico(nombre_spec, valores_view)
    if errores:
        ack(response_action="errors", errors=errores)
        return
    ack()
    try:
        canal = privado["canal"]
        ts = privado["ts"]
        fecha_original = privado.get("fecha") or datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
        reportado_por = privado.get("reportado_por") or body["user"]["id"]
        editor = body["user"]["id"]
        datos_campos = _extraer_valores_formulario(spec, valores_view)
        texto = _construir_texto_mensaje(spec, datos_campos, fecha_original, reportado_por)
        # El aviso de edición usa ✏️ (no ✅/❌/⚠) para no confundir a _ya_procesado(), que
        # se fija en esos tres emojis para saber si un mensaje ya fue aprobado/rechazado.
        texto_con_aviso = f"✏️ *Editado por <@{editor}> antes de aprobar*\n\n{texto}"
        metadata_payload = dict(datos_campos)
        metadata_payload["_fecha"] = fecha_original
        metadata_payload["_reportado_por"] = reportado_por
        sufijo_accion = spec.get("accion_id", nombre_spec)
        boton_aprobar = {"type": "button", "text": {"type": "plain_text", "text": "✅ Aprobar"}, "style": "primary",
                         "action_id": f"aprobar_{sufijo_accion}"}
        client.chat_update(
            channel=canal, ts=ts, text=spec["titulo_mensaje"],
            metadata={"event_type": f"{nombre_spec}_reportado", "event_payload": metadata_payload},
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": texto_con_aviso}},
                {"type": "actions", "elements": [
                    boton_aprobar,
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Rechazar"}, "style": "danger",
                     "action_id": f"rechazar_{sufijo_accion}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "✏️ Editar"},
                     "action_id": f"editar_{sufijo_accion}"},
                ]}
            ]
        )
    except Exception as e:
        print(f"⚠️ [{nombre_spec}] Error guardando la corrección: {e}")
# ============ FIN BOTÓN "✏️ Editar" ANTES DE APROBAR ============


# ============ COMANDO "/deshacer" (revertir la ÚLTIMA aprobación, dentro de una ventana corta) ============
# Objetivo: si alguien aprueba un cobro/domiciliación/etc. por error (o con un dato que nadie
# notó a tiempo), poder revertirlo SIN tener que ir a corregir el Sheet a mano. Por seguridad,
# a propósito NO se puede deshacer cualquier aprobación del historial — solo la MÁS RECIENTE, y
# solo dentro de los primeros minutos (ver _DESHACER_VENTANA_MINUTOS): pasado ese tiempo, es más
# probable que ese registro ya se haya usado en un reporte o conciliación, y deshacerlo en
# silencio generaría más confusión que la que resuelve. El registro en el Sheet NUNCA se borra:
# solo se le antepone "ANULADO-" a su "ID Registro", así el dato completo sigue ahí como
# evidencia de que existió y fue anulado — nada se destruye. El mensaje de Slack vuelve a su
# estado pendiente (con los mismos botones que tenía antes) para poder corregirlo y volver a
# aprobar, o rechazarlo directamente. NO cubre /liquidacion-estatus: ese comando no crea una
# fila nueva, actualiza el estatus de una ya existente — deshacer eso necesitaría guardar el
# estatus ANTERIOR, que es un mecanismo distinto y queda fuera de este alcance por ahora.
_ULTIMAS_APROBACIONES = []  # pila: la más reciente al final (solo se puede deshacer esa)
_DESHACER_VENTANA_MINUTOS = 30
_DESHACER_MAX_GUARDADAS = 20


def _registrar_aprobacion_para_deshacer(entry):
    """Guarda una aprobación reciente en la pila de 'deshacer'. 'entry' debe traer: abrir_hoja
    (callable que abre la hoja donde se guardó), columna_id_registro, registro_id, canal, ts,
    texto_original (el texto de ANTES de que se marcara aprobado, para poder restaurarlo),
    blocks_accion_original (el bloque 'actions' original, con los mismos botones de
    siempre — se reutiliza tal cual, así no hace falta reconstruirlo comando por comando),
    aprobado_por (quién lo aprobó) y resumen (una descripción corta para mostrar en la
    confirmación de /deshacer). Nunca lanza error."""
    try:
        entry = dict(entry)
        entry["momento"] = datetime.now(ZoneInfo("America/Caracas"))
        _ULTIMAS_APROBACIONES.append(entry)
        if len(_ULTIMAS_APROBACIONES) > _DESHACER_MAX_GUARDADAS:
            _ULTIMAS_APROBACIONES.pop(0)
    except Exception as e:
        print(f"⚠️ No se pudo registrar la aprobación para poder deshacerla: {e}")


def _ultima_aprobacion_deshacible():
    """Devuelve (entry, minutos_transcurridos) de la ÚLTIMA aprobación SI todavía está dentro
    de la ventana de tiempo para deshacer, o (None, None) si no hay ninguna o ya se pasó del
    tiempo permitido."""
    if not _ULTIMAS_APROBACIONES:
        return None, None
    entry = _ULTIMAS_APROBACIONES[-1]
    minutos = (datetime.now(ZoneInfo("America/Caracas")) - entry["momento"]).total_seconds() / 60
    if minutos > _DESHACER_VENTANA_MINUTOS:
        return None, None
    return entry, round(minutos)


def _anular_registro_por_id(abrir_hoja, columna_id_registro, registro_id):
    """Busca 'registro_id' en la columna 'columna_id_registro' de la hoja que abre
    'abrir_hoja()', y le antepone 'ANULADO-' — SIN borrar la fila (todo el resto del registro
    queda intacto, visible en el Sheet, como evidencia de que existió y fue anulado). Devuelve
    True si lo encontró y lo marcó, False si no lo encontró o algo falló (nunca lanza error)."""
    try:
        sheet = abrir_hoja()
        if sheet is None or not registro_id:
            return False
        col = _columna_por_nombre(sheet, columna_id_registro)
        if col is None:
            return False
        objetivo = str(registro_id).strip()
        valores = sheet.get_all_values()
        for i, fila in enumerate(valores[1:], start=2):  # fila 1 = encabezados
            if len(fila) >= col and str(fila[col - 1]).strip() == objetivo:
                sheet.update_cell(i, col, f"ANULADO-{objetivo}")
                return True
        return False
    except Exception as e:
        print(f"⚠️ No se pudo anular el registro '{registro_id}': {e}")
        return False


def _ejecutar_deshacer(client, quien_deshace, registro_id_esperado=None):
    """Ejecuta el deshacer de la ÚLTIMA aprobación (si sigue disponible y dentro de la
    ventana de tiempo): anula el registro en el Sheet y regresa el mensaje de Slack a su
    estado pendiente. 'registro_id_esperado' (opcional) protege contra la carrera de que,
    entre que se mostró la confirmación de /deshacer y que alguien la apretó, se haya
    aprobado algo MÁS reciente — si el tope de la pila cambió, no deshace nada a ciegas y
    avisa que hay que volver a correr /deshacer. Devuelve el texto para confirmarle a quien
    corrió /deshacer qué pasó."""
    entry, minutos = _ultima_aprobacion_deshacible()
    if entry is None:
        if _ULTIMAS_APROBACIONES:
            return (f"⚠️ La última aprobación ya pasó de los {_DESHACER_VENTANA_MINUTOS} minutos "
                     "— ya no se puede deshacer automáticamente. Corrígelo directamente en el Sheet si hace falta.")
        return "No hay ninguna aprobación reciente para deshacer."
    if registro_id_esperado and entry.get("registro_id") != registro_id_esperado:
        return ("⚠️ Mientras tanto se aprobó algo más reciente — corre `/deshacer` de nuevo si quieres "
                "deshacer ESA aprobación (ya no la que habías visto antes).")
    _ULTIMAS_APROBACIONES.pop()  # se consume: no se puede deshacer dos veces lo mismo
    anulado = _anular_registro_por_id(entry["abrir_hoja"], entry["columna_id_registro"], entry["registro_id"])
    aviso = (f"↩️ *DESHECHO por <@{quien_deshace}>* — la aprobación de <@{entry['aprobado_por']}> quedó anulada.\n\n"
             f"{entry['texto_original']}")
    try:
        client.chat_update(
            channel=entry["canal"], ts=entry["ts"], text="Pendiente de nuevo (aprobación deshecha)",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": aviso}},
                entry["blocks_accion_original"],
            ]
        )
    except Exception as e:
        print(f"⚠️ [deshacer] No se pudo actualizar el mensaje en Slack: {e}")
    if not anulado:
        return (f"⚠️ Deshice *{entry.get('resumen', '')}* en Slack (el mensaje vuelve a estar pendiente), "
                "pero NO pude anular el registro en el Sheet — revísalo a mano si ya se había guardado.")
    return (f"✅ Deshecho: *{entry.get('resumen', '')}* (estaba aprobado hace {minutos} min "
            f"por <@{entry['aprobado_por']}>). El registro quedó anulado en el Sheet y el mensaje "
            "volvió a estar pendiente.")
# ============ FIN COMANDO "/deshacer" ============


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


def _construir_handler_historial(nombre_spec, columnas_valores, autocompletar=None, mapeo_autocompletar=None,
                                  calcular_score=None):
    """Arma el handler del botón 'Ver historial' para la ficha 'nombre_spec' (que debe tener
    'boton_historial' y 'verificar_duplicado' definidos). Se registra en el archivo del
    comando así:
        _handler_historial_x = _construir_handler_historial("x", ["Columna A", "Columna B"])
        @app.action(FORM_SPECS["x"]["boton_historial"])
        def ver_historial_x(ack, body, client):
            _handler_historial_x(ack, body, client)

    'autocompletar' es opcional: una función(cedula_digitos) -> {"nombre": ..., "telefono": ...}
    o None si no encontró nada. Si se pasa (y la ficha busca por cédula, no por empresa), el
    MISMO botón —además de mostrar el historial— rellena los campos del formulario con los
    datos ya conocidos del cliente, para no escribirlos dos veces. 'mapeo_autocompletar' indica
    a qué campo del formulario va cada dato (por defecto {"nombre": "nombre", "telefono":
    "telefono"} — se pasa uno distinto cuando el campo se llama diferente, ej. /conciliar usa
    "cliente" en vez de "nombre" y no tiene campo de teléfono."""
    def _handler(ack, body, client):
        ack()
        spec = FORM_SPECS[nombre_spec]
        dup_spec = spec["verificar_duplicado"]
        valores_view = dict(body["view"]["state"]["values"])
        valor_input = (_valor_actual_bloque(valores_view, dup_spec["campo"]) or "").strip()
        action_id = spec["boton_historial"]
        if not valor_input:
            texto_extra = f"⚠️ Escribe primero la/el {dup_spec['etiqueta']} arriba, y vuelve a apretar el botón."
        else:
            partes_texto = []
            # ---- Autocompletar nombre/teléfono con lo ya conocido del cliente ----
            if autocompletar and dup_spec.get("modo", "cedula") == "cedula":
                try:
                    datos_cliente = autocompletar(_solo_digitos(valor_input))
                except Exception as e:
                    datos_cliente = None
                    print(f"⚠️ [{action_id}] No se pudo autocompletar los datos del cliente: {e}")
                if datos_cliente:
                    mapeo = mapeo_autocompletar or {"nombre": "nombre", "telefono": "telefono"}
                    campos_ids = {c["id"] for c in spec["campos"]}
                    algo_relleno = False
                    for clave_canonica, campo_id in mapeo.items():
                        valor = datos_cliente.get(clave_canonica)
                        if valor and campo_id in campos_ids:
                            valores_view[campo_id] = {"valor": {"value": valor}}
                            algo_relleno = True
                    if algo_relleno:
                        partes_texto.append("✅ *Cliente encontrado:* se rellenaron los datos "
                                             "conocidos (puedes corregirlos si hace falta).")
            # ---- Historial reciente (como ya funcionaba) ----
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
                partes_texto.append(f"📜 *Historial reciente de {dup_spec['etiqueta']} {valor_input}:*\n{lineas}")
            else:
                partes_texto.append(f"📜 No encontré registros anteriores de {dup_spec['etiqueta']} {valor_input}.")
            # ---- Score de riesgo (opcional, solo cuando se busca por cédula) ----
            if calcular_score and dup_spec.get("modo", "cedula") == "cedula":
                try:
                    score = calcular_score(_solo_digitos(valor_input))
                except Exception as e:
                    score = None
                    print(f"⚠️ [{action_id}] No se pudo calcular el score de riesgo: {e}")
                if score:
                    partes_texto.append(f"📊 *Score de riesgo:* {score}")
            texto_extra = "\n\n".join(partes_texto)
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


# ============ BOTÓN "Buscar cliente" — solo autocompletar, sin historial ============
# Versión más liviana de _construir_handler_historial: para comandos que NO llevan un
# "verificar_duplicado" en su ficha (ej. /contactar, /contacto-legal, /clientes-escalados —
# no son formularios de cobro, así que nunca se les agregó el aviso de duplicado) pero que
# sí se benefician de no reescribir nombre/teléfono si el cliente ya está registrado en otro
# lado. No requiere 'abrir_hoja' ni 'verificar_duplicado' en la ficha — la función
# 'autocompletar' que se le pasa ya sabe dónde buscar.
def _construir_handler_autocompletar(nombre_spec, campo_busqueda, autocompletar, mapeo_autocompletar=None,
                                      calcular_score=None):
    """Arma el handler del botón 'Buscar cliente' para la ficha 'nombre_spec'. Se registra
    igual que _construir_handler_historial:
        _handler_x = _construir_handler_autocompletar("x", "cedula", _autocompletar_cliente)
        @app.action(FORM_SPECS["x"]["boton_historial"])
        def ver_historial_x(ack, body, client):
            _handler_x(ack, body, client)
    'autocompletar' es una función(valor_digitos) -> {"nombre": ..., "telefono": ...} o None.
    'mapeo_autocompletar' indica a qué campo del formulario va cada dato (por defecto
    {"nombre": "nombre", "telefono": "telefono"})."""
    def _handler(ack, body, client):
        ack()
        spec = FORM_SPECS[nombre_spec]
        valores_view = dict(body["view"]["state"]["values"])
        valor_input = (_valor_actual_bloque(valores_view, campo_busqueda) or "").strip()
        action_id = spec["boton_historial"]
        if not valor_input:
            texto_extra = "⚠️ Escribe primero la cédula arriba, y vuelve a apretar el botón."
        else:
            try:
                datos_cliente = autocompletar(_solo_digitos(valor_input))
            except Exception as e:
                datos_cliente = None
                print(f"⚠️ [{action_id}] No se pudo autocompletar los datos del cliente: {e}")
            algo_relleno = False
            if datos_cliente:
                mapeo = mapeo_autocompletar or {"nombre": "nombre", "telefono": "telefono"}
                campos_ids = {c["id"] for c in spec["campos"]}
                for clave_canonica, campo_id in mapeo.items():
                    valor = datos_cliente.get(clave_canonica)
                    if valor and campo_id in campos_ids:
                        valores_view[campo_id] = {"valor": {"value": valor}}
                        algo_relleno = True
            partes_texto = []
            if algo_relleno:
                partes_texto.append("✅ *Cliente encontrado:* se rellenaron los datos conocidos "
                                     "(puedes corregirlos si hace falta).")
            else:
                partes_texto.append("🔍 No encontré datos adicionales para esta cédula.")
            if calcular_score:
                try:
                    score = calcular_score(_solo_digitos(valor_input))
                except Exception as e:
                    score = None
                    print(f"⚠️ [{action_id}] No se pudo calcular el score de riesgo: {e}")
                if score:
                    partes_texto.append(f"📊 *Score de riesgo:* {score}")
            texto_extra = "\n\n".join(partes_texto)
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
# ============ FIN BOTÓN "Buscar cliente" ============
