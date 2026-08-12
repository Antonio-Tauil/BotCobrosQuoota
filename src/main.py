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


if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    _avisar_arranque()
    iniciar_scheduler()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
