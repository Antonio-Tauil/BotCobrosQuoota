"""
scheduler.py — Programa las tareas automáticas del bot: el Radar de Promesas a las
4:00 PM, el Cierre Diario de Cobros a las 6:00 PM, y el Reporte Semanal de pagos los
lunes a las 8:00 AM (todo hora Venezuela). No define lógica de negocio propia: solo
conecta el reloj (BackgroundScheduler) con las funciones que ya existen en promesas.py
y reportes.py.
"""
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

from promesas import generar_resumen_promesas, generar_cierre_diario
from reportes import generar_reportes_semanales


def iniciar_scheduler():
    scheduler = BackgroundScheduler(timezone=ZoneInfo("America/Caracas"))
    scheduler.add_job(generar_resumen_promesas, "cron", hour=16, minute=0)
    scheduler.add_job(generar_cierre_diario, "cron", hour=18, minute=0)
    scheduler.add_job(generar_reportes_semanales, "cron", day_of_week="mon", hour=8, minute=0)
    scheduler.start()
    print("⏰ Scheduler activo (Radar 4:00 PM, Cierre 6:00 PM, Reporte semanal lunes 8:00 AM — hora Venezuela).")
    return scheduler
