# ============================================================
# JANUS INDEX – CRON JOB v1.0
# Usa APScheduler — mesmo padrão de threading do servidor.py
# Adicione ao requirements.txt: APScheduler>=3.10.0
# ============================================================

import threading
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BRASILIA)

def iniciar_cron_janus():
    """
    Inicia o agendador do Janus Index.
    Chame no servidor.py assim:
        from janus_cron import iniciar_cron_janus
        iniciar_cron_janus()
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from janus_collector import run_collector

        def executar():
            hora = agora().strftime("%H:%M")
            print(f"[JANUS CRON] ⏰ {hora} — iniciando coleta agendada...")
            threading.Thread(target=run_collector, daemon=True).start()

        scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")

        # Coleta principal: dias úteis às 19h (após fechamento da B3)
        scheduler.add_job(executar, "cron",
                          day_of_week="mon-fri",
                          hour=19, minute=0,
                          id="janus_coleta_fechamento")

        # Coleta de abertura: dias úteis às 10h (dados do dia anterior consolidados)
        scheduler.add_job(executar, "cron",
                          day_of_week="mon-fri",
                          hour=10, minute=0,
                          id="janus_coleta_abertura")

        scheduler.start()

        print("[JANUS CRON] ✅ Agendamentos configurados:")
        print("  → Coleta fechamento: dias úteis às 19h BRT")
        print("  → Coleta abertura:   dias úteis às 10h BRT")

        return scheduler

    except ImportError:
        print("[JANUS CRON] ⚠️ APScheduler não instalado.")
        print("             Adicione ao requirements.txt: APScheduler>=3.10.0")
        return None
    except Exception as e:
        print(f"[JANUS CRON] ❌ Erro ao iniciar cron: {e}")
        return None
