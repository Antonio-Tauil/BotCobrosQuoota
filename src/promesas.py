"""
promesas.py — Radar de Promesas de Pago: el resumen automático de las 4 PM, marcar
promesas como Cumplida/Fallida (a mano o con los botones de /mis-promesas), y el
Cierre Diario de Cobros de las 6 PM. Las funciones generar_resumen_promesas() y
generar_cierre_diario() las usa scheduler.py para programarlas todos los días.
"""
import os
import re
import json
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config import app, CANAL_SEGUIMIENTO, CANAL_CIERRE, SUPERVISOR_ID, abrir_pestana_cacheada
from validaciones import _solo_digitos, parse_numero
from motor_formularios import _resumen_metricas_hoy, _marcar_inicio_reporte, _marcar_fin_reporte


# ============ RADAR DE PROMESAS DE PAGO (Fase 1) ============

# Tabla nombre del cobrador (como aparece en el Sheet) -> ID(s) de Slack
COBRADOR_SLACK_IDS = {
    "DIEGO": ["U0B3BAA8Y01", "U0B68124C9E"],
    "IARA": ["U0B58192UJH"],
    "REBECA": ["U0B59P76H8U"],
    "MARIANGEL": ["U0B4QM8D3PH"],
    "LUISMAR": ["U0BA6BWJWBF"],
    "ANGELY": ["U0BA8AHVA3Z"],
    "DANIEL": ["U0BAH6AFMA7"],
    "MARIANA": ["U0BHUF23EQY"],
    "ANDRES": ["U0BH22WRTQR"],
    "NELMAYRI": ["U0BK8E35T9A"],
    "ALEJANDRO": ["U0BLAV5EVSR"],
    "ISAAC": ["U0BL80V55DZ"],
    # BARBARA aún sin ID: se muestra en texto
}

# Cuántos días hacia atrás/adelante se considera una fecha "creíble"
RADAR_RANGO_DIAS = 90


def _medalla(posicion):
    """1->🥇, 2->🥈, 3->🥉, el resto->'4.', '5.', etc. — mismo helper que usa reportes.py
    para los rankings por persona (Cierre Diario aquí, semanal/mensual allá)."""
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(posicion, f"{posicion}.")


