"""
main.py — Punto de entrada del bot. Importa config.py PRIMERO (crea la conexión a
Slack), luego cada módulo de comandos (cobros, mercadeo, promesas) para que registren
sus comandos/botones/formularios, y por último arranca el scheduler y la conexión de
Slack. Si agregas un módulo nuevo, IMPÓRTALO AQUÍ para que sus comandos funcionen.
"""
import os
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import app
import validaciones      # noqa: F401  (funciones de apoyo, sin comandos propios)
import motor_formularios  # noqa: F401  (motor genérico, sin comandos propios)
import cobros             # noqa: F401  (registra /cobro, /domiciliar, /conciliar, etc.)
import mercadeo           # noqa: F401  (registra /merca-reporte)
import promesas           # noqa: F401  (registra /mis-promesas, /promesa-cumplida, etc.)
from scheduler import iniciar_scheduler

if __name__ == "__main__":
    print("🤖 Robotín está despierto y conectándose a Slack...")
    iniciar_scheduler()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
