# ============================================================
# JANUS HISTÓRICO COLLECTOR v1.0
# Importa 5 anos de histórico de preços para o banco local
# Estratégia:
#   - 5 anos mensal (1mo) para todos os ativos → visão longa
#   - 1 ano diário (1d) para ativos da carteira → precisão
#   - Incremental: só busca o que falta desde último registro
# ============================================================

import os, time, requests, sys
from datetime import datetime, timezone, timedelta, date
import psycopg2, psycopg2.extras

TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"
TZ_BR = timezone(timedelta(hours=-3))

def agora(): return datetime.now(TZ_BR)
def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

def buscar_historico_brapi(ticker, range_param, interval_param, tentativas=3):
    """Busca histórico de um ticker na Brapi com retry."""
    url = f"{BRAPI_BASE}/quote/{ticker}?range={range_param}&interval={interval_param}&token={TOKEN_BRAPI}"
    for t in range(tentativas):
        try:
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    return results[0].get("historicalDataPrice", [])
            elif r.status_code == 429:
                print(f"[HIST] ⏳ Rate limit, aguardando 30s...", flush=True)
                time.sleep(30)
            else:
                time.sleep(1)
        except Exception as e:
            print(f"[HIST] Tentativa {t+1} falhou: {e}", flush=True)
            time.sleep(2)
    return []

def run_historico_collector(modo='full', tickers_extra=None, on_progress=None):
    """
    Modos:
      'full'   — importa 5y/1mo para todos os ativos (carga inicial)
      'update' — atualiza apenas os dias faltantes (cron diário)
      'carteira' — importa 1y/1d para ativos da carteira
    """
    def prog(pct, msg):
        print(f"[HIST] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, msg)
            except: pass

    print(f"[HIST] 📈 Histórico Collector v1.0 — modo: {modo}", flush=True)
    conn = get_conn()

    try:
        import db as janus_db
        janus_db.db_init_historico_table(conn)

        # Busca lista de ativos
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if modo == 'carteira':
                cur.execute("""
                    SELECT DISTINCT c.ticker FROM carteira c
                    WHERE c.status = 'confirmada'
                    ORDER BY c.ticker
                """)
            else:
                cur.execute("""
                    SELECT DISTINCT ticker FROM assets
                    WHERE status = 'ATIVO' AND asset_type IN ('ACAO','FII','ETF','BDR')
                    ORDER BY ticker
                """)
            tickers = [r['ticker'] for r in cur.fetchall()]

        # Adiciona tickers extras se fornecidos
        if tickers_extra:
            for t in tickers_extra:
                if t not in tickers:
                    tickers.append(t)

        total = len(tickers)
        prog(0, f"Iniciando — {total} ativos para processar")

        total_registros = 0
        erros = 0
        DELAY = 0.3

        for i, ticker in enumerate(tickers):
            pct = round(i / total * 100)
            if i % 10 == 0:
                prog(pct, f"{i}/{total} — {ticker}")

            try:
                if modo == 'full':
                    # Verifica se já tem dados mensais
                    ultimo = janus_db.db_ultimo_historico(ticker, '1mo')
                    hoje = date.today()

                    if ultimo and (hoje - ultimo).days < 30:
                        continue  # já está atualizado

                    # Busca 5 anos mensal
                    time.sleep(DELAY)
                    hist_5y = buscar_historico_brapi(ticker, '5y', '1mo')
                    if hist_5y:
                        salvos = janus_db.db_salvar_historico_lote(conn, ticker, hist_5y, '1mo')
                        total_registros += salvos

                elif modo == 'update':
                    # Só busca o que falta desde o último registro diário
                    ultimo = janus_db.db_ultimo_historico(ticker, '1d')
                    hoje = date.today()

                    if ultimo and (hoje - ultimo).days <= 1:
                        continue  # já está atualizado

                    # Busca apenas 1 mês (captura dias faltantes)
                    time.sleep(DELAY)
                    hist_1m = buscar_historico_brapi(ticker, '1mo', '1d')
                    if hist_1m:
                        salvos = janus_db.db_salvar_historico_lote(conn, ticker, hist_1m, '1d')
                        total_registros += salvos

                elif modo == 'carteira':
                    # 1 ano diário para ativos da carteira
                    ultimo = janus_db.db_ultimo_historico(ticker, '1d')
                    hoje = date.today()

                    if ultimo and (hoje - ultimo).days <= 1:
                        continue

                    time.sleep(DELAY)
                    hist_1y = buscar_historico_brapi(ticker, '1y', '1d')
                    if hist_1y:
                        salvos = janus_db.db_salvar_historico_lote(conn, ticker, hist_1y, '1d')
                        total_registros += salvos

                    # Também importa 5y mensal se não tiver
                    ultimo_mo = janus_db.db_ultimo_historico(ticker, '1mo')
                    if not ultimo_mo:
                        time.sleep(DELAY)
                        hist_5y = buscar_historico_brapi(ticker, '5y', '1mo')
                        if hist_5y:
                            salvos = janus_db.db_salvar_historico_lote(conn, ticker, hist_5y, '1mo')
                            total_registros += salvos

            except Exception as e:
                print(f"[HIST] ❌ Erro {ticker}: {e}", flush=True)
                erros += 1

        prog(100, f"Concluído! {total_registros} registros salvos ({erros} erros)")
        print(f"[HIST] ✅ Total no banco: {janus_db.db_total_historico()} registros", flush=True)

    except Exception as e:
        print(f"[HIST] ❌ Erro fatal: {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else 'full'
    run_historico_collector(modo=modo)