# ============ ALERTA SI UN REPORTE FALLA (mismo helper que reportes.py — antes solo quedaba
# en los logs de Railway, que nadie revisa a diario — ahora también llega un DM al supervisor,
# igual que hace main.py con cualquier error de un comando/botón) ============
def _avisar_falla_reporte(nombre_reporte, error):
    try:
        app.client.chat_postMessage(
            channel=SUPERVISOR_ID,
            text=(f"🔴 *Robotín: falló el reporte '{nombre_reporte}'*\n```{error}```\n"
                  "Revisa los logs de Railway para más detalle.")
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar la alerta de '{nombre_reporte}': {e}")
# ============ FIN ALERTA SI UN REPORTE FALLA ============


def _linea_compacta_promesas(items, con_dias=False, hoy=None, maximo=10):
    """Arma UNA SOLA línea con los nombres separados por comas (formato compacto, igual al
    mockup 'Idea C' que se le mostró a Antonio antes de rediseñar el Radar) en vez de una
    viñeta por persona — corta en 'maximo' y agrega cuántas quedan fuera. Si 'con_dias' es
    True, agrega 'hace Xd' (y 🔴 si son 3 días o más) — para la sección de Vencidas."""
    mostrados = items[:maximo]
    partes = []
    for it in mostrados:
        ced = f" · {it['cedula']}" if it.get("cedula") else ""
        if con_dias:
            dias = (hoy - it["fecha"]).days
            alerta = " 🔴" if dias >= 3 else ""
            partes.append(f"{it['nombre']}{ced} — hace {dias}d{alerta}")
        else:
            partes.append(f"{it['nombre']}{ced}")
    texto = ", ".join(partes)
    restantes = len(items) - len(mostrados)
    if restantes > 0:
        texto += f", … y {restantes} más (ver Sheet)"
    return texto


def _mencion_cobrador(nombre):
    clave = str(nombre).strip().upper()
    ids = COBRADOR_SLACK_IDS.get(clave)
    if ids:
        return " ".join(f"<@{uid}>" for uid in ids)
    return f"*{nombre or 'Sin cobrador'}*"


def _parsear_fecha_radar(texto, hoy):
    """Devuelve (fecha, motivo). fecha=date si es creíble; si no, fecha=None y motivo explica por qué.
    Corrige años de 3 dígitos (206->2026) y de 2 dígitos. Marca como 'revisar' lo fuera de rango."""
    if not texto or not str(texto).strip():
        return None, "sin fecha"
    t = str(texto).strip()
    # Separar día/mes/año aceptando / o -
    partes = re.split(r"[/\-]", t)
    if len(partes) != 3:
        return None, "formato raro"
    try:
        dia = int(partes[0]); mes = int(partes[1]); anio = int(partes[2])
    except ValueError:
        return None, "no numérica"
    # Corregir el año
    if anio < 100:            # 2 dígitos: 26 -> 2026
        anio += 2000
    elif 100 <= anio < 1000:  # 3 dígitos: 206 -> 2026 (toma el año actual como base)
        anio = int(str(hoy.year)[:1] + str(anio).zfill(3))  # 206 -> 2 + 026? -> ajuste abajo
        # Ajuste simple y seguro: usar el año actual (los de 3 dígitos son claramente error de tipeo)
        anio = hoy.year
    try:
        f = date(anio, mes, dia)
    except ValueError:
        return None, "día/mes inválido"
    # ¿Está dentro del rango creíble?
    if abs((f - hoy).days) > RADAR_RANGO_DIAS:
        return None, "fuera de rango"
    return f, "ok"


def generar_resumen_promesas():
    """Lee 'Contactados', agrupa promesas por cobrador y publica el resumen."""
    _marcar_inicio_reporte("Radar de Promesas")
    try:
        # Pestaña cacheada (ver abrir_pestana_cacheada en config.py) — no vuelve a gastar
        # cuota de lectura buscando la pestaña en cada llamada.
        sheet = abrir_pestana_cacheada(os.environ["SHEET_ID"], "Contactados")
        if sheet is None:
            # abrir_pestana_cacheada nunca lanza error (para no romper el bot por una
            # cuota excedida puntual) — pero si de verdad no se pudo abrir la hoja
            # (credenciales, permisos, etc.), igual hay que avisarle al supervisor,
            # no solo dejarlo en los logs de Railway.
            error = RuntimeError("No se encontró/pudo abrir la hoja 'Contactados'")
            print(f"❌ Radar: {error}")
            _avisar_falla_reporte("Radar de Promesas", error)
            return

        valores = sheet.get_all_values()
        hoy = datetime.now(ZoneInfo("America/Caracas")).date()
        hoy_txt = hoy.strftime("%d/%m/%Y")

        # Estructura: por cobrador -> {"hoy": [...], "vencidas": [...]}
        por_cobrador = {}
        revisar = []       # fechas raras
        total_hoy = 0
        total_venc = 0

        for i, fila in enumerate(valores):
            if i == 0:
                continue

            def celda(idx):
                return fila[idx].strip() if len(fila) > idx else ""

            nombre = celda(1) or "(sin nombre)"
            cedula = celda(3)
            compromiso = celda(4)
            cobrador = celda(5) or "Sin cobrador"
            estado = celda(7)
            if estado:  # ya resuelta
                continue

            fecha_prom, motivo = _parsear_fecha_radar(compromiso, hoy)

            if fecha_prom is None:
                # Solo mandamos a "revisar" las que tienen ALGO escrito pero está mal
                if motivo in ("formato raro", "no numérica", "día/mes inválido", "fuera de rango"):
                    revisar.append({"nombre": nombre, "cedula": cedula,
                                    "cobrador": cobrador, "texto": compromiso})
                continue  # "sin fecha" simplemente se ignora

            grupo = por_cobrador.setdefault(cobrador, {"hoy": [], "vencidas": [], "vistas": set()})
            # Evitar duplicados: misma cédula ya contada para este cobrador
            ced_norm = _solo_digitos(cedula)
            if ced_norm and ced_norm in grupo["vistas"]:
                continue
            if ced_norm:
                grupo["vistas"].add(ced_norm)
            item = {"nombre": nombre, "cedula": cedula, "fecha": fecha_prom}
            if fecha_prom == hoy:
                grupo["hoy"].append(item); total_hoy += 1
            elif fecha_prom < hoy:
                grupo["vencidas"].append(item); total_venc += 1
            # (futuras dentro de rango: no se listan hoy)

        # ---- Armar el mensaje ----
        # ============ MENSAJE REDISEÑADO (mismo estilo del resto del bot: resumen arriba con
        # lo más urgente + línea divisoria consistente con los demás mensajes) ============
        if total_venc > 0:
            resumen_titular = f"{total_venc} vencida(s) requieren atención"
        elif total_hoy > 0:
            resumen_titular = f"{total_hoy} para hoy"
        else:
            resumen_titular = "todo al día"
        lineas = []
        lineas.append(f"📅 *Radar de Promesas — {resumen_titular}*")
        lineas.append(f"{hoy_txt} · {total_hoy} para hoy · {total_venc} vencidas · {len(revisar)} por revisar")
        lineas.append("──────────────────────────")
        # ============ FIN MENSAJE REDISEÑADO (encabezado del radar) ============

        if not por_cobrador:
            lineas.append("")
            lineas.append("✅ No hay promesas de hoy ni vencidas con fecha válida.")
        else:
            # Orden alfabético por cobrador
            for cobrador in sorted(por_cobrador.keys(), key=lambda x: x.upper()):
                grupo = por_cobrador[cobrador]
                if not grupo["hoy"] and not grupo["vencidas"]:
                    continue
                lineas.append("")
                lineas.append(f"👤 *{cobrador.upper()}*  {_mencion_cobrador(cobrador)}")

                # ============ FORMATO COMPACTO (mismo estilo del mockup "Idea C": una sola
                # línea por sección, nombres separados por comas, en vez de una viñeta por
                # persona — más fácil de leer de un vistazo cuando hay muchas promesas) ============
                if grupo["hoy"]:
                    lineas.append(f"   ☀️ *Para hoy ({len(grupo['hoy'])}):* "
                                  f"{_linea_compacta_promesas(grupo['hoy'], maximo=15)}")

                if grupo["vencidas"]:
                    # Más recientes primero (fecha más nueva arriba)
                    grupo["vencidas"].sort(key=lambda x: x["fecha"], reverse=True)
                    total_v = len(grupo["vencidas"])
                    lineas.append(f"   ⏰ *Vencidas ({total_v}):* "
                                  f"{_linea_compacta_promesas(grupo['vencidas'], con_dias=True, hoy=hoy, maximo=10)}")
                # ============ FIN FORMATO COMPACTO ============

        # ---- Sección "revisar fecha" (resumida, no abruma) ----
        if revisar:
            lineas.append("")
            lineas.append("━━━━━━━━━━━━━━━━━━━━")
            lineas.append(f"⚠️ *{len(revisar)} promesa(s) con fecha rara* (corregir en el Sheet). Ejemplos:")
            for it in revisar[:5]:
                ced = f" · {it['cedula']}" if it["cedula"] else ""
                lineas.append(f"      • {it['nombre']}{ced} — escrito: \"{it['texto']}\"")
            if len(revisar) > 5:
                lineas.append(f"      … y {len(revisar) - 5} más.")

        # ---- Escalamiento al supervisor ----
        muy_vencidas = total_venc  # cuántas vencidas totales
        if muy_vencidas > 0:
            lineas.append("")
            lineas.append(f"🔴 <@{SUPERVISOR_ID}> hay *{muy_vencidas}* promesa(s) vencida(s) que requieren seguimiento.")

        mensaje = "\n".join(lineas).strip()

        # Slack corta mensajes muy largos (~40k). Si es enorme, avisamos y mandamos recortado.
        if len(mensaje) > 38000:
            mensaje = mensaje[:38000] + "\n\n… (lista recortada por tamaño; hay más en el Sheet)"

        app.client.chat_postMessage(channel=CANAL_SEGUIMIENTO, text=mensaje)
        print(f"✅ Radar publicado: {total_hoy} hoy, {total_venc} vencidas, {len(revisar)} por revisar")
    except Exception as e:
        print(f"❌ Error generando el resumen de promesas: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Radar de Promesas", e)
    finally:
        _marcar_fin_reporte("Radar de Promesas")


# Comando manual para PROBAR el radar sin esperar a las 4 PM
@app.command("/probar-radar")
def probar_radar(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando el radar de promesas ahora mismo... revisa el canal #cobranzas-seguimiento."
    )
    generar_resumen_promesas()
# ============ FIN RADAR DE PROMESAS ============


# ============ FASE 2: MARCAR PROMESAS (cumplida / fallida) ============
# Marca TODAS las filas de 'Contactados' con esa cédula:
#   Columna H (8) = Estado de promesa (Cumplida/Fallida)
#   Columna I (9) = Fecha resultado (hoy)
# Así la promesa desaparece del radar.

def _marcar_promesa(cedula_texto, estado):
    """Devuelve (cantidad_marcada, nombre_ejemplo)."""
    cedula_digitos = _solo_digitos(cedula_texto)
    if not cedula_digitos:
        return 0, None
    hoja = abrir_pestana_cacheada(os.environ["SHEET_ID"], "Contactados")
    if hoja is None:
        return 0, None
    valores = hoja.get_all_values()
    hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")
    marcadas = 0
    nombre_ejemplo = None
    for i, fila in enumerate(valores):
        if i == 0:
            continue
        # Columna D (índice 3) = Cédula
        if len(fila) > 3 and _solo_digitos(fila[3]) == cedula_digitos:
            num_fila = i + 1  # gspread cuenta desde 1
            hoja.update_cell(num_fila, 8, estado)   # H = Estado de promesa
            hoja.update_cell(num_fila, 9, hoy_txt)  # I = Fecha resultado
            marcadas += 1
            if nombre_ejemplo is None and len(fila) > 1:
                nombre_ejemplo = fila[1]
    return marcadas, nombre_ejemplo


def _comando_marcar(ack, body, client, estado, emoji):
    ack()
    texto = (body.get("text") or "").strip()
    canal = body["channel_id"]
    usuario = body["user_id"]
    sufijo = "cumplida" if estado == "Cumplida" else "fallida"
    if not texto:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"Escribe la cédula. Ejemplo: `/promesa-{sufijo} 12345678`")
        return
    try:
        marcadas, nombre = _marcar_promesa(texto, estado)
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al marcar: {type(e).__name__}: {e}")
        return
    if marcadas == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"⚠️ No encontré la cédula *{texto}* en Contactados. No se marcó nada.")
        return
    nombre_txt = f" ({nombre})" if nombre else ""
    # Aviso público en el canal de seguimiento (quién marcó qué)
    try:
        client.chat_postMessage(
            channel=CANAL_SEGUIMIENTO,
            text=f"{emoji} <@{usuario}> marcó como *{estado}* la promesa de la cédula *{texto}*{nombre_txt} — {marcadas} registro(s) actualizado(s)."
        )
    except Exception as e:
        print(f"⚠️ No se pudo avisar en el canal de seguimiento: {e}")
    # Confirmación a quien ejecutó
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"{emoji} Listo. Marqué {marcadas} registro(s) de la cédula {texto} como {estado}.")


