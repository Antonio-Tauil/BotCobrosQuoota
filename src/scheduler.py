"""
scheduler.py — Programa las tareas automáticas del bot (el Radar de Promesas a las
4:00 PM y el Cierre Diario de Cobros a las 6:00 PM, hora Venezuela). No define lógica
de negocio propia: solo conecta el reloj (BackgroundScheduler) con las funciones que
ya existen en promesas.py.
"""
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

from promesas import generar_resumen_promesas, generar_cierre_diario


def iniciar_scheduler():
    scheduler = BackgroundScheduler(timezone=ZoneInfo("America/Caracas"))
    scheduler.add_job(generar_resumen_promesas, "cron", hour=16, minute=0)
    scheduler.add_job(generar_cierre_diario, "cron", hour=18, minute=0)
    scheduler.start()
    print("⏰ Scheduler del Radar de Promesas activo (4:00 PM Venezuela).")
    return scheduler
