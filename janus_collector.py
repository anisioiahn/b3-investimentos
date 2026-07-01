# ============================================================
# JANUS INDEX – DATA COLLECTOR v1.4
# - Conexão única reutilizada
# - Lotes de 10 tickers na Brapi
# - Progresso via callback opcional
# ============================================================

import os, time, requests
import psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta

TOKEN_BRAPI   = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE    = "https://brapi.dev/api"
BRAPI_MODULES = "defaultKeyStatistics,balanceSheetHistory,incomeStatementHistory,summaryProfile"
DELAY_MS      = 0.5
LOTE_BRAPI    = 10

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora():     return datetime.now(TZ_BRASILIA)
def agora_str(): return agora().isoformat()
def hoje():      return agora().strftime("%Y-%m-%d")

def safe_num(v):
    try:    return float(v) if v is not None else None
    except: return None

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── Fonte de dados ─────────────────────────────────────────
def get_source_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source_id FROM data_sources WHERE source_name = 'Brapi'")
        row = cur.fetchone()
        if row: return row[0]
        cur.execute("""
            INSERT INTO data_sources (source_name, source_type, url, reliability, is_primary)
            VALUES ('Brapi', 'API', 'https://brapi.dev', 95.0, TRUE)
            RETURNING source_id
        """)
        return cur.fetchone()[0]

def iniciar_log(conn, source_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO data_ingestion_logs
                (source_id, job_name, started_at, status, records_processed)
            VALUES (%s, 'janus-data-collector', %s, 'RUNNING', 0)
            RETURNING ingestion_log_id
        """, (source_id, agora_str()))
        return cur.fetchone()[0]

def finalizar_log(conn, log_id, status, records, error_msg=None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE data_ingestion_logs
            SET finished_at=%s, status=%s, records_processed=%s, error_message=%s
            WHERE ingestion_log_id=%s
        """, (agora_str(), status, records, error_msg, log_id))

# ── Lista de ativos ────────────────────────────────────────
def buscar_lista_ativos():
    print("[COLLECTOR] 📋 Buscando lista de ativos da B3...", flush=True)
    todos, pagina, limite = [], 1, 500
    while True:
        try:
            url = f"{BRAPI_BASE}/quote/list?token={TOKEN_BRAPI}&type=stock&limit={limite}&page={pagina}&sortBy=volume&sortOrder=desc"
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                stocks = r.json().get("stocks", [])
                if not stocks: break
                todos.extend(stocks)
                print(f"[COLLECTOR] 📄 Página {pagina}: {len(stocks)} ativos (total: {len(todos)})", flush=True)
                if len(stocks) < limite: break
                pagina += 1
                time.sleep(0.5)
            elif r.status_code == 429:
                print("[COLLECTOR] ⏳ Rate limit, aguardando 30s...", flush=True)
                time.sleep(30)
            else:
                print(f"[COLLECTOR] ⚠️ Erro na lista: {r.status_code}", flush=True)
                break
        except Exception as e:
            print(f"[COLLECTOR] ❌ Erro página {pagina}: {e}", flush=True)
            break
    print(f"[COLLECTOR] ✅ {len(todos)} ativos encontrados", flush=True)
    return todos

