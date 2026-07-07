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
            print(f"[HIST] Brapi {ticker} {range_param}/{interval_param} → status {r.status_code}", flush=True)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    hist = results[0].get("historicalDataPrice", [])
                    print(f"[HIST] Brapi {ticker} → {len(hist)} pontos brutos", flush=True)
                    if hist:
                        print(f"[HIST] Exemplo ponto: {hist[0]}", flush=True)
                    return hist
                else:
                    print(f"[HIST] Brapi {ticker} → sem results no JSON", flush=True)
            elif r.status_code == 429:
                print(f"[HIST] Rate limit — aguardando 30s...", flush=True)
                time.sleep(30)
            else:
                print(f"[HIST] Erro HTTP {r.status_code} para {ticker}", flush=True)
        except Exception as e:
            print(f"[HIST] Erro tentativa {t+1} {ticker}: {e}", flush=True)
            time.sleep(2)
    return []

def buscar_yahoo(ticker, tentativas=3):
    """Busca 5 anos de histórico diário via Yahoo Finance. Ativos BR usam sufixo .SA"""
    try:
        import yfinance as yf
        # Tickers BR precisam de .SA exceto ^BVSP que vira ^BVSP no Yahoo
        yf_ticker = ticker if ticker.startswith('^') else f"{ticker}.SA"
        print(f"[HIST] Yahoo Finance: buscando {yf_ticker}...", flush=True)
        t = yf.Ticker(yf_ticker)
        hist = t.history(period="5y", interval="1d")
        if hist.empty:
            print(f"[HIST] Yahoo: {yf_ticker} sem dados", flush=True)
            return []
        # Converte para formato padrão
        registros = []
        for dt, row in hist.iterrows():
            try:
                ts = int(dt.timestamp())
                registros.append({
                    'date':   ts,
                    'open':   float(row['Open'])   if row['Open']   else None,
                    'high':   float(row['High'])   if row['High']   else None,
                    'low':    float(row['Low'])    if row['Low']    else None,
                    'close':  float(row['Close'])  if row['Close']  else None,
                    'volume': int(row['Volume'])   if row['Volume'] else None,
                })
            except: continue
        print(f"[HIST] Yahoo: {yf_ticker} → {len(registros)} pontos", flush=True)
        return registros
    except ImportError:
        print("[HIST] yfinance não instalado — rode: pip install yfinance", flush=True)
        return []
    except Exception as e:
        print(f"[HIST] Yahoo erro {ticker}: {e}", flush=True)
        return []
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

            # ── 5 anos DIÁRIO ──────────────────────────
            tem_5anos = False
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT MIN(data) FROM historico_precos
                    WHERE ticker=%s AND intervalo='1d'
                """, (ticker,))
                row = cur.fetchone()
                if row and row[0]:
                    anos = (hoje - row[0]).days / 365
                    tem_5anos = anos >= 4

            if tem_5anos:
                print(f"[HIST] {ticker} já tem 5 anos de histórico", flush=True)
            else:
                # 1️⃣ Tenta Brapi 1y (suportado)
                hist_5y = buscar_brapi(ticker, '1y', '1d')
                if hist_5y:
                    salvos = salvar_lote(conn, ticker, hist_5y, '1d')
                    total_salvos += salvos
                    print(f"[HIST] {ticker} 1y/brapi → {salvos} pts", flush=True)

                # 2️⃣ Yahoo Finance para completar os 5 anos
                hist_yahoo = buscar_yahoo(ticker)
                if hist_yahoo:
                    salvos = salvar_lote(conn, ticker, hist_yahoo, '1d')
                    total_salvos += salvos
                    print(f"[HIST] {ticker} 5y/yahoo → {salvos} pts", flush=True)
                elif not hist_5y:
                    print(f"[HIST] {ticker} ❌ sem dados em nenhuma fonte", flush=True)

                time.sleep(0.3)

            # ── 1 ano DIÁRIO ───────────────────────────
            ultimo_1d = ultimo_registro(conn, ticker, '1d')
            # Só pula se já tem dados diários E estão atualizados
            if ultimo_1d and (hoje - ultimo_1d).days <= 3:
                print(f"[HIST] {ticker} 1d já atualizado ({ultimo_1d})", flush=True)
            else:
                hist_1y = buscar_brapi(ticker, '1y', '1d')
                salvos = salvar_lote(conn, ticker, hist_1y, '1d')
                total_salvos += salvos
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
