"""
validaciones.py — Funciones de apoyo usadas por todo el bot: convertir texto a número,
evitar guardar el mismo registro dos veces, guardar/leer en el Sheet por NOMBRE de columna
(no por posición), y validar cédula/teléfono/fecha en los formularios.
"""
import re
import time
import unicodedata
import threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

def parse_numero(texto):
    if texto is None:
        raise ValueError("vacío")
    s = re.sub(r"[^0-9.,\-]", "", str(texto).strip())
    # Quitar separadores sobrantes al inicio/fin (ej. el punto que deja "Bs.")
    neg = s.startswith("-")
    s = s.lstrip("-").strip(".,")
    if s == "":
        raise ValueError("sin dígitos")
    if neg:
        s = "-" + s
    tiene_punto = "." in s
    tiene_coma = "," in s
    if tiene_punto and tiene_coma:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif tiene_coma:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            _, _, dec = s.partition(",")
            s = s.replace(",", "") if len(dec) == 3 else s.replace(",", ".")
    elif tiene_punto:
        if s.count(".") > 1:
            s = s.replace(".", "")
        else:
            _, _, dec = s.partition(".")
            if len(dec) == 3:
                s = s.replace(".", "")
    return float(s)
# ============ FIN FUNCIÓN parse_numero ============


# ============ BLINDAJE ANTI-DUPLICADOS ============

def _id_amigable(prefijo, ts):
    try:
        dt = datetime.fromtimestamp(float(ts), ZoneInfo("America/Caracas"))
        frac = str(ts).split(".")[-1]
        return f"{prefijo}-{dt.strftime('%Y%m%d-%H%M%S')}-{frac}"
    except Exception:
        return f"{prefijo}-{ts}"



def _registro_ya_guardado(sheet, registro_id):
    if not registro_id:
        return False
    objetivo = str(registro_id).strip()
    try:
        # _con_reintento: revisar duplicados es de lo más frecuente que hace el bot (se
        # llama antes de guardar CUALQUIER cobro) — vale la pena reintentar si Google
        # responde "cuota excedida" en ese momento puntual, en vez de asumir "no hay
        # duplicado" solo porque la lectura falló.
        valores = _con_reintento(lambda: sheet.get_all_values())
        if not valores:
            return False
        encabezados = [c.strip().lower() for c in valores[0]]
        if "id registro" in encabezados:
            col = encabezados.index("id registro")
            return any(len(fila) > col and str(fila[col]).strip() == objetivo for fila in valores[1:])
        return any(str(celda).strip() == objetivo for fila in valores for celda in fila)
    except Exception as e:
        print(f"⚠️ No se pudo verificar duplicado: {e}")
        return False



def _ya_procesado(texto):
    return texto.lstrip().startswith(("✅", "❌", "⚠"))


# Blindaje contra doble clic / dos personas aprobando (o rechazando) el mismo mensaje casi
# al mismo tiempo: _ya_procesado() de arriba no alcanza a atajarlo, porque los dos clics le
# llegan al bot con el texto del mensaje TODAVÍA sin marcar "APROBADO" (Slack no espera a que
# el primero termine para mandar el segundo). _reservar_mensaje() se usa ANTES de tocar el
# Sheet: el primer clic "reserva" el mensaje y sigue de largo; cualquier otro clic sobre el
# mismo mensaje mientras tanto se ignora, evitando que se guarde el mismo cobro dos veces.
_LOCK_APROBACION = threading.Lock()
_MENSAJES_EN_PROCESO = set()


def _reservar_mensaje(ts):
    """True si se pudo reservar este mensaje (nadie más lo está procesando); False si ya
    se estaba procesando (doble clic, o dos personas casi al mismo tiempo)."""
    with _LOCK_APROBACION:
        if ts in _MENSAJES_EN_PROCESO:
            return False
        _MENSAJES_EN_PROCESO.add(ts)
        return True
# ============ FIN BLINDAJE ANTI-DUPLICADOS ============


