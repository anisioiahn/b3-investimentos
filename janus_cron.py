# ============================================================
# JANUS INDEX – CRON JOB v1.1
# Respeita o flag _janus_rodando para não duplicar coletas
# ============================================================

import threading
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BRASILIA)

def iniciar_cron_janus():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        def executar():
            # Importa o wrapper com flag de controle (evita duplicatas)
            from janus_routes import _janus_rodando, run_collector_com_progresso
            hora = agora().strftime("%H:%M")
            if _janus_rodando:
                print(f"[JANUS CRON] ⚠️ {hora} — coleta já em andamento, pulando...")
                return
            print(f"[JANUS CRON] ⏰ {hora} — iniciando coleta agendada...")
            threading.Thread(target=run_collector_com_progresso, daemon=True).start()

        scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")

        scheduler.add_job(executar, "cron",
                          day_of_week="mon-fri",
                          hour=19, minute=0,
                          id="janus_coleta_fechamento")

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
        return None
    except Exception as e:
        print(f"[JANUS CRON] ❌ Erro ao iniciar cron: {e}")
        return None
