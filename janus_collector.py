# ============================================================
# JANUS INDEX – DATA COLLECTOR v1.1 (Python)
# Segue o padrão do projeto: psycopg2 + get_conn()
# ============================================================

import os, time, json, requests
import psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta

TOKEN_BRAPI   = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE    = "https://brapi.dev/api"
BRAPI_MODULES = "defaultKeyStatistics,financialData,balanceSheetHistory,cashflowStatementHistory"
DELAY_MS      = 0.5
MAX_ATIVOS    = 100

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora():    return datetime.now(TZ_BRASILIA)
def agora_str(): return agora().isoformat()
def hoje():     return agora().strftime("%Y-%m-%d")

def safe_num(v):
    try:    return float(v) if v is not None else None
    except: return None

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── Fonte de dados ────────────────────────────────────────────
def get_source_id():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT source_id FROM data_sources WHERE source_name = 'Brapi'")
            row = cur.fetchone()
            if row:
                conn.close()
                return row[0]
            cur.execute("""
                INSERT INTO data_sources (source_name, source_type, url, reliability, is_primary)
                VALUES ('Brapi', 'API', 'https://brapi.dev', 95.0, TRUE)
                RETURNING source_id
            """)
            source_id = cur.fetchone()[0]
        conn.commit(); conn.close()
        return source_id
    except Exception as e:
        print(f"[COLLECTOR] Erro ao buscar source_id: {e}")
        return None

# ── Log de ingestão ───────────────────────────────────────────
def iniciar_log(job_name, source_id):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO data_ingestion_logs (source_id, job_name, started_at, status, records_processed)
                VALUES (%s, %s, %s, 'RUNNING', 0) RETURNING ingestion_log_id
            """, (source_id, job_name, agora_str()))
            log_id = cur.fetchone()[0]
        conn.commit(); conn.close()
        return log_id
    except Exception as e:
        print(f"[COLLECTOR] Erro ao iniciar log: {e}")
        return None

def finalizar_log(log_id, status, records, error_msg=None):
    if not log_id: return
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE data_ingestion_logs
                SET finished_at=%s, status=%s, records_processed=%s, error_message=%s
                WHERE ingestion_log_id=%s
            """, (agora_str(), status, records, error_msg, log_id))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] Erro ao finalizar log: {e}")

# ── STEP 1: Lista de ativos da B3 ────────────────────────────
def buscar_lista_ativos():
    print("[COLLECTOR] 📋 Buscando lista de ativos da B3...")
    url = f"{BRAPI_BASE}/quote/list?token={TOKEN_BRAPI}&type=stock&limit={MAX_ATIVOS}&sortBy=volume&sortOrder=desc"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            stocks = r.json().get("stocks", [])
            print(f"[COLLECTOR] ✅ {len(stocks)} ativos encontrados")
            return stocks
        print(f"[COLLECTOR] ⚠️ Erro na lista: {r.status_code}")
    except Exception as e:
        print(f"[COLLECTOR] ❌ Erro ao buscar lista: {e}")
    return []

# ── STEP 2: Upsert company + asset ───────────────────────────
def upsert_asset(stock):
    ticker = stock.get("stock") or stock.get("symbol")
    if not ticker: return None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Upsert company
            company_id = None
            nome = stock.get("name", ticker)
            setor = stock.get("sector")
            if nome or setor:
                cur.execute("""
                    INSERT INTO companies (corporate_name, trading_name, sector, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (trading_name) DO UPDATE SET
                        corporate_name=EXCLUDED.corporate_name,
                        sector=EXCLUDED.sector,
                        updated_at=EXCLUDED.updated_at
                    RETURNING company_id
                    """, (nome, nome, setor, agora_str()))
                company_id = cur.fetchone()[0]

            # Upsert asset
            cur.execute("""
                INSERT INTO assets (ticker, company_id, asset_type, currency, country, status, updated_at)
                VALUES (%s, %s, 'ACAO', 'BRL', 'BR', 'ATIVO', %s)
                ON CONFLICT (ticker) DO UPDATE SET company_id=EXCLUDED.company_id, updated_at=EXCLUDED.updated_at
                RETURNING asset_id
            """, (ticker, company_id, agora_str()))
            asset_id = cur.fetchone()[0]
        conn.commit(); conn.close()
        return asset_id
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro upsert {ticker}: {e}")
        return None