@app.command("/promesa-cumplida")
def promesa_cumplida(ack, body, client):
    _comando_marcar(ack, body, client, "Cumplida", "✅")


@app.command("/promesa-fallida")
def promesa_fallida(ack, body, client):
    _comando_marcar(ack, body, client, "Fallida", "❌")
# ============ FIN FASE 2 ============


# ============ CIERRE DIARIO DE COBROS (reporte automático 6 PM) ============


def generar_cierre_diario():
    """Lee 'Pagos Recibidos', suma los cobros de HOY y publica el cierre del día."""
    _marcar_inicio_reporte("Cierre Diario")
    try:
        hoja = abrir_pestana_cacheada(os.environ["SHEET_ID"], "Pagos Recibidos")
        if hoja is None:
            error = RuntimeError("No se encontró/pudo abrir la hoja 'Pagos Recibidos'")
            print(f"❌ Cierre: {error}")
            _avisar_falla_reporte("Cierre Diario", error)
            return
        valores = hoja.get_all_values()
        hoy_txt = datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y")

        # Columnas (0-based): 0 fecha, 4 monto_bs, 7 monto_usd, 9 cobrador
        total_usd = 0.0
        total_bs = 0.0
        cantidad = 0
        por_cobrador = {}  # nombre -> {"n": x, "usd": x, "bs": x}

        for i, fila in enumerate(valores):
            if i == 0:
                continue

            def celda(idx):
                return fila[idx].strip() if len(fila) > idx else ""

            fecha_celda = celda(0).split()[0] if celda(0) else ""  # quita hora si la hubiera
            if fecha_celda != hoy_txt:
                continue

            cantidad += 1
            cobrador_raw = celda(9) or "Sin cobrador"
            # Normalizar: ignorar mayúsculas/minúsculas y espacios extra (REBECA = rebeca = Rebeca)
            clave = " ".join(cobrador_raw.split()).upper()
            nombre_bonito = " ".join(cobrador_raw.split()).title()

            try:
                usd = parse_numero(celda(7))
            except (ValueError, ZeroDivisionError):
                usd = 0.0
            try:
                bs = parse_numero(celda(4))
            except (ValueError, ZeroDivisionError):
                bs = 0.0

            total_usd += usd
            total_bs += bs
            g = por_cobrador.setdefault(clave, {"nombre": nombre_bonito, "n": 0, "usd": 0.0, "bs": 0.0})
            g["n"] += 1
            g["usd"] += usd
            g["bs"] += bs

        # ============ MENSAJE REDISEÑADO (mismo estilo del resto del bot: línea divisoria
        # consistente, y ranking con medallas para el top de cobradores del día) ============
        lineas = [f"📊 *Cierre del día — {hoy_txt}*", "──────────────────────────"]
        if cantidad == 0:
            lineas.append("No se registraron cobros hoy.")
        else:
            lineas.append(f"💰 *Total cobrado:* Bs. {total_bs:,.2f}  ·  ${total_usd:,.2f}")
            lineas.append(f"📝 *Cantidad de cobros:* {cantidad}")
            lineas.append("")
            lineas.append("*Por cobrador:*")
            ranking = sorted(por_cobrador.keys(), key=lambda x: por_cobrador[x]["usd"], reverse=True)
            for posicion, clave in enumerate(ranking, 1):
                g = por_cobrador[clave]
                lineas.append(f"   {_medalla(posicion)} {g['nombre']} — {g['n']} cobro(s) — ${g['usd']:,.2f}")
        # ============ FIN MENSAJE REDISEÑADO (cierre diario) ============

        mensaje = "\n".join(lineas)

        # ============ RESUMEN DE ACTIVIDAD DEL DÍA (para gerencia) ============
        # Cuántos formularios se enviaron/aprobaron/rechazaron hoy en TODOS los comandos
        # (no solo cobros) — ver motor_formularios.py (_registrar_metrica/_resumen_metricas_hoy).
        resumen_actividad = _resumen_metricas_hoy()
        if resumen_actividad:
            mensaje += "\n\n" + resumen_actividad
        # ============ FIN RESUMEN DE ACTIVIDAD DEL DÍA ============

        app.client.chat_postMessage(channel=CANAL_CIERRE, text=mensaje)
        print(f"✅ Cierre diario publicado: {cantidad} cobros, ${total_usd:,.2f}")
    except Exception as e:
        print(f"❌ Error generando el cierre diario: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Cierre Diario", e)
    finally:
        _marcar_fin_reporte("Cierre Diario")


