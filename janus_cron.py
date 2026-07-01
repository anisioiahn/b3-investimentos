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
            from janus_routes import _janus_estado, _rodar_coleta
            hora = agora()
            hora_str = hora.strftime("%H:%M")
            hora_h = hora.hour
            hora_m = hora.minute

            # Só executa se estiver dentro de 5 minutos do horário agendado
            # Evita disparar no boot do servidor
            horarios_validos = [(10, 0), (19, 0)]
            valido = any(
                hora_h == h and hora_m <= 5
                for h, m in horarios_validos
            )
            if not valido:
                print(f"[JANUS CRON] ⏭️ {hora_str} — fora do horário de coleta, ignorando")
                return

            if _janus_estado["rodando"]:
                print(f"[JANUS CRON] ⚠️ {hora_str} — coleta já em andamento, pulando...")
                return

            print(f"[JANUS CRON] ⏰ {hora_str} — iniciando coleta agendada...")
            threading.Thread(target=_rodar_coleta, daemon=True).start()

        scheduler = BackgroundScheduler(
            timezone="America/Sao_Paulo",
            job_defaults={
                'misfire_grace_time': 60,  # descarta se atrasado mais de 60s (evita disparo no boot)
                'coalesce': True,          # agrupa múltiplos disparos em um só
                'max_instances': 1         # nunca mais de 1 instância simultânea
            }
        )

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