# ── STEP 3: Dados completos do ativo na Brapi ────────────────
def buscar_dados_ativo(ticker):
    dados = None
    try:
        r = requests.get(f"{BRAPI_BASE}/quote/{ticker}?token={TOKEN_BRAPI}", timeout=20)
        if r.status_code == 200:
            results = r.json().get("results", [])
            dados = results[0] if results else None
        elif r.status_code == 429:
            print("[COLLECTOR] ⏳ Rate limit, aguardando 30s...")
            time.sleep(30)
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro cotação {ticker}: {e}")

    if not dados:
        return None

    try:
        time.sleep(0.3)
        r2 = requests.get(f"{BRAPI_BASE}/quote/{ticker}?modules={BRAPI_MODULES}&token={TOKEN_BRAPI}", timeout=20)
        if r2.status_code == 200:
            results2 = r2.json().get("results", [])
            if results2:
                for k, v in results2[0].items():
                    if v is not None and dados.get(k) is None:
                        dados[k] = v
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Módulos indisponíveis para {ticker}")

    return dados

# ── STEP 4: Market snapshot ───────────────────────────────────
def salvar_market_snapshot(asset_id, dados, source_id):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO market_snapshots
                    (asset_id, reference_date, open_price, high_price, low_price,
                     close_price, last_price, volume, market_cap, beta, source_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, reference_date) DO UPDATE SET
                    open_price=EXCLUDED.open_price, high_price=EXCLUDED.high_price,
                    low_price=EXCLUDED.low_price, close_price=EXCLUDED.close_price,
                    last_price=EXCLUDED.last_price, volume=EXCLUDED.volume,
                    market_cap=EXCLUDED.market_cap, beta=EXCLUDED.beta
            """, (
                asset_id, hoje(),
                safe_num(dados.get("regularMarketOpen")),
                safe_num(dados.get("regularMarketDayHigh")),
                safe_num(dados.get("regularMarketDayLow")),
                safe_num(dados.get("regularMarketPrice")),
                safe_num(dados.get("regularMarketPrice")),
                safe_num(dados.get("regularMarketVolume")),
                safe_num(dados.get("marketCap")),
                safe_num(dados.get("beta")),
                source_id
            ))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro market_snapshot: {e}")

# ── STEP 5: Financial snapshot ────────────────────────────────
def salvar_financial_snapshot(asset_id, dados, source_id):
    try:
        fin = dados.get("financialData") or {}
        bal = ((dados.get("balanceSheetHistory") or {}).get("balanceSheetStatements") or [{}])[0]
        cf  = ((dados.get("cashflowStatementHistory") or {}).get("cashflowStatements") or [{}])[0]

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO financial_snapshots
                    (asset_id, reference_date, period_type, revenue, gross_profit, ebitda,
                     net_income, equity, total_assets, total_debt, cash_and_equivalents,
                     operating_cash_flow, free_cash_flow, capex, source_id, data_version)
                VALUES (%s,%s,'TTM',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'1.0')
                ON CONFLICT (asset_id, reference_date, period_type) DO UPDATE SET
                    revenue=EXCLUDED.revenue, gross_profit=EXCLUDED.gross_profit,
                    ebitda=EXCLUDED.ebitda, net_income=EXCLUDED.net_income,
                    equity=EXCLUDED.equity, total_assets=EXCLUDED.total_assets,
                    total_debt=EXCLUDED.total_debt, cash_and_equivalents=EXCLUDED.cash_and_equivalents,
                    operating_cash_flow=EXCLUDED.operating_cash_flow,
                    free_cash_flow=EXCLUDED.free_cash_flow, capex=EXCLUDED.capex
            """, (
                asset_id, hoje(),
                safe_num((fin.get("totalRevenue") or {}).get("raw")),
                safe_num((fin.get("grossProfits") or {}).get("raw")),
                safe_num((fin.get("ebitda") or {}).get("raw")),
                safe_num((fin.get("netIncomeToCommon") or {}).get("raw")),
                safe_num((bal.get("totalStockholderEquity") or {}).get("raw")),
                safe_num((bal.get("totalAssets") or {}).get("raw")),
                safe_num((bal.get("totalDebt") or fin.get("totalDebt") or {}).get("raw")),
                safe_num((bal.get("cash") or fin.get("totalCash") or {}).get("raw")),
                safe_num((fin.get("operatingCashflow") or cf.get("totalCashFromOperatingActivities") or {}).get("raw")),
                safe_num((fin.get("freeCashflow") or {}).get("raw")),
                safe_num((cf.get("capitalExpenditures") or {}).get("raw")),
                source_id
            ))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro financial_snapshot: {e}")