# Comando manual para probar el cierre sin esperar las 6 PM
@app.command("/probar-cierre")
def probar_cierre(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando el cierre del día ahora mismo... revisa el canal #cobranzas-log."
    )
    generar_cierre_diario()
# ============ FIN CIERRE DIARIO DE COBROS ============




# ============ COMANDO /mis-promesas (con botones para marcar) ============
MIS_PROMESAS_LIMITE = 15  # cuántas promesas mostrar con botones a la vez


def _cobrador_por_slack_id(user_id):
    """Busca a qué nombre de cobrador corresponde el usuario de Slack que escribe."""
    for nombre, ids in COBRADOR_SLACK_IDS.items():
        if user_id in ids:
            return nombre
    return None


def _boton_marcar(cedula, nombre, estado):
    """Crea un botón con confirmación nativa para marcar una promesa."""
    emoji = "✅" if estado == "Cumplida" else "❌"
    estilo = "primary" if estado == "Cumplida" else "danger"
    valor = json.dumps({"c": cedula, "e": estado, "n": nombre[:60]})
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": f"{emoji} {estado}"},
        "style": estilo,
        "action_id": f"marcar_{estado.lower()}_btn",
        "value": valor,
        "confirm": {
            "title": {"type": "plain_text", "text": "¿Confirmar?"},
            "text": {"type": "mrkdwn", "text": f"Marcar la promesa de *{nombre}* (cédula {cedula}) como *{estado}*?"},
            "confirm": {"type": "plain_text", "text": "Sí, marcar"},
            "deny": {"type": "plain_text", "text": "Cancelar"}
        }
    }