# ============ AVISO DE POSIBLE DUPLICADO (mismo cliente, misma semana) ============
# Antes de aprobar un cobro/domiciliación/etc., revisa si YA existe un registro de ese mismo
# cliente (por cédula, o por empresa en /domiciliar) con fecha dentro de la semana en curso.
# No bloquea el guardado — solo agrega un aviso al mensaje de aprobación y pide una
# confirmación extra (ventana emergente de Slack) antes de dejar aprobar, para que quien
# aprueba decida si es un cobro repetido de verdad o si son dos pagos distintos válidos.

def _normalizar_para_comparar(texto, modo="texto"):
    """Deja un valor listo para comparar si es 'el mismo cliente': para cédulas compara
    solo los dígitos (para que V-12.345.678, 12345678 y 12.345.678 se reconozcan igual);
    para texto (ej. nombre de empresa) compara sin tildes, en minúsculas y sin espacios de más."""
    if modo == "cedula":
        return _solo_digitos(texto)
    return _quitar_acentos(str(texto or "").strip().lower()).strip()


def _parsear_fecha_ddmmyyyy(texto):
    """Convierte 'DD/MM/AAAA' (o con '-') a un date de Python. Devuelve None si no se puede."""
    t = str(texto or "").strip()
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


def _inicio_semana_actual():
    """Lunes de la semana en curso (hora Venezuela), a medianoche."""
    hoy = datetime.now(ZoneInfo("America/Caracas")).date()
    return hoy - timedelta(days=hoy.weekday())


def _buscar_duplicado_reciente(sheet, columna_valor, columna_fecha, valor, modo="cedula"):
    """Revisa si YA existe una fila en 'sheet' con el mismo 'valor' (misma cédula o misma
    empresa, según 'modo') y fecha dentro de la semana en curso (lunes a hoy). Devuelve la
    fecha (texto, tal como está en el Sheet) del registro encontrado, o None si no hay
    ninguno o no se pudo revisar (nunca lanza error — si algo falla, simplemente no avisa)."""
    if not valor or sheet is None:
        return None
    try:
        col_valor = _columna_por_nombre(sheet, columna_valor)
        col_fecha = _columna_por_nombre(sheet, columna_fecha)
        if col_valor is None or col_fecha is None:
            return None
        objetivo = _normalizar_para_comparar(valor, modo)
        if not objetivo:
            return None
        lunes = _inicio_semana_actual()
        valores = _con_reintento(lambda: sheet.get_all_values())
        idx_valor, idx_fecha = col_valor - 1, col_fecha - 1
        for fila in valores[1:]:
            if len(fila) <= max(idx_valor, idx_fecha):
                continue
            if _normalizar_para_comparar(fila[idx_valor], modo) != objetivo:
                continue
            fecha_fila = _parsear_fecha_ddmmyyyy(fila[idx_fecha])
            if fecha_fila and fecha_fila >= lunes:
                return fila[idx_fecha].strip()
        return None
    except Exception as e:
        print(f"⚠️ No se pudo revisar duplicado reciente: {e}")
        return None
# ============ FIN AVISO DE POSIBLE DUPLICADO ============


# ============ GUARDAR EN SHEETS POR NOMBRE DE COLUMNA (Fase 2 - Sostenibilidad) ============

def _normalizar_encabezado(texto):
    """Deja un encabezado listo para comparar: minúsculas, sin tildes, sin espacios de sobra."""
    t = str(texto or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))