# ── STEP 6: Indicadores ───────────────────────────────────────
def salvar_indicadores(asset_id, dados, source_id):
    ks  = dados.get("defaultKeyStatistics") or {}
    fin = dados.get("financialData") or {}

    indicadores = [
        ("FIN_ROE",            (ks.get("returnOnEquity")       or {}).get("raw"), "%"),
        ("FIN_ROIC",           (ks.get("returnOnCapital")      or {}).get("raw"), "%"),
        ("FIN_NET_MARGIN",     (fin.get("profitMargins")       or {}).get("raw"), "%"),
        ("FIN_FCO",            (fin.get("operatingCashflow")   or {}).get("raw"), "R$"),
        ("FIN_REVENUE",        (fin.get("totalRevenue")        or {}).get("raw"), "R$"),
        ("FIN_REVENUE_GROWTH", (fin.get("revenueGrowth")       or {}).get("raw"), "%"),
        ("FIN_GROSS_MARGIN",   (fin.get("grossMargins")        or {}).get("raw"), "%"),
        ("FIN_EBITDA_MARGIN",  (fin.get("ebitdaMargins")       or {}).get("raw"), "%"),
        ("VAL_PE",             (ks.get("forwardPE")            or {}).get("raw"), "x"),
        ("VAL_PVP",            (ks.get("priceToBook")          or {}).get("raw"), "x"),
        ("VAL_EV_EBITDA",      (ks.get("enterpriseToEbitda")   or {}).get("raw"), "x"),
        ("VAL_DIVIDEND_YIELD", (ks.get("dividendYield")        or {}).get("raw"), "%"),
    ]

    for code, value, unit in indicadores:
        if value is None: continue
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO indicator_values
                        (asset_id, indicator_code, reference_date, raw_value, unit,
                         period_type, source_id, calculation_version)
                    VALUES (%s,%s,%s,%s,%s,'TTM',%s,'1.0')
                    ON CONFLICT (asset_id, indicator_code, reference_date, period_type)
                    DO UPDATE SET raw_value=EXCLUDED.raw_value
                """, (asset_id, code, hoje(), safe_num(value), unit, source_id))
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[COLLECTOR] ⚠️ Erro indicador {code}: {e}")

# ── STEP 7: Calcular Quality Score ───────────────────────────
BENCHMARKS = {
    "FIN_ROE":            {"min": -0.10, "max": 0.40},
    "FIN_ROIC":           {"min": -0.05, "max": 0.35},
    "FIN_NET_MARGIN":     {"min": -0.10, "max": 0.30},
    "FIN_FCO":            {"min": 0,     "max": 10_000_000_000},
    "FIN_REVENUE_GROWTH": {"min": -0.20, "max": 0.50},
}
PESOS = {
    "FIN_ROE":            0.25,
    "FIN_ROIC":           0.20,
    "FIN_NET_MARGIN":     0.20,
    "FIN_FCO":            0.15,
    "FIN_REVENUE_GROWTH": 0.20,
}

def calcular_quality_score(ind_map):
    score_total = 0.0
    peso_total  = 0.0
    evidencias  = []

    for code, peso in PESOS.items():
        valor = ind_map.get(code)
        if valor is None: continue
        b = BENCHMARKS[code]
        norm = (valor - b["min"]) / (b["max"] - b["min"]) * 100
        norm = max(0.0, min(100.0, norm))
        score_total += norm * peso
        peso_total  += peso
        evidencias.append({"code": code, "valor": valor, "score": norm, "peso": peso})

    if peso_total == 0: return None
    return {
        "score":      round(score_total / peso_total, 2),
        "confidence": round((peso_total / 1.0) * 100, 2),
        "evidencias": evidencias
    }

def classificar_score(score):
    if score >= 80: return "Muito Favorável"
    if score >= 60: return "Favorável"
    if score >= 40: return "Neutro"
    if score >= 20: return "Desfavorável"
    return "Muito Desfavorável"

# ── STEP 8: Salvar evidências e scores ───────────────────────
def salvar_scores(asset_id, ind_map):
    resultado = calcular_quality_score(ind_map)
    if not resultado: return None

    score      = resultado["score"]
    confidence = resultado["confidence"]
    evidencias = resultado["evidencias"]
    ref_date   = hoje()

    for ev in evidencias:
        trend = "UP" if ev["valor"] > 0 else ("DOWN" if ev["valor"] < 0 else "STABLE")
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO evidences
                        (asset_id, evidence_code, engine_name, score, confidence,
                         trend, weight, explanation, methodology_version, reference_date)
                    VALUES (%s,%s,'Quality',%s,%s,%s,%s,%s,'1.0',%s)
                """, (
                    asset_id, f"QUALITY_{ev['code']}",
                    round(ev["score"], 2), round(confidence),
                    trend, ev["peso"],
                    f"{ev['code']} = {ev['valor']:.4f} → score: {ev['score']:.1f}",
                    ref_date
                ))
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[COLLECTOR] ⚠️ Erro evidence {ev['code']}: {e}")

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO engine_scores
                    (asset_id, engine_name, score, confidence, trend,
                     reference_date, engine_version, methodology_version)
                VALUES (%s,'Quality',%s,%s,%s,%s,'1.0','1.0')
                ON CONFLICT (asset_id, engine_name, reference_date)
                DO UPDATE SET score=EXCLUDED.score, confidence=EXCLUDED.confidence
            """, (asset_id, score, confidence, "UP" if score >= 50 else "DOWN", ref_date))
            cur.execute("""
                INSERT INTO janus_scores
                    (asset_id, overall_score, confidence, classification, trend,
                     reference_date, methodology_version, engine_version)
                VALUES (%s,%s,%s,%s,%s,%s,'1.0','1.0')
                ON CONFLICT (asset_id, reference_date)
                DO UPDATE SET overall_score=EXCLUDED.overall_score,
                    confidence=EXCLUDED.confidence, classification=EXCLUDED.classification
            """, (asset_id, score, confidence, classificar_score(score),
                  "UP" if score >= 50 else "DOWN", ref_date))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro scores: {e}")

    return score

# ── STEP 9: Ranking ───────────────────────────────────────────
def salvar_ranking(rankings):
    ref_date = hoje()
    rankings.sort(key=lambda x: x["score"], reverse=True)
    for i, item in enumerate(rankings):
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ranking_snapshots
                        (asset_id, reference_date, janus_score, quality_score,
                         general_position, ranking_type, methodology_version)
                    VALUES (%s,%s,%s,%s,%s,'GERAL','1.0')
                    ON CONFLICT (asset_id, reference_date, ranking_type)
                    DO UPDATE SET janus_score=EXCLUDED.janus_score,
                        quality_score=EXCLUDED.quality_score,
                        general_position=EXCLUDED.general_position
                """, (item["asset_id"], ref_date, item["score"], item["score"], i + 1))
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[COLLECTOR] ⚠️ Erro ranking {item['ticker']}: {e}")
    print(f"[COLLECTOR] 🏆 Ranking salvo com {len(rankings)} ativos")