@app.command("/mis-promesas")
def mis_promesas(ack, body, client):
    ack()
    canal = body["channel_id"]
    usuario = body["user_id"]

    nombre_cobrador = _cobrador_por_slack_id(usuario)
    if not nombre_cobrador:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text="No te reconozco como cobrador en la lista. Pídele al administrador que agregue tu ID de Slack.")
        return

    try:
        hoja = abrir_pestana_cacheada(os.environ["SHEET_ID"], "Contactados")
        if hoja is None:
            client.chat_postEphemeral(channel=canal, user=usuario, text="❌ No encontré la hoja 'Contactados'.")
            return
        valores = hoja.get_all_values()
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al leer las promesas: {type(e).__name__}: {e}")
        return

    hoy = datetime.now(ZoneInfo("America/Caracas")).date()
    objetivo = nombre_cobrador.strip().upper()
    de_hoy = []
    vencidas = []
    vistas = set()

    for i, fila in enumerate(valores):
        if i == 0:
            continue

        def celda(idx):
            return fila[idx].strip() if len(fila) > idx else ""

        if celda(5).strip().upper() != objetivo:
            continue
        if celda(7):  # ya tiene estado
            continue
        f, _motivo = _parsear_fecha_radar(celda(4), hoy)
        if f is None:
            continue
        ced = _solo_digitos(celda(3))
        if ced and ced in vistas:
            continue
        if ced:
            vistas.add(ced)
        item = {"nombre": celda(1) or "(sin nombre)", "cedula": celda(3), "fecha": f}
        if f == hoy:
            de_hoy.append(item)
        elif f < hoy:
            vencidas.append(item)

    total = len(de_hoy) + len(vencidas)
    if total == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"✅ *{nombre_cobrador}*, no tienes promesas pendientes ni vencidas. ¡Estás al día!")
        return

    # Ordenar vencidas: más recientes primero
    vencidas.sort(key=lambda x: x["fecha"], reverse=True)
    # Combinar: primero las de hoy, luego vencidas
    lista = [("hoy", it) for it in de_hoy] + [("venc", it) for it in vencidas]

    bloques = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"📋 *Tus promesas pendientes — {nombre_cobrador}* ({total})\nMarca cada una con los botones. Se te pedirá confirmar."}
    }, {"type": "divider"}]

    mostradas = 0
    for tipo, it in lista:
        if mostradas >= MIS_PROMESAS_LIMITE:
            break
        ced = f" · {it['cedula']}" if it["cedula"] else ""
        if tipo == "hoy":
            etiqueta = "☀️ hoy"
        else:
            dias = (hoy - it["fecha"]).days
            alerta = " 🔴" if dias >= 3 else ""
            etiqueta = f"⏰ hace {dias}d{alerta}"
        bloques.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"• *{it['nombre']}*{ced} — {etiqueta}"}
        })
        bloques.append({
            "type": "actions",
            "elements": [
                _boton_marcar(it["cedula"], it["nombre"], "Cumplida"),
                _boton_marcar(it["cedula"], it["nombre"], "Fallida"),
            ]
        })
        mostradas += 1

    if total > mostradas:
        bloques.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"… y {total - mostradas} más. Marca estas y vuelve a escribir `/mis-promesas` para ver las siguientes."}]
        })

    client.chat_postEphemeral(channel=canal, user=usuario, blocks=bloques,
                              text=f"Tus promesas pendientes ({total})")