def _guardar_fila_por_encabezado(sheet, datos):
    """
    Guarda una fila nueva en 'sheet' colocando cada valor en la columna que le corresponde
    POR NOMBRE, no por posición. 'datos' es un diccionario {nombre_de_columna: valor}, en el
    orden en que se quiere que caigan los datos que no tengan columna en el Sheet.

    Compara los nombres de columna ignorando mayúsculas, tildes y espacios de más, para que un
    encabezado como "Conciliación" o "Conciliacion" (con o sin tilde) se reconozca igual.

    Si alguna clave de 'datos' no tiene una columna con ese nombre en el Sheet, su valor se
    agrega al final de la fila, siempre en el mismo orden, para no perder el dato ni desalinear
    las columnas que sí existen.
    """
    encabezados_sheet = _con_reintento(lambda: sheet.row_values(1))
    restantes = dict(datos)
    fila = []
    for encabezado in encabezados_sheet:
        objetivo = _normalizar_encabezado(encabezado)
        valor_encontrado = ""
        for clave in list(restantes.keys()):
            if _normalizar_encabezado(clave) == objetivo:
                valor_encontrado = restantes.pop(clave)
                break
        fila.append(valor_encontrado)
    # Cualquier dato que no tenía columna con ese nombre se agrega al final (siempre el mismo orden)
    fila.extend(restantes.values())
    # _con_reintento: este es el guardado real de un cobro/contacto/etc. — el paso más
    # importante de todos para reintentar si Google responde "cuota excedida" en ese
    # momento puntual, en vez de perder el registro por las puras.
    _con_reintento(lambda: sheet.append_row(fila))



def _columna_por_nombre(ws, nombre):
    """Ubica por NOMBRE (no por posición) el índice base-1 de una columna, comparando
    ignorando mayúsculas, tildes y espacios de más. Devuelve None si no la encuentra."""
    objetivo = _normalizar_encabezado(nombre)
    encabezados = [_normalizar_encabezado(c) for c in _con_reintento(lambda: ws.row_values(1))]
    if objetivo in encabezados:
        return encabezados.index(objetivo) + 1
    return None
# ============ FIN GUARDAR POR NOMBRE DE COLUMNA ============


