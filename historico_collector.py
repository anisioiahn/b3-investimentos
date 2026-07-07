# ============================================================
# JANUS HISTÓRICO COLLECTOR v2.0
# Importa 5 anos de histórico mensal + 1 ano diário
# para todos os ativos. Incremental — só busca o que falta.
# ============================================================

import os, time, sys, requests
from datetime import date, timedelta
import psycopg2, psycopg2.extras

TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

def buscar_brapi(ticker, range_param, interval_param, tentativas=3):
    url = f"{BRAPI_BASE}/quote/{ticker}?range={range_param}&interval={interval_param}&token={TOKEN_BRAPI}"
    for t in range(tentativas):
        try:
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    return results[0].get("historicalDataPrice", [])
            elif r.status_code == 429:
                print(f"[HIST] Rate limit — aguardando 30s...", flush=True)
                time.sleep(30)
        except Exception as e:
            print(f"[HIST] Erro tentativa {t+1}: {e}", flush=True)
            time.sleep(2)
    return []

def salvar_lote(conn, ticker, registros, intervalo):
    if not registros: return 0
    from datetime import datetime
    args = []
    for r in registros:
        try:
            dt = datetime.utcfromtimestamp(r['date']).date() if isinstance(r.get('date'), (int,float)) else None
            if not dt or not r.get('close'): continue
            args.append((ticker, dt, intervalo,
                r.get('open'), r.get('high'), r.get('low'),
                r.get('close'), r.get('volume')))
        except: continue
    if not args: return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO historico_precos
                (ticker, data, intervalo, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (ticker, data, intervalo) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high,
                low=EXCLUDED.low,   close=EXCLUDED.close,
                volume=EXCLUDED.volume
        """, args, template="(%s,%s,%s,%s,%s,%s,%s,%s)")
    conn.commit()
    return len(args)

def ultimo_registro(conn, ticker, intervalo):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(data) FROM historico_precos WHERE ticker=%s AND intervalo=%s",
                    (ticker, intervalo))
        row = cur.fetchone()
    return row[0] if row else None

def run_historico_collector(modo='full', on_progress=None):
    def prog(pct, msg):
        print(f"[HIST] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, msg)
            except: pass

    print(f"[HIST] 📈 Histórico Collector v2.0 — modo: {modo}", flush=True)
    conn = get_conn()
    hoje = date.today()

    try:
        # Inicializa tabela
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS historico_precos (
                    ticker TEXT NOT NULL, data DATE NOT NULL,
                    open NUMERIC, high NUMERIC, low NUMERIC,
                    close NUMERIC NOT NULL, volume BIGINT,
                    intervalo TEXT NOT NULL DEFAULT '1d',
                    PRIMARY KEY (ticker, data, intervalo)
                );
                CREATE INDEX IF NOT EXISTS idx_hist_ticker_data
                    ON historico_precos(ticker, intervalo, data DESC);
            """)
        conn.commit()

        # Lista de ativos
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if modo == 'carteira':
                cur.execute("""
                    SELECT DISTINCT c.ticker FROM carteira c
                    WHERE c.status = 'confirmada' ORDER BY c.ticker
                """)
            else:
                cur.execute("""
                    SELECT DISTINCT ticker FROM assets
                    WHERE status = 'ATIVO'
                    AND asset_type IN ('ACAO','FII','ETF','BDR')
                    ORDER BY ticker
                """)
            tickers = [r['ticker'] for r in cur.fetchall()]

        # Sempre inclui IBOVESPA
        if '^BVSP' not in tickers:
            tickers.insert(0, '^BVSP')

        total = len(tickers)
        total_salvos = 0
        prog(0, f"{total} ativos para processar")

        for i, ticker in enumerate(tickers):
            pct = round(i / total * 100)
            if i % 5 == 0:
                prog(pct, f"{i+1}/{total} — {ticker}")

            # ── 5 anos MENSAL ──────────────────────────
            ultimo_mo = ultimo_registro(conn, ticker, '1mo')
            # Importa se não tem ou está desatualizado há mais de 30 dias
            if not ultimo_mo or (hoje - ultimo_mo).days > 30:
                hist_5y = buscar_brapi(ticker, '5y', '1mo')
                salvos = salvar_lote(conn, ticker, hist_5y, '1mo')
                total_salvos += salvos
                if salvos:
                    print(f"[HIST] {ticker} 5y/1mo → {salvos} pts salvos", flush=True)
                time.sleep(0.5)

            # ── 1 ano DIÁRIO ───────────────────────────
            ultimo_1d = ultimo_registro(conn, ticker, '1d')
            # Importa se não tem ou está desatualizado há mais de 1 dia
            if not ultimo_1d or (hoje - ultimo_1d).days > 1:
                hist_1y = buscar_brapi(ticker, '1y', '1d')
                salvos = salvar_lote(conn, ticker, hist_1y, '1d')
                total_salvos += salvos
                if salvos:
                    print(f"[HIST] {ticker} 1y/1d → {salvos} pts salvos", flush=True)
                time.sleep(0.5)

        prog(100, f"Concluído! {total_salvos} registros salvos de {total} ativos")

    except Exception as e:
        print(f"[HIST] ❌ Erro fatal: {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else 'full'
    run_historico_collector(modo=modo)
