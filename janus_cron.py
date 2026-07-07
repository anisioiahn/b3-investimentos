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

        def executar_snapshot():
            """Salva snapshot diário após fechamento da B3."""
            hora_str = agora().strftime("%H:%M")
            hora_h = agora().hour
            hora_m = agora().minute
            # Só executa entre 17:30 e 17:35
            if not (hora_h == 17 and 30 <= hora_m <= 35):
                print(f"[SNAPSHOT CRON] ⏭️ {hora_str} — fora do horário, ignorando")
                return
            print(f"[SNAPSHOT CRON] 📸 {hora_str} — salvando snapshot de fechamento...")
            try:
                from servidor import salvar_snapshots_fechamento
                salvar_snapshots_fechamento()
            except Exception as e:
                print(f"[SNAPSHOT CRON] ❌ Erro: {e}", flush=True)

        scheduler.add_job(executar, "cron",
                          day_of_week="mon-fri",
                          hour=19, minute=0,
                          id="janus_coleta_fechamento")

        scheduler.add_job(executar, "cron",
                          day_of_week="mon-fri",
                          hour=10, minute=0,
                          id="janus_coleta_abertura")

        scheduler.add_job(executar_snapshot, "cron",
                          day_of_week="mon-fri",
                          hour=17, minute=30,
                          id="carteira_snapshot_fechamento")

        # Agenda do mercado — toda segunda-feira às 8h
        def executar_agenda():
            print("[AGENDA CRON] 📅 Coletando agenda do mercado...", flush=True)
            try:
                import subprocess, sys
                subprocess.Popen([sys.executable, "agenda_collector.py"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[AGENDA CRON] ❌ Erro: {e}", flush=True)

        scheduler.add_job(executar_agenda, "cron",
                          day_of_week="mon",
                          hour=8, minute=0,
                          id="agenda_semanal")

        # Histórico de preços — atualização diária às 18h
        def executar_historico_update():
            print("[HIST CRON] 📈 Atualizando histórico diário...", flush=True)
            try:
                import subprocess, sys
                subprocess.Popen([sys.executable, "historico_collector.py", "update"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[HIST CRON] ❌ Erro: {e}", flush=True)

        scheduler.add_job(executar_historico_update, "cron",
                          day_of_week="mon-fri",
                          hour=18, minute=0,
                          id="historico_update_diario")
        def executar_janus_noturno():
            print("[JANUS CRON] 🌙 Coleta noturna do Janus Index iniciando...", flush=True)
            from janus_routes import _janus_estado, _rodar_coleta
            if _janus_estado["rodando"]:
                print("[JANUS CRON] ⚠️ Coleta já em andamento, pulando...", flush=True)
                return
            threading.Thread(target=_rodar_coleta, daemon=True).start()

        scheduler.add_job(executar_janus_noturno, "cron",
                          day_of_week="mon-fri",
                          hour=2, minute=0,
                          id="janus_coleta_noturna")

        # Dividend Engine — sábado às 3h (semanal)
        def executar_dividendos_noturno():
            print("[DIVIDEND CRON] 🌙 Coleta semanal de dividendos iniciando...", flush=True)
            try:
                import subprocess, sys
                subprocess.Popen([sys.executable, "dividend_collector.py"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[DIVIDEND CRON] ❌ Erro: {e}", flush=True)

        scheduler.add_job(executar_dividendos_noturno, "cron",
                          day_of_week="sat",
                          hour=3, minute=0,
                          id="dividend_coleta_semanal")

        scheduler.start()
        print("[JANUS CRON] ✅ Agendamentos configurados:")
        print("  → Coleta abertura:      dias úteis às 10h BRT")
        print("  → Coleta fechamento:    dias úteis às 19h BRT")
        print("  → Coleta noturna:       dias úteis às 02h BRT")
        print("  → Snapshot carteira:    dias úteis às 17:30h BRT")
        print("  → Agenda do mercado:    segunda-feira às 08h BRT")
        print("  → Dividend Engine:      sábado às 03h BRT")
        return scheduler

    except ImportError:
        print("[JANUS CRON] ⚠️ APScheduler não instalado.")
        return None
    except Exception as e:
        print(f"[JANUS CRON] ❌ Erro ao iniciar cron: {e}")
        return None