def _procesar_boton_marcar(ack, body, action, client, estado):
    ack()
    usuario = body["user"]["id"]
    canal = body["channel"]["id"]
    try:
        data = json.loads(action["value"])
    except Exception:
        data = {}
    cedula = data.get("c", "")
    nombre = data.get("n", "")
    try:
        marcadas, _n = _marcar_promesa(cedula, estado)
    except Exception as e:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"❌ Error al marcar: {type(e).__name__}: {e}")
        return
    emoji = "✅" if estado == "Cumplida" else "❌"
    if marcadas == 0:
        client.chat_postEphemeral(channel=canal, user=usuario,
            text=f"⚠️ No encontré la cédula {cedula} en Contactados.")
        return
    client.chat_postEphemeral(channel=canal, user=usuario,
        text=f"{emoji} Marcaste a *{nombre}* ({cedula}) como *{estado}*. ({marcadas} registro/s)")
    # Aviso público en el canal de seguimiento
    try:
        client.chat_postMessage(channel=CANAL_SEGUIMIENTO,
            text=f"{emoji} <@{usuario}> marcó como *{estado}* la promesa de {nombre} ({cedula}).")
    except Exception as e:
        print(f"⚠️ No se pudo avisar en seguimiento: {e}")


@app.action("marcar_cumplida_btn")
def marcar_cumplida_btn(ack, body, action, client):
    _procesar_boton_marcar(ack, body, action, client, "Cumplida")


@app.action("marcar_fallida_btn")
def marcar_fallida_btn(ack, body, action, client):
    _procesar_boton_marcar(ack, body, action, client, "Fallida")
# ============ FIN COMANDO /mis-promesas ============