# ============ REINTENTO ANTE CUOTA EXCEDIDA DE GOOGLE SHEETS (compartido) ============
# Antes esto solo vivía en reportes.py (para los reportes semanales/mensuales). Se movió
# aquí para que CUALQUIER parte del bot pueda usarlo — sobre todo los caminos "calientes"
# como guardar un cobro o revisar duplicados, que son los que más sufren cuando hay una
# ráfaga de actividad y Google empieza a responder '429 Quota exceeded'.
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
    de lecturas/escrituras pierda datos por las puras."""
    espera = espera_inicial
    for intento in range(intentos):
        try:
            return func()
        except Exception as e:
            if not _es_error_de_cuota(e) or intento == intentos - 1:
                raise
            print(f"⚠️ Google Sheets pidió esperar (cuota excedida), "
                  f"reintentando en {espera}s (intento {intento + 1}/{intentos})...")
            time.sleep(espera)
            espera *= 3
# ============ FIN REINTENTO ANTE CUOTA EXCEDIDA ============


# ============ LISTA DE COBRADORES (compartida) ============


# ============ VALIDACIÓN DE DATOS (estricta) ============
def _es_cedula_valida(texto):
    t = str(texto or "").strip().upper()
    if not t:
        return False, "La cédula está vacía."
    t2 = t.replace(".", "").replace("-", "").replace(" ", "")
    m = re.match(r"^([VEJPG]?)(\d+)$", t2)
    if not m:
        return False, "Cédula inválida. Usa números y opcional V/E/J/P (ej: V-12.345.678)."
    digitos = m.group(2)
    if not (6 <= len(digitos) <= 10):
        return False, f"La cédula debe tener entre 6 y 10 dígitos (tiene {len(digitos)})."
    return True, ""


def _es_texto_no_vacio(texto):
    """Para campos de texto libre que SÍ son obligatorios (ej. 'Nombre del Cliente') pero no
    tenían ningún validador — Slack exige que el campo no esté vacío, pero acepta un solo
    espacio en blanco como 'lleno', y eso se guarda en el Sheet como una celda que se ve
    vacía (así aparecieron varias filas de 'Contactados' con Nombre en blanco pero cédula,
    teléfono y cobrador sí completos). Este validador exige texto de verdad, no solo espacios."""
    t = str(texto or "").strip()
    if not t:
        return False, "Este campo no puede quedar vacío (o solo con espacios en blanco)."
    if len(t) < 2:
        return False, "Este campo parece incompleto (muy corto). Revisa que esté completo."
    return True, ""


def _es_telefono_valido(texto):
    t = str(texto or "").strip()
    if not t:
        return False, "El teléfono está vacío."
    limpio = re.sub(r"[()\-\s+]", "", t)
    if not limpio.isdigit():
        return False, "El teléfono solo debe tener números (ej: 0414-1234567)."
    n = len(limpio)
    if n not in (10, 11, 12):
        return False, f"El teléfono debe tener 10, 11 o 12 dígitos (tiene {n}). Ej: 04141234567."
    return True, ""


def _es_fecha_valida(texto):
    t = str(texto or "").strip()
    if not t:
        return False, "La fecha está vacía."
    partes = re.split(r"[/\-]", t)
    if len(partes) != 3:
        return False, "Fecha inválida. Usa el formato DD/MM/AAAA (ej: 25/12/2026)."
    try:
        d = int(partes[0]); mth = int(partes[1]); y = int(partes[2])
    except ValueError:
        return False, "La fecha debe tener solo números en formato DD/MM/AAAA."
    if y < 100:
        y += 2000
    try:
        date(y, mth, d)
    except ValueError:
        return False, "Esa fecha no existe. Revisa día/mes (formato DD/MM/AAAA)."
    if not (2024 <= y <= 2030):
        return False, "El año parece un error de tipeo. Usa DD/MM/AAAA (ej: 25/12/2026)."
    return True, ""


MONTO_MIN = 0.01           # un monto de 0 o negativo no es un cobro real
MONTO_MAX = 5_000_000_000  # tope de seguridad para atrapar tipeos absurdos (ej. ceros de más)


def _es_monto_valido(texto):
    """Valida un campo de monto en Bs (o USD): tiene que ser un número, mayor que 0, y
    dentro de un rango razonable (blinda contra tipeos como '5000000000' de más, o un
    número negativo por error)."""
    t = str(texto or "").strip()
    if not t:
        return False, "El monto está vacío."
    try:
        num = parse_numero(t)
    except (ValueError, ZeroDivisionError, TypeError):
        return False, "Ese monto no es un número válido. Ejemplo: 1500,50."
    if num < MONTO_MIN:
        return False, "El monto no puede ser cero ni negativo."
    if num > MONTO_MAX:
        return False, f"El monto *{num:,.2f}* parece un error de tipeo (demasiado alto). Revisa y vuelve a intentar."
    return True, ""


_VALIDADORES = {
    "cedula": _es_cedula_valida,
    "telefono": _es_telefono_valido,
    "fecha": _es_fecha_valida,
    "monto": _es_monto_valido,
    "requerido": _es_texto_no_vacio,
}


def _validar_view(valores, specs):
    """Devuelve un dict {block_id: mensaje} con los campos inválidos (vacío si todo OK)."""
    errores = {}
    for block_id, tipo in specs:
        try:
            valor = valores[block_id]["valor"]["value"]
        except (KeyError, TypeError):
            valor = ""
        ok, msg = _VALIDADORES[tipo](valor)
        if not ok:
            errores[block_id] = msg
    return errores
# ============ FIN VALIDACIÓN DE DATOS ============


# ============ MOTOR GENÉRICO DE FORMULARIOS (Fase 3 - Sostenibilidad) ============
# En vez de que cada comando repita ~150 líneas (abrir modal, validar, guardar, publicar),
# cada comando se describe como una "ficha" corta en FORM_SPECS: qué campos tiene el
# formulario, en qué Sheet se guarda, en qué canal se publica. El motor (las funciones de
# abajo) sabe leer cualquier ficha y hacer todo el trabajo. Así, agregar o ajustar un
# comando migrado a este patrón es cuestión de editar su ficha, no de tocar el motor.

def _solo_digitos(texto):
    return re.sub(r"\D", "", str(texto or ""))


def _quitar_acentos(texto):
    reemplazos = (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"))
    t = str(texto).lower()
    for a, b in reemplazos:
        t = t.replace(a, b)
    return t
