"""
reportes.py — Reporte semanal automático de las áreas que manejan pagos. Cada lunes en
la mañana (hora Venezuela) se arma un resumen de la semana pasada (lunes a domingo) para
cada área, y se publica en el canal de esa misma área — así queda un registro semanal sin
que nadie tenga que armarlo a mano.

Áreas incluidas (todas las que registran un monto de pago):
  1. Cobranzas (/cobro + /domiciliar juntos)      -> #cobranzas-log
  2. Call Center (/cobro-callcenter)               -> mismo canal del comando
  3. Comercial (/cobro-comercial)                  -> mismo canal del comando
  4. Conciliación de Cobranzas (/conciliar)        -> mismo canal del comando (solo Bs, no
     guarda un monto en USD)
  5. Mercadeo, Conciliación de Pagos (/merca-reporte) -> canal de mercadeo-pagos

No incluidas (porque no registran un monto de pago): /contactar, /contacto-legal,
/clientes-escalados, /liquidacion-nueva, /liquidacion-estatus, /incidencia-fullcode,
Incidencias Técnicas de Mercadeo.
"""
import os
import re
import json
import time
import gspread
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

from config import app, SHEET_ID_MERCADEO, CANAL_CIERRE, CANAL_MERCADEO_PAGOS, CANAL_REPORTES_MENSUALES, SUPERVISOR_ID
from validaciones import parse_numero, _columna_por_nombre
from motor_formularios import FORM_SPECS
from cobros import _abrir_hoja_domiciliacion, _abrir_hoja_cobro2, _abrir_hoja_conciliacion, _abrir_hoja_comercial
from mercadeo import _abrir_hoja_mercadeo


# ============ REPORTE SEMANAL DE PAGOS (lunes en la mañana) ============

