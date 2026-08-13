"""
main.py — Punto de entrada del bot. Importa config.py PRIMERO (crea la conexión a
Slack), luego cada módulo de comandos (cobros, mercadeo, promesas) para que registren
sus comandos/botones/formularios, y por último arranca el scheduler y la conexión de
Slack. Si agregas un módulo nuevo, IMPÓRTALO AQUÍ para que sus comandos funcionen.
"""
import os
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import app, SUPERVISOR_ID
import validaciones      # noqa: F401  (funciones de apoyo, sin comandos propios)
import motor_formularios  # noqa: F401  (motor genérico, sin comandos propios)
import cobros             # noqa: F401  (registra /cobro, /domiciliar, /conciliar, etc.)
import mercadeo           # noqa: F401  (registra /merca-reporte)
import promesas           # noqa: F401  (registra /mis-promesas, /promesa-cumplida, etc.)
import reportes           # noqa: F401  (registra /probar-reporte-semanal y /probar-reporte-mensual)
from scheduler import iniciar_scheduler


# ============ ALERTAS: aviso de arranque y errores graves (DM a Leandro) ============
def _avisar_arranque():
    """Manda un DM a Leandro cada vez que el bot arranca — sea por un deploy hecho a
    propósito, o porque Railway lo reinició solo tras una caída. Un aviso de arranque
    que nadie hizo a propósito es señal de que el bot se cayó y se reinició solo."""
    try:
        app.client.chat_postMessage(
            channel=SUPERVISOR_ID,
            text="🟢 *Robotín se acaba de iniciar* (deploy nuevo, o reinicio automático tras una caída)."
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar el aviso de arranque: {e}")


@app.error
def _manejar_error_global(error, body, logger):
    """Se dispara automáticamente cuando CUALQUIER comando, botón o formulario del bot
    lanza un error que nadie atrapó. Antes esto solo quedaba en los logs de Railway (que
    nadie revisa a diario) — ahora también llega un aviso por DM a Leandro."""
    logger.exception(f"Error no manejado en el bot: {error}")
    try:
        app.client.chat_postMessage(
            channel=SUPERVISOR_ID,
            text=f"🔴 *Robotín tuvo un error*\n```{error}```\nRevisa los logs de Railway para más detalle."
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar la alerta de error: {e}")
# ============ FIN ALERTAS ============


# ============ /ayuda: lista de comandos disponibles ============
@app.command("/ayuda")
def _mostrar_ayuda(ack, respond):
    """Muestra la lista de comandos del bot, solo visible para quien lo escribió (no se
    publica en el canal). Útil para que alguien nuevo en el equipo aprenda a usar el bot
    sin tener que preguntarle a Antonio."""
    ack()
    texto = (
        "*🤖 Comandos de Robotín — Guía rápida*\n\n"
        "*Registrar cobros y contactos (con aprobación):*\n"
        "• `/cobro` — Registrar un cobro\n"
        "• `/domiciliar` — Registrar una domiciliación\n"
        "• `/cobro-callcenter` — Cobro de Call Center Seguros\n"
        "• `/conciliar` — Conciliar un pago\n"
        "• `/liquidacion-nueva` — Registrar una liquidación nueva\n"
        "• `/liquidacion-estatus` — Actualizar el estatus de una liquidación\n"
        "• `/cobro-comercial` — Cobro del equipo Comercial\n"
        "• `/contacto-legal` — Contacto del equipo Legal\n\n"
        "*Registrar sin aprobación:*\n"
        "• `/contactar` — Registrar un contacto con el cliente\n"
        "• `/clientes-escalados` — Ver clientes escalados a Legal\n\n"
        "*Consultar información:*\n"
        "• `/buscar-cliente [cédula]` — Buscar el historial de un cliente\n"
        "• `/mis-promesas` — Ver tus promesas de pago pendientes, con botones para marcarlas\n\n"
        "*Promesas de pago:*\n"
        "• `/promesa-cumplida` — Marcar una promesa como cumplida\n"
        "• `/promesa-fallida` — Marcar una promesa como fallida\n\n"
        "*Tasa del día:*\n"
        "• `/tasa-hoy [valor]` — Fijar la tasa de cambio de hoy (ej: `/tasa-hoy 761.50`)\n\n"
        "*Mercadeo:*\n"
        "• `/merca-reporte` — Reportar conciliación de pago, incidencia técnica o problema de acceso\n"
        "• `/incidencia-fullcode` — Reportar una incidencia técnica con código completo\n\n"
        "*Utilidad:*\n"
        "• `/listar-ids` — Ver los IDs de canales y usuarios de Slack\n"
        "• `/probar-radar` — Probar manualmente el Radar de Promesas (4 PM)\n"
        "• `/probar-cierre` — Probar manualmente el Cierre Diario (6 PM)\n"
        "• `/probar-reporte-semanal` — Probar manualmente los reportes semanales\n"
        "• `/probar-reporte-mensual` — Probar manualmente los reportes mensuales\n"
        "• `/ayuda` — Ver esta lista de comandos\n\n"
        "_¿Algo no funciona como esperabas? Avísale a Antonio._"
    )
    respond(text=texto)
# ============ FIN /ayuda ============


if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    _avisar_arranque()
    iniciar_scheduler()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