# ── Upsert em batch ────────────────────────────────────────
def upsert_assets_batch(conn, lista):
    asset_map = {}
    with conn.cursor() as cur:
        for stock in lista:
            ticker = stock.get("stock") or stock.get("symbol")
            if not ticker: continue
            nome  = stock.get("name", ticker)
            setor = stock.get("sector")
            try:
                cur.execute("""
                    INSERT INTO companies (corporate_name, trading_name, sector, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (trading_name) DO UPDATE SET
                        sector=EXCLUDED.sector, updated_at=EXCLUDED.updated_at
                    RETURNING company_id
                """, (nome, nome, setor, agora_str()))
                company_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO assets (ticker, company_id, asset_type, currency, country, status, updated_at)
                    VALUES (%s, %s, 'ACAO', 'BRL', 'BR', 'ATIVO', %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        company_id=EXCLUDED.company_id, updated_at=EXCLUDED.updated_at
                    RETURNING asset_id
                """, (ticker, company_id, agora_str()))
                asset_map[ticker] = cur.fetchone()[0]
            except Exception as e:
                print(f"[COLLECTOR] ⚠️ Upsert {ticker}: {e}", flush=True)
    conn.commit()
    print(f"[COLLECTOR] 💾 {len(asset_map)} ativos registrados", flush=True)
    return asset_map

# ── Busca dados fundamentalistas ───────────────────────────
def buscar_dados_lote(tickers):
    joined = ",".join(tickers)
    url = f"{BRAPI_BASE}/quote/{joined}?modules={BRAPI_MODULES}&token={TOKEN_BRAPI}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            return {d["symbol"]: d for d in r.json().get("results", []) if d.get("symbol")}
        elif r.status_code == 429:
            print("[COLLECTOR] ⏳ Rate limit, aguardando 30s...", flush=True)
            time.sleep(30)
            r2 = requests.get(url, headers=headers, timeout=30)
            if r2.status_code == 200:
                return {d["symbol"]: d for d in r2.json().get("results", []) if d.get("symbol")}
        else:
            print(f"[COLLECTOR] ⚠️ Status {r.status_code} lote {joined[:50]}", flush=True)
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro lote: {e}", flush=True)
    return {}

# ── Indicadores e score ────────────────────────────────────
def calcular_indicadores(dados):
    ks       = dados.get("defaultKeyStatistics") or {}
    inc_hist = dados.get("incomeStatementHistory") or []
    net_margin  = safe_num(ks.get("profitMargins"))
    net_income  = safe_num(ks.get("netIncomeToCommon"))
    book_value  = safe_num(ks.get("bookValue"))
    shares      = safe_num(ks.get("sharesOutstanding"))
    roe = (net_income / (book_value * shares)) if (net_income and book_value and shares) else None
    rev_growth = None
    if len(inc_hist) >= 2:
        r0 = safe_num(inc_hist[0].get("totalRevenue"))
        r1 = safe_num(inc_hist[1].get("totalRevenue"))
        if r0 is not None and r1:
            rev_growth = (r0 - r1) / r1
    return {
        "FIN_ROE":            roe,
        "FIN_ROIC":           roe,
        "FIN_NET_MARGIN":     net_margin,
        "FIN_REVENUE_GROWTH": rev_growth,
        "FIN_REVENUE":        safe_num((inc_hist[0] if inc_hist else {}).get("totalRevenue")),
        "VAL_PE":             safe_num(ks.get("trailingPE")),
        "VAL_PVP":            safe_num(ks.get("priceToBook")),
        "VAL_EV_EBITDA":      safe_num(ks.get("enterpriseToEbitda")),
        "VAL_DIVIDEND_YIELD": safe_num(ks.get("dividendYield")),
    }

BENCHMARKS = {
    "FIN_ROE":            {"min": -0.10, "max": 0.40},
    "FIN_ROIC":           {"min": -0.05, "max": 0.35},
    "FIN_NET_MARGIN":     {"min": -0.10, "max": 0.30},
    "FIN_REVENUE_GROWTH": {"min": -0.20, "max": 0.50},
}
PESOS = {"FIN_ROE": 0.30, "FIN_ROIC": 0.25, "FIN_NET_MARGIN": 0.25, "FIN_REVENUE_GROWTH": 0.20}

def calcular_score(ind_map):
    score_total = peso_total = 0.0
    for code, peso in PESOS.items():
        valor = ind_map.get(code)
        if valor is None: continue
        b = BENCHMARKS[code]
        norm = max(0.0, min(100.0, (valor - b["min"]) / (b["max"] - b["min"]) * 100))
        score_total += norm * peso
        peso_total  += peso
    if peso_total == 0: return None, None
    return round(score_total / peso_total, 2), round((peso_total / 1.0) * 100, 2)

def classificar(score):
    if score >= 80: return "Muito Favorável"
    if score >= 60: return "Favorável"
    if score >= 40: return "Neutro"
    if score >= 20: return "Desfavorável"
    return "Muito Desfavorável"

# ── Salvar lote no banco ───────────────────────────────────
def salvar_lote_banco(conn, lote_dados, source_id):
    rankings_lote = []
    ref = hoje()
    with conn.cursor() as cur:
        for asset_id, ticker, dados in lote_dados:
            try:
                ks       = dados.get("defaultKeyStatistics") or {}
                inc_hist = dados.get("incomeStatementHistory") or []
                bal_hist = dados.get("balanceSheetHistory") or []
                inc = inc_hist[0] if inc_hist else {}
                bal = bal_hist[0] if bal_hist else {}

                cur.execute("""
                    INSERT INTO market_snapshots
                        (asset_id, reference_date, open_price, high_price, low_price,
                         close_price, last_price, volume, market_cap, beta, source_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (asset_id, reference_date) DO UPDATE SET
                        last_price=EXCLUDED.last_price, volume=EXCLUDED.volume,
                        market_cap=EXCLUDED.market_cap
                """, (asset_id, ref,
                    safe_num(dados.get("regularMarketOpen")),
                    safe_num(dados.get("regularMarketDayHigh")),
                    safe_num(dados.get("regularMarketDayLow")),
                    safe_num(dados.get("regularMarketPrice")),
                    safe_num(dados.get("regularMarketPrice")),
                    safe_num(dados.get("regularMarketVolume")),
                    safe_num(dados.get("marketCap")),
                    safe_num(ks.get("beta")), source_id))

                cur.execute("""
                    INSERT INTO financial_snapshots
                        (asset_id, reference_date, period_type, revenue, gross_profit,
                         ebitda, net_income, equity, total_assets, total_debt,
                         cash_and_equivalents, operating_cash_flow, free_cash_flow,
                         capex, source_id, data_version)
                    VALUES (%s,%s,'ANUAL',%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,'1.0')
                    ON CONFLICT (asset_id, reference_date, period_type) DO UPDATE SET
                        revenue=EXCLUDED.revenue, net_income=EXCLUDED.net_income
                """, (asset_id, ref,
                    safe_num(inc.get("totalRevenue")),
                    safe_num(inc.get("grossProfit")),
                    safe_num(inc.get("cleanEbitda") or inc.get("ebit")),
                    safe_num(inc.get("netIncome")),
                    safe_num(bal.get("totalStockholderEquity")),
                    safe_num(bal.get("totalAssets")),
                    safe_num(bal.get("totalLiab") or bal.get("totalDebt")),
                    safe_num(bal.get("cash")), source_id))

                ind_map = calcular_indicadores(dados)
                for code, value, unit in [
                    ("FIN_ROE",            ind_map.get("FIN_ROE"),            "%"),
                    ("FIN_ROIC",           ind_map.get("FIN_ROIC"),           "%"),
                    ("FIN_NET_MARGIN",     ind_map.get("FIN_NET_MARGIN"),     "%"),
                    ("FIN_REVENUE",        ind_map.get("FIN_REVENUE"),        "R$"),
                    ("FIN_REVENUE_GROWTH", ind_map.get("FIN_REVENUE_GROWTH"), "%"),
                    ("VAL_PE",             ind_map.get("VAL_PE"),             "x"),
                    ("VAL_PVP",            ind_map.get("VAL_PVP"),            "x"),
                    ("VAL_EV_EBITDA",      ind_map.get("VAL_EV_EBITDA"),      "x"),
                    ("VAL_DIVIDEND_YIELD", ind_map.get("VAL_DIVIDEND_YIELD"), "%"),
                ]:
                    if value is None: continue
                    cur.execute("""
                        INSERT INTO indicator_values
                            (asset_id, indicator_code, reference_date, raw_value,
                             unit, period_type, source_id, calculation_version)
                        VALUES (%s,%s,%s,%s,%s,'TTM',%s,'1.0')
                        ON CONFLICT (asset_id, indicator_code, reference_date, period_type)
                        DO UPDATE SET raw_value=EXCLUDED.raw_value
                    """, (asset_id, code, ref, value, unit, source_id))

                score, confianca = calcular_score(ind_map)
                if score is not None:
                    classif = classificar(score)
                    trend   = "UP" if score >= 50 else "DOWN"
                    cur.execute("""
                        INSERT INTO engine_scores
                            (asset_id, engine_name, score, confidence, trend,
                             reference_date, engine_version, methodology_version)
                        VALUES (%s,'Quality',%s,%s,%s,%s,'1.0','1.0')
                        ON CONFLICT (asset_id, engine_name, reference_date)
                        DO UPDATE SET score=EXCLUDED.score, confidence=EXCLUDED.confidence
                    """, (asset_id, score, confianca, trend, ref))
                    cur.execute("""
                        INSERT INTO janus_scores
                            (asset_id, overall_score, confidence, classification,
                             trend, reference_date, methodology_version, engine_version)
                        VALUES (%s,%s,%s,%s,%s,%s,'1.0','1.0')
                        ON CONFLICT (asset_id, reference_date)
                        DO UPDATE SET overall_score=EXCLUDED.overall_score,
                            confidence=EXCLUDED.confidence,
                            classification=EXCLUDED.classification
                    """, (asset_id, score, confianca, classif, trend, ref))
                    rankings_lote.append({"asset_id": asset_id, "ticker": ticker, "score": score})

            except Exception as e:
                print(f"[COLLECTOR] ⚠️ Erro {ticker}: {e}", flush=True)

    conn.commit()
    return rankings_lote

# ── Ranking ────────────────────────────────────────────────
def salvar_ranking(conn, rankings):
    ref = hoje()
    rankings.sort(key=lambda x: x["score"], reverse=True)
    with conn.cursor() as cur:
        for i, item in enumerate(rankings):
            cur.execute("""
                INSERT INTO ranking_snapshots
                    (asset_id, reference_date, janus_score, quality_score,
                     general_position, ranking_type, methodology_version)
                VALUES (%s,%s,%s,%s,%s,'GERAL','1.0')
                ON CONFLICT (asset_id, reference_date, ranking_type)
                DO UPDATE SET janus_score=EXCLUDED.janus_score,
                    quality_score=EXCLUDED.quality_score,
                    general_position=EXCLUDED.general_position
            """, (item["asset_id"], ref, item["score"], item["score"], i + 1))
    conn.commit()
    print(f"[COLLECTOR] 🏆 Ranking salvo: {len(rankings)} ativos", flush=True)

# ── MAIN ───────────────────────────────────────────────────
def run_collector(on_progress=None):
    def prog(pct, atual, total, msg):
        print(f"[COLLECTOR] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, atual, total, msg)
            except: pass

    print("[COLLECTOR] 🚀 Iniciando coleta v1.4...", flush=True)
    conn = get_conn()
    try:
        source_id = get_source_id(conn); conn.commit()
        log_id    = iniciar_log(conn, source_id); conn.commit()

        prog(0, 0, 0, "Buscando lista de ativos...")
        lista = buscar_lista_ativos()
        if not lista:
            finalizar_log(conn, log_id, "FAILED", 0, "Lista vazia")
            conn.commit(); return

        prog(5, 0, len(lista), f"Registrando {len(lista)} ativos no banco...")
        asset_map = upsert_assets_batch(conn, lista)

        tickers   = list(asset_map.keys())
        total_l   = (len(tickers) + LOTE_BRAPI - 1) // LOTE_BRAPI
        processados = 0
        erros       = 0
        rankings    = []

        for i in range(0, len(tickers), LOTE_BRAPI):
            lote     = tickers[i:i+LOTE_BRAPI]
            lote_num = i // LOTE_BRAPI + 1
            atual    = i + len(lote)
            pct      = 10 + round(atual / len(tickers) * 89)
            msg      = f"Lote {lote_num}/{total_l}: {', '.join(lote[:5])}..."
            prog(pct, atual, len(tickers), msg)

            time.sleep(DELAY_MS)
            dados_lote = buscar_dados_lote(lote)

            lote_salvar = [
                (asset_map[t], t, dados_lote[t])
                for t in lote if t in dados_lote
            ]
            erros += len(lote) - len(lote_salvar)

            if lote_salvar:
                try:
                    rl = salvar_lote_banco(conn, lote_salvar, source_id)
                    rankings.extend(rl)
                    processados += len(lote_salvar)
                except Exception as e:
                    print(f"[COLLECTOR] ❌ Erro lote {lote_num}: {e}", flush=True)
                    erros += len(lote_salvar)

        if rankings:
            prog(99, len(tickers), len(tickers), "Salvando ranking...")
            salvar_ranking(conn, rankings)

        finalizar_log(conn, log_id, "SUCCESS", processados); conn.commit()
        prog(100, len(tickers), len(tickers), f"Concluído! {processados} ativos")
        print(f"[COLLECTOR] Total: {len(lista)} | Processados: {processados} | Erros: {erros} | Ranking: {len(rankings)}", flush=True)

    except Exception as e:
        print(f"[COLLECTOR] ❌ Erro fatal: {e}", flush=True)
        try: finalizar_log(conn, log_id, "FAILED", 0, str(e)); conn.commit()
        except: pass
    finally:
        conn.close()

if __name__ == "__main__":
    run_collector()