def _abrir_hoja_pagos_recibidos():
    """Abre la misma hoja 'Pagos Recibidos' (SHEET_ID) donde se guardan los cobros de
    /cobro, igual que hace el Cierre Diario en promesas.py."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    cliente = gspread.authorize(creds)
    try:
        return cliente.open_by_key(os.environ["SHEET_ID"]).worksheet("Pagos Recibidos")
    except Exception as e:
        print(f"❌ Reporte semanal: no se encontró la hoja 'Pagos Recibidos': {e}")
        return None


def _parsear_fecha(texto):
    """Convierte 'DD/MM/AAAA' (o con '-') a un date de Python. Devuelve None si no se
    puede leer (celda vacía, formato raro, etc.) — esas filas simplemente se ignoran."""
    t = str(texto or "").strip()
    if not t:
        return None
    partes = re.split(r"[/\-]", t)
    if len(partes) != 3:
        return None
    try:
        d, m, y = int(partes[0]), int(partes[1]), int(partes[2])
        if y < 100:
            y += 2000
        return date(y, m, d)
    except ValueError:
        return None


def _parse_monto_seguro(texto):
    """Igual que parse_numero, pero nunca lanza error: si el texto no es un número
    (celda vacía, '(No calculable)', etc.), simplemente cuenta como 0."""
    try:
        return parse_numero(texto)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0.0


def _es_error_de_cuota(e):
    """True si el error es un '429 Quota exceeded' de Google Sheets (demasiadas consultas
    por minuto) — ese caso sí vale la pena reintentar un poco después. Cualquier otro tipo
    de error (credenciales, hoja no encontrada, etc.) no se reintenta, porque esperar no
    lo va a arreglar."""
    texto = str(e)
    return "429" in texto or "Quota exceeded" in texto or "RESOURCE_EXHAUSTED" in texto


def _con_reintento(func, intentos=3, espera_inicial=5):
    """Ejecuta 'func' (sin argumentos). Si Google Sheets responde '429 Quota exceeded'
    (se hicieron demasiadas consultas seguidas), espera un poco y lo intenta de nuevo,
    esperando cada vez más (5s, luego 15s) antes de rendirse. Esto evita que una ráfaga
    de lecturas (como generar varios reportes seguidos) pierda datos por las puras."""
    espera = espera_inicial
    for intento in range(intentos):
        try:
            return func()
        except Exception as e:
            if not _es_error_de_cuota(e) or intento == intentos - 1:
                raise
            print(f"⚠️ Reportes: Google Sheets pidió esperar (cuota excedida), "
                  f"reintentando en {espera}s (intento {intento + 1}/{intentos})...")
            time.sleep(espera)
            espera *= 3


# ============ ALERTA SI UN REPORTE FALLA (antes solo quedaba en los logs de Railway, que
# nadie revisa a diario — ahora también llega un DM al supervisor, igual que hace
# main.py con cualquier error de un comando/botón) ============
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


def _sumar_area(ws, col_fecha, col_monto_bs, col_monto_usd, col_persona, lunes, domingo):
    """Lee 'ws' y suma lo registrado entre 'lunes' y 'domingo' (ambos incluidos): total en
    Bs, total en USD (si se pasó esa columna), cantidad de casos, y un desglose por persona
    (si se pasó esa columna). Si no encuentra alguna columna esperada, avisa por consola y
    sigue con lo que sí pudo leer — nunca detiene el resto del reporte. Las consultas a
    Google Sheets pasan por _con_reintento, por si Google pide esperar por cuota."""
    resumen = {"conteo": 0, "total_bs": 0.0, "total_usd": 0.0, "por_persona": {}}
    if ws is None:
        return resumen
    try:
        idx_fecha = _con_reintento(lambda: _columna_por_nombre(ws, col_fecha))
        idx_bs = _con_reintento(lambda: _columna_por_nombre(ws, col_monto_bs)) if col_monto_bs else None
        idx_usd = _con_reintento(lambda: _columna_por_nombre(ws, col_monto_usd)) if col_monto_usd else None
        idx_persona = _con_reintento(lambda: _columna_por_nombre(ws, col_persona)) if col_persona else None
        if idx_fecha is None:
            print(f"⚠️ Reporte semanal: no se encontró la columna de fecha '{col_fecha}' en '{ws.title}'.")
            return resumen
        valores = _con_reintento(lambda: ws.get_all_values())
    except Exception as e:
        print(f"⚠️ Reporte semanal: error abriendo '{getattr(ws, 'title', '?')}': {type(e).__name__}: {e}")
        return resumen

    def celda(fila, idx):
        return fila[idx - 1].strip() if idx and len(fila) >= idx else ""

    for fila in valores[1:]:
        fecha_txt = celda(fila, idx_fecha).split()[0] if celda(fila, idx_fecha) else ""
        fecha = _parsear_fecha(fecha_txt)
        if fecha is None or not (lunes <= fecha <= domingo):
            continue
        resumen["conteo"] += 1
        monto_bs = _parse_monto_seguro(celda(fila, idx_bs)) if idx_bs else 0.0
        monto_usd = _parse_monto_seguro(celda(fila, idx_usd)) if idx_usd else 0.0
        resumen["total_bs"] += monto_bs
        resumen["total_usd"] += monto_usd
        if idx_persona:
            persona = celda(fila, idx_persona) or "(sin especificar)"
            persona = " ".join(persona.split()).upper()
            resumen["por_persona"][persona] = resumen["por_persona"].get(persona, 0.0) + monto_bs
    return resumen


def _combinar_resumenes(*resumenes):
    """Junta dos o más resúmenes de _sumar_area en uno solo (para 'Cobranzas', que junta
    /cobro y /domiciliar en un mismo reporte)."""
    total = {"conteo": 0, "total_bs": 0.0, "total_usd": 0.0, "por_persona": {}}
    for r in resumenes:
        total["conteo"] += r["conteo"]
        total["total_bs"] += r["total_bs"]
        total["total_usd"] += r["total_usd"]
        for persona, monto in r["por_persona"].items():
            total["por_persona"][persona] = total["por_persona"].get(persona, 0.0) + monto
    return total


def _medalla(posicion):
    """1->🥇, 2->🥈, 3->🥉, el resto->'4.', '5.', etc. — para los rankings por persona de
    los reportes (semanal, mensual, cierre diario), para que se note de un vistazo quién
    va ganando en vez de una lista plana de viñetas."""
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(posicion, f"{posicion}.")


def _barra_comparativa(valor, referencia, ancho=10):
    """Barra visual (▰ llenos / ▱ vacíos) proporcional a 'valor' contra el mayor entre
    'valor' y 'referencia' — para comparar dos montos (este mes vs. el anterior) de un
    vistazo, sin depender de que Slack alinee texto en columnas."""
    tope = max(valor, referencia, 0.01)
    llenos = round((valor / tope) * ancho)
    llenos = max(0, min(ancho, llenos))
    return "▰" * llenos + "▱" * (ancho - llenos)


# ============ MENSAJE REDISEÑADO (mismo estilo del resto del bot: resumen arriba + línea
# divisoria, y ranking con medallas para el top 3 por persona) ============
def _formatear_reporte(titulo, emoji, resumen, lunes, domingo, mostrar_usd=True):
    lineas = [
        f"{emoji} *Reporte semanal — {titulo}*",
        f"Semana: {lunes.strftime('%d/%m/%Y')} al {domingo.strftime('%d/%m/%Y')}",
        "──────────────────────────",
    ]
    if resumen["conteo"] == 0:
        lineas.append("No hubo registros esta semana.")
        return "\n".join(lineas)
    if mostrar_usd:
        lineas.append(f"💰 *Total:* Bs. {resumen['total_bs']:,.2f}  ·  ${resumen['total_usd']:,.2f}")
    else:
        lineas.append(f"💰 *Total:* Bs. {resumen['total_bs']:,.2f}")
    lineas.append(f"📝 *Casos:* {resumen['conteo']}")
    if resumen["por_persona"]:
        lineas.append("")
        lineas.append("*Por persona:*")
        ranking = sorted(resumen["por_persona"].keys(), key=lambda p: resumen["por_persona"][p], reverse=True)
        for posicion, persona in enumerate(ranking, 1):
            lineas.append(f"   {_medalla(posicion)} {persona.title()} — Bs. {resumen['por_persona'][persona]:,.2f}")
    return "\n".join(lineas)
# ============ FIN MENSAJE REDISEÑADO (semanal) ============


def _rango_semana_pasada():
    """La semana pasada completa (lunes a domingo), sin importar qué día se llame esta
    función — así /probar-reporte-semanal siempre da la última semana cerrada."""
    hoy = datetime.now(ZoneInfo("America/Caracas")).date()
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    domingo_pasado = lunes_actual - timedelta(days=1)
    lunes_pasado = domingo_pasado - timedelta(days=6)
    return lunes_pasado, domingo_pasado


def generar_reportes_semanales():
    """Se ejecuta cada lunes en la mañana: arma el resumen de la semana pasada para cada
    área que maneja pagos, y lo publica en el canal de esa misma área. Cada área se
    procesa por separado (con su propio try/except) para que un problema en una no
    impida que las demás se publiquen."""
    lunes_pasado, domingo_pasado = _rango_semana_pasada()

    try:
        r_cobro = _sumar_area(_con_reintento(_abrir_hoja_pagos_recibidos), "Fecha", "MontoBs", "MontoUsd",
                               "Cobrador", lunes_pasado, domingo_pasado)
        r_domic = _sumar_area(_con_reintento(_abrir_hoja_domiciliacion), "Fecha", "Monto recuperado Bs", "MontoUsd",
                               "Cobrador", lunes_pasado, domingo_pasado)
        r_cobranzas = _combinar_resumenes(r_cobro, r_domic)
        app.client.chat_postMessage(
            channel=CANAL_CIERRE,
            text=_formatear_reporte("Cobranzas", "📊", r_cobranzas, lunes_pasado, domingo_pasado)
        )
    except Exception as e:
        print(f"❌ Reporte semanal [Cobranzas]: error: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Reporte semanal — Cobranzas", e)

    try:
        r_cc = _sumar_area(_con_reintento(_abrir_hoja_cobro2), "Fecha", "MontoBs", "MontoUsd",
                            None, lunes_pasado, domingo_pasado)
        app.client.chat_postMessage(
            channel=FORM_SPECS["cobro_callcenter"]["canal"],
            text=_formatear_reporte("Call Center", "📞", r_cc, lunes_pasado, domingo_pasado)
        )
    except Exception as e:
        print(f"❌ Reporte semanal [Call Center]: error: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Reporte semanal — Call Center", e)

    try:
        r_com = _sumar_area(_con_reintento(_abrir_hoja_comercial), "Fecha", "MontoBs", "MontoUsd",
                             None, lunes_pasado, domingo_pasado)
        app.client.chat_postMessage(
            channel=FORM_SPECS["cobro_comercial"]["canal"],
            text=_formatear_reporte("Comercial", "🤝", r_com, lunes_pasado, domingo_pasado)
        )
    except Exception as e:
        print(f"❌ Reporte semanal [Comercial]: error: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Reporte semanal — Comercial", e)

    try:
        r_conc = _sumar_area(_con_reintento(_abrir_hoja_conciliacion), "Fecha conciliación", "Monto reportado",
                              None, "Conciliador", lunes_pasado, domingo_pasado)
        app.client.chat_postMessage(
            channel=FORM_SPECS["conciliar"]["canal"],
            text=_formatear_reporte("Conciliación de Cobranzas", "🧾", r_conc, lunes_pasado, domingo_pasado,
                                     mostrar_usd=False)
        )
    except Exception as e:
        print(f"❌ Reporte semanal [Conciliación]: error: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Reporte semanal — Conciliación", e)

    try:
        r_merc = _sumar_area(_con_reintento(lambda: _abrir_hoja_mercadeo("Conciliacion")), "Fecha de Reporte",
                              "Monto en Bs", "Monto en USD", None, lunes_pasado, domingo_pasado)
        app.client.chat_postMessage(
            channel=CANAL_MERCADEO_PAGOS,
            text=_formatear_reporte("Mercadeo (Pagos)", "🛒", r_merc, lunes_pasado, domingo_pasado)
        )
    except Exception as e:
        print(f"❌ Reporte semanal [Mercadeo]: error: {type(e).__name__}: {e}")
        _avisar_falla_reporte("Reporte semanal — Mercadeo", e)

    print("✅ Reportes semanales generados.")


# Comando manual para probar los reportes sin esperar al lunes
@app.command("/probar-reporte-semanal")
def probar_reporte_semanal(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando los reportes semanales ahora mismo... revisa cada canal de área."
    )
    generar_reportes_semanales()
# ============ FIN REPORTE SEMANAL DE PAGOS ============


# ============ REPORTE MENSUAL DE PAGOS (día 1 de cada mes, para gerencia) ============
# Junta los 5 reportes en UN SOLO canal (CANAL_REPORTES_MENSUALES), con la comparación
# contra el mes anterior — a diferencia del semanal, que va cada uno a su propio canal.

def _calcular_area_cobranzas(inicio, fin):
    r_cobro = _sumar_area(_con_reintento(_abrir_hoja_pagos_recibidos), "Fecha", "MontoBs", "MontoUsd",
                           "Cobrador", inicio, fin)
    r_domic = _sumar_area(_con_reintento(_abrir_hoja_domiciliacion), "Fecha", "Monto recuperado Bs", "MontoUsd",
                           "Cobrador", inicio, fin)
    return _combinar_resumenes(r_cobro, r_domic)


def _calcular_area_callcenter(inicio, fin):
    return _sumar_area(_con_reintento(_abrir_hoja_cobro2), "Fecha", "MontoBs", "MontoUsd", None, inicio, fin)


def _calcular_area_comercial(inicio, fin):
    return _sumar_area(_con_reintento(_abrir_hoja_comercial), "Fecha", "MontoBs", "MontoUsd", None, inicio, fin)


def _calcular_area_conciliacion(inicio, fin):
    return _sumar_area(_con_reintento(_abrir_hoja_conciliacion), "Fecha conciliación", "Monto reportado",
                        None, "Conciliador", inicio, fin)


def _calcular_area_mercadeo(inicio, fin):
    return _sumar_area(_con_reintento(lambda: _abrir_hoja_mercadeo("Conciliacion")), "Fecha de Reporte",
                        "Monto en Bs", "Monto en USD", None, inicio, fin)


def _primer_dia_mes(fecha):
    return fecha.replace(day=1)


def _ultimo_dia_mes_anterior(fecha):
    return _primer_dia_mes(fecha) - timedelta(days=1)


def _rango_mes_pasado():
    """El mes calendario que acaba de terminar (ej. si hoy es 01/09, devuelve todo agosto).
    Sin importar qué día se llame esta función — así /probar-reporte-mensual siempre da
    el último mes cerrado."""
    hoy = datetime.now(ZoneInfo("America/Caracas")).date()
    fin_mes_pasado = _ultimo_dia_mes_anterior(hoy)
    inicio_mes_pasado = _primer_dia_mes(fin_mes_pasado)
    return inicio_mes_pasado, fin_mes_pasado


def _rango_mes_anterior_a(inicio_de_un_mes):
    """Dado el primer día de un mes, devuelve (inicio, fin) del mes ANTERIOR a ese — para
    poder comparar 'este mes' contra 'el mes pasado de ese'."""
    fin_mes_anterior = inicio_de_un_mes - timedelta(days=1)
    inicio_mes_anterior = _primer_dia_mes(fin_mes_anterior)
    return inicio_mes_anterior, fin_mes_anterior


# ============ MENSAJE REDISEÑADO (mismo estilo del resto del bot: resumen arriba + línea
# divisoria, ranking con medallas, y una barra visual comparando este mes contra el
# anterior en vez de solo el texto del % de cambio) ============
def _formatear_reporte_mensual(titulo, emoji, resumen, resumen_anterior, inicio, fin, mostrar_usd=True):
    lineas = [
        f"{emoji} *Reporte mensual — {titulo}*",
        f"Mes: {inicio.strftime('%m/%Y')} ({inicio.strftime('%d/%m')} al {fin.strftime('%d/%m/%Y')})",
        "──────────────────────────",
    ]
    if resumen["conteo"] == 0:
        lineas.append("No hubo registros este mes.")
        return "\n".join(lineas)
    if mostrar_usd:
        lineas.append(f"💰 *Total:* Bs. {resumen['total_bs']:,.2f}  ·  ${resumen['total_usd']:,.2f}")
    else:
        lineas.append(f"💰 *Total:* Bs. {resumen['total_bs']:,.2f}")
    lineas.append(f"📝 *Casos:* {resumen['conteo']}")
    if resumen_anterior and resumen_anterior["total_bs"] > 0:
        cambio = ((resumen["total_bs"] - resumen_anterior["total_bs"]) / resumen_anterior["total_bs"]) * 100
        flecha = "📈" if cambio >= 0 else "📉"
        lineas.append(f"{flecha} *Vs. mes anterior:* {cambio:+.1f}% en Bs")
        lineas.append(f"   `Ant.` {_barra_comparativa(resumen_anterior['total_bs'], resumen['total_bs'])}")
        lineas.append(f"   `Este` {_barra_comparativa(resumen['total_bs'], resumen_anterior['total_bs'])}")
    else:
        lineas.append("📈 *Vs. mes anterior:* sin datos del mes anterior para comparar")
    if resumen["por_persona"]:
        lineas.append("")
        lineas.append("*Por persona (top 5):*")
        top5 = sorted(resumen["por_persona"].keys(), key=lambda p: resumen["por_persona"][p], reverse=True)[:5]
        for posicion, persona in enumerate(top5, 1):
            lineas.append(f"   {_medalla(posicion)} {persona.title()} — Bs. {resumen['por_persona'][persona]:,.2f}")
    return "\n".join(lineas)
# ============ FIN MENSAJE REDISEÑADO (mensual) ============


def generar_reportes_mensuales():
    """Se ejecuta el día 1 de cada mes en la mañana: arma el resumen del mes que acaba de
    terminar para cada área, lo compara contra el mes anterior a ese, y publica los 5
    juntos en CANAL_REPORTES_MENSUALES (para que gerencia los vea todos en un solo lugar).
    Cada área se procesa por separado para que un problema en una no impida las demás."""
    inicio_mes, fin_mes = _rango_mes_pasado()
    inicio_mes_ant, fin_mes_ant = _rango_mes_anterior_a(inicio_mes)

    areas = [
        ("Cobranzas", "📊", _calcular_area_cobranzas, True),
        ("Call Center", "📞", _calcular_area_callcenter, True),
        ("Comercial", "🤝", _calcular_area_comercial, True),
        ("Conciliación de Cobranzas", "🧾", _calcular_area_conciliacion, False),
        ("Mercadeo (Pagos)", "🛒", _calcular_area_mercadeo, True),
    ]

    for titulo, emoji, calcular, mostrar_usd in areas:
        try:
            resumen_actual = calcular(inicio_mes, fin_mes)
            resumen_anterior = calcular(inicio_mes_ant, fin_mes_ant)
            app.client.chat_postMessage(
                channel=CANAL_REPORTES_MENSUALES,
                text=_formatear_reporte_mensual(titulo, emoji, resumen_actual, resumen_anterior,
                                                 inicio_mes, fin_mes, mostrar_usd=mostrar_usd)
            )
        except Exception as e:
            print(f"❌ Reporte mensual [{titulo}]: error: {type(e).__name__}: {e}")
            _avisar_falla_reporte(f"Reporte mensual — {titulo}", e)

    print("✅ Reportes mensuales generados.")


# Comando manual para probar el reporte mensual sin esperar al día 1
@app.command("/probar-reporte-mensual")
def probar_reporte_mensual(ack, body, client):
    ack()
    client.chat_postEphemeral(
        channel=body["channel_id"], user=body["user_id"],
        text="⏳ Generando los reportes mensuales ahora mismo... revisa el canal de reportes mensuales."
    )
    generar_reportes_mensuales()
# ============ FIN REPORTE MENSUAL DE PAGOS ============
