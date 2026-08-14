"""
scheduler.py — Programa las tareas automáticas del bot: el Radar de Promesas a las
4:00 PM, el Cierre Diario de Cobros a las 6:00 PM, el Reporte Semanal de pagos los lunes
a las 8:00 AM, y el Reporte Mensual de pagos el día 1 de cada mes a las 8:30 AM (todo
hora Venezuela). El Reporte Mensual se dejó media hora después del Semanal a propósito:
si el día 1 de un mes cae lunes, ambos dispararían a la misma hora y competirían por la
misma cuota de lecturas de Google Sheets — con esta separación, nunca coinciden. No
define lógica de negocio propia: solo conecta el reloj (BackgroundScheduler) con las
funciones que ya existen en promesas.py y reportes.py.
"""
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

from promesas import generar_resumen_promesas, generar_cierre_diario
from reportes import generar_reportes_semanales, generar_reportes_mensuales, revisar_reportes_colgados


def iniciar_scheduler():
    scheduler = BackgroundScheduler(timezone=ZoneInfo("America/Caracas"))
    scheduler.add_job(generar_resumen_promesas, "cron", hour=16, minute=0)
    scheduler.add_job(generar_cierre_diario, "cron", hour=18, minute=0)
    scheduler.add_job(generar_reportes_semanales, "cron", day_of_week="mon", hour=8, minute=0)
    scheduler.add_job(generar_reportes_mensuales, "cron", day=1, hour=8, minute=30)
    # Vigilante de reportes colgados: no genera ningún reporte, solo revisa cada 20 minutos si
    # alguno de los 4 de arriba empezó y lleva más de 10 minutos sin terminar (algo anormal —
    # normalmente tardan segundos). No compite por cuota de Sheets: no abre ninguna hoja.
    scheduler.add_job(revisar_reportes_colgados, "interval", minutes=20)
    scheduler.start()
    print("⏰ Scheduler activo (Radar 4:00 PM, Cierre 6:00 PM, Reporte semanal lunes 8:00 AM, "
          "Reporte mensual día 1 8:30 AM, Vigilante de reportes colgados cada 20 min — hora Venezuela).")
    return scheduler
