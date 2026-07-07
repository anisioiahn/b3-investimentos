# ============================================================
# JANUS HISTÓRICO COLLECTOR v2.1
# Importa 5 anos de histórico via Yahoo Finance + Brapi
# ============================================================

import os, time, sys, requests
from datetime import date
import psycopg2, psycopg2.extras

TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

def buscar_brapi(ticker, range_param, interval_param):
    url = f"{BRAPI_BASE}/quote/{ticker}?range={range_param}&interval={interval_param}&token={TOKEN_BRAPI}"
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
        print(f"[HIST] Brapi {ticker} {range_param} → {r.status_code}", flush=True)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                hist = results[0].get("historicalDataPrice", [])
                print(f"[HIST] Brapi {ticker} → {len(hist)} pts", flush=True)
                return hist
    except Exception as e:
        print(f"[HIST] Brapi erro {ticker}: {e}", flush=True)
    return []

def buscar_yahoo(ticker):
    """Busca 5 anos via Yahoo Finance. Ativos BR usam sufixo .SA"""
    try:
        import yfinance as yf
        yf_ticker = ticker if ticker.startswith('^') else f"{ticker}.SA"
        print(f"[HIST] Yahoo {yf_ticker}...", flush=True)
        hist = yf.Ticker(yf_ticker).history(period="5y", interval="1d")
        if hist.empty:
            print(f"[HIST] Yahoo {yf_ticker} sem dados", flush=True)
            return []
        registros = []
        for dt, row in hist.iterrows():
            try:
                registros.append({
                    'date':   int(dt.timestamp()),
                    'open':   float(row['Open'])   if row['Open']   else None,
                    'high':   float(row['High'])   if row['High']   else None,
                    'low':    float(row['Low'])    if row['Low']    else None,
                    'close':  float(row['Close'])  if row['Close']  else None,
                    'volume': int(row['Volume'])   if row['Volume'] else None,
                })
            except: continue
        print(f"[HIST] Yahoo {yf_ticker} → {len(registros)} pts", flush=True)
        return registros
    except ImportError:
        print("[HIST] yfinance não instalado", flush=True)
        return []
    except Exception as e:
        print(f"[HIST] Yahoo erro {ticker}: {e}", flush=True)
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
                low=EXCLUDED.low, close=EXCLUDED.close,
                volume=EXCLUDED.volume
        """, args, template="(%s,%s,%s,%s,%s,%s,%s,%s)")
    conn.commit()
    return len(args)

def ultimo_registro(conn, ticker, intervalo):
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(data), MAX(data) FROM historico_precos WHERE ticker=%s AND intervalo=%s",
                    (ticker, intervalo))
        row = cur.fetchone()
    return row if row else (None, None)

def run_historico_collector(modo='full', on_progress=None):
    def prog(pct, msg):
        print(f"[HIST] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, msg)
            except: pass

    print(f"[HIST] 📈 Histórico Collector v2.1 — modo: {modo}", flush=True)
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

        if '^BVSP' not in tickers:
            tickers.insert(0, '^BVSP')

        total = len(tickers)
        total_salvos = 0
        prog(0, f"{total} ativos para processar")

        for i, ticker in enumerate(tickers):
            pct = round(i / total * 100)
            if i % 5 == 0:
                prog(pct, f"{i+1}/{total} — {ticker}")

            # Verifica se já tem 5 anos
            data_min, data_max = ultimo_registro(conn, ticker, '1d')
            tem_5anos = data_min and (hoje - data_min).days >= 4*365

            if tem_5anos:
                # Só atualiza dias recentes via Brapi
                if not data_max or (hoje - data_max).days > 1:
                    hist = buscar_brapi(ticker, '1mo', '1d')
                    salvos = salvar_lote(conn, ticker, hist, '1d')
                    total_salvos += salvos
                    if salvos: print(f"[HIST] {ticker} update → {salvos} pts", flush=True)
                else:
                    print(f"[HIST] {ticker} ✅ atualizado ({data_min} → {data_max})", flush=True)
            else:
                # Carga completa: Yahoo Finance 5 anos
                hist_yahoo = buscar_yahoo(ticker)
                if hist_yahoo:
                    salvos = salvar_lote(conn, ticker, hist_yahoo, '1d')
                    total_salvos += salvos
                    print(f"[HIST] {ticker} yahoo → {salvos} pts salvos", flush=True)
                else:
                    # Fallback: Brapi 1 ano
                    hist_brapi = buscar_brapi(ticker, '1y', '1d')
                    salvos = salvar_lote(conn, ticker, hist_brapi, '1d')
                    total_salvos += salvos
                    if salvos: print(f"[HIST] {ticker} brapi 1y → {salvos} pts", flush=True)

            time.sleep(0.3)

        prog(100, f"Concluído! {total_salvos} registros salvos de {total} ativos")

    except Exception as e:
        print(f"[HIST] ❌ Erro fatal: {e}", flush=True)
        import traceback; traceback.print_exc()
    finally:
        conn.close()

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else 'full'
    run_historico_collector(modo=modo)