# ── MAIN ─────────────────────────────────────────────────────
def run_collector():
    print(f"[COLLECTOR] 🚀 Janus Index Data Collector iniciando...")
    print(f"[COLLECTOR] 📅 Data de referência: {hoje()}")

    source_id = get_source_id()
    log_id    = iniciar_log("janus-data-collector", source_id)

    total_processados = 0
    total_erros       = 0
    rankings          = []

    try:
        lista = buscar_lista_ativos()
        if not lista:
            finalizar_log(log_id, "FAILED", 0, "Lista de ativos vazia")
            return

        for stock in lista:
            ticker = stock.get("stock") or stock.get("symbol")
            if not ticker: continue

            try:
                print(f"[COLLECTOR] 📊 Processando {ticker}...")

                asset_id = upsert_asset(stock)
                if not asset_id:
                    print(f"[COLLECTOR] ⚠️ Sem asset_id para {ticker}")
                    continue

                time.sleep(DELAY_MS)
                dados = buscar_dados_ativo(ticker)
                if not dados:
                    print(f"[COLLECTOR] ⚠️ Sem dados para {ticker}")
                    continue

                salvar_market_snapshot(asset_id, dados, source_id)
                salvar_financial_snapshot(asset_id, dados, source_id)
                salvar_indicadores(asset_id, dados, source_id)

                ks  = dados.get("defaultKeyStatistics") or {}
                fin = dados.get("financialData") or {}

                ind_map = {
                    "FIN_ROE":            safe_num((ks.get("returnOnEquity")     or {}).get("raw")),
                    "FIN_ROIC":           safe_num((ks.get("returnOnCapital")    or {}).get("raw")),
                    "FIN_NET_MARGIN":     safe_num((fin.get("profitMargins")     or {}).get("raw")),
                    "FIN_FCO":            safe_num((fin.get("operatingCashflow") or {}).get("raw")),
                    "FIN_REVENUE_GROWTH": safe_num((fin.get("revenueGrowth")     or {}).get("raw")),
                }

                score = salvar_scores(asset_id, ind_map)
                if score is not None:
                    rankings.append({"asset_id": asset_id, "ticker": ticker, "score": score})

                total_processados += 1
                score_str = f"{score:.1f}" if score is not None else "N/A"
                print(f"[COLLECTOR] ✅ {ticker} → Quality Score: {score_str}")

            except Exception as e:
                total_erros += 1
                print(f"[COLLECTOR] ❌ Erro em {ticker}: {e}")

        if rankings:
            salvar_ranking(rankings)

        finalizar_log(log_id, "SUCCESS", total_processados)
        print(f"[COLLECTOR] ✅ Coleta finalizada!")
        print(f"[COLLECTOR]    Processados: {total_processados}")
        print(f"[COLLECTOR]    Erros:       {total_erros}")
        print(f"[COLLECTOR]    No ranking:  {len(rankings)}")

    except Exception as e:
        print(f"[COLLECTOR] ❌ Erro fatal: {e}")
        finalizar_log(log_id, "FAILED", total_processados, str(e))

if __name__ == "__main__":
    run_collector()
