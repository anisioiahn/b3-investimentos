# ============================================================
# JANUS INDEX – DATA COLLECTOR v1.2 (Python)
# Adaptado aos módulos do plano Brapi Startup:
#   defaultKeyStatistics, balanceSheetHistory, incomeStatementHistory, summaryProfile
# (financialData e cashflowStatementHistory NÃO disponíveis no plano)
# ============================================================

import os, time, json, requests
import psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta

TOKEN_BRAPI   = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE    = "https://brapi.dev/api"
BRAPI_MODULES = "defaultKeyStatistics,balanceSheetHistory,incomeStatementHistory,summaryProfile"
DELAY_MS      = 0.8   # delay entre requisições para não estourar rate limit
MAX_ATIVOS    = 500   # cobre toda a B3

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
    """Busca todos os ativos da B3 com paginação (limite 500 por página)."""
    print("[COLLECTOR] 📋 Buscando lista completa de ativos da B3...")
    todos = []
    pagina = 1
    limite_pagina = 500
    while True:
        try:
            url = f"{BRAPI_BASE}/quote/list?token={TOKEN_BRAPI}&type=stock&limit={limite_pagina}&page={pagina}&sortBy=volume&sortOrder=desc"
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                dados = r.json()
                stocks = dados.get("stocks", [])
                if not stocks:
                    break
                todos.extend(stocks)
                print(f"[COLLECTOR] 📄 Página {pagina}: {len(stocks)} ativos (total: {len(todos)})")
                # Se retornou menos que o limite, chegamos ao fim
                if len(stocks) < limite_pagina:
                    break
                pagina += 1
                time.sleep(1)
            elif r.status_code == 429:
                print("[COLLECTOR] ⏳ Rate limit na lista, aguardando 30s...")
                time.sleep(30)
            else:
                print(f"[COLLECTOR] ⚠️ Erro na lista: {r.status_code}")
                break
        except Exception as e:
            print(f"[COLLECTOR] ❌ Erro ao buscar lista página {pagina}: {e}")
            break
    print(f"[COLLECTOR] ✅ {len(todos)} ativos encontrados no total")
    return todos

# ── STEP 2: Upsert company + asset ───────────────────────────
def upsert_asset(stock):
    ticker = stock.get("stock") or stock.get("symbol")
    if not ticker: return None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
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

# ── STEP 3: Dados completos em lote (até 5 tickers por chamada) ──
LOTE_FUNDAMENTALISTA = 5  # Brapi suporta múltiplos tickers com módulos

def buscar_dados_lote(tickers):
    """Busca dados fundamentalistas de até 5 tickers numa só chamada."""
    joined = ",".join(tickers)
    url = f"{BRAPI_BASE}/quote/{joined}?modules={BRAPI_MODULES}&token={TOKEN_BRAPI}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            results = r.json().get("results", [])
            # Retorna dicionário ticker → dados
            return {d["symbol"]: d for d in results if d.get("symbol")}
        elif r.status_code == 429:
            print("[COLLECTOR] ⏳ Rate limit, aguardando 30s...")
            time.sleep(30)
            # Tenta de novo após espera
            r2 = requests.get(url, headers=headers, timeout=30)
            if r2.status_code == 200:
                results = r2.json().get("results", [])
                return {d["symbol"]: d for d in results if d.get("symbol")}
        else:
            print(f"[COLLECTOR] ⚠️ Status {r.status_code} para lote {joined}: {r.text[:150]}")
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro lote {joined}: {e}")
    return {}

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

        # STEP 1: Upsert todos os assets primeiro (rápido — só banco)
        asset_map = {}  # ticker → asset_id
        for stock in lista:
            ticker = stock.get("stock") or stock.get("symbol")
            if not ticker: continue
            asset_id = upsert_asset(stock)
            if asset_id:
                asset_map[ticker] = asset_id

        print(f"[COLLECTOR] 💾 {len(asset_map)} ativos registrados no banco")

        # STEP 2: Busca dados fundamentalistas em lotes de 5
        tickers_lista = list(asset_map.keys())
        total_lotes = (len(tickers_lista) + LOTE_FUNDAMENTALISTA - 1) // LOTE_FUNDAMENTALISTA
        print(f"[COLLECTOR] 📦 Processando {len(tickers_lista)} ativos em {total_lotes} lotes de {LOTE_FUNDAMENTALISTA}")

        for i in range(0, len(tickers_lista), LOTE_FUNDAMENTALISTA):
            lote = tickers_lista[i:i+LOTE_FUNDAMENTALISTA]
            lote_num = i // LOTE_FUNDAMENTALISTA + 1
            print(f"[COLLECTOR] 📊 Lote {lote_num}/{total_lotes}: {', '.join(lote)}")

            time.sleep(DELAY_MS)
            dados_lote = buscar_dados_lote(lote)

            for ticker in lote:
                asset_id = asset_map[ticker]
                dados = dados_lote.get(ticker)

                if not dados:
                    print(f"[COLLECTOR] ⚠️ Sem dados para {ticker}")
                    total_erros += 1
                    continue

                try:
                    salvar_market_snapshot(asset_id, dados, source_id)
                    salvar_financial_snapshot(asset_id, dados, source_id)
                    ind_map = salvar_indicadores(asset_id, dados, source_id)

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
        print(f"[COLLECTOR]    Total B3:    {len(lista)}")
        print(f"[COLLECTOR]    Processados: {total_processados}")
        print(f"[COLLECTOR]    Erros:       {total_erros}")
        print(f"[COLLECTOR]    No ranking:  {len(rankings)}")

    except Exception as e:
        print(f"[COLLECTOR] ❌ Erro fatal: {e}")
        finalizar_log(log_id, "FAILED", total_processados, str(e))
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
                safe_num((dados.get("defaultKeyStatistics") or {}).get("beta")),
                source_id
            ))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro market_snapshot: {e}")

# ── STEP 5: Financial snapshot (a partir de incomeStatementHistory + balanceSheetHistory) ─
def salvar_financial_snapshot(asset_id, dados, source_id):
    try:
        ks = dados.get("defaultKeyStatistics") or {}
        inc_hist = dados.get("incomeStatementHistory") or []
        bal_hist = dados.get("balanceSheetHistory") or []

        inc = inc_hist[0] if inc_hist else {}
        bal = bal_hist[0] if bal_hist else {}

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO financial_snapshots
                    (asset_id, reference_date, period_type, revenue, gross_profit, ebitda,
                     net_income, equity, total_assets, total_debt, cash_and_equivalents,
                     operating_cash_flow, free_cash_flow, capex, source_id, data_version)
                VALUES (%s,%s,'ANUAL',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'1.0')
                ON CONFLICT (asset_id, reference_date, period_type) DO UPDATE SET
                    revenue=EXCLUDED.revenue, gross_profit=EXCLUDED.gross_profit,
                    ebitda=EXCLUDED.ebitda, net_income=EXCLUDED.net_income,
                    equity=EXCLUDED.equity, total_assets=EXCLUDED.total_assets,
                    total_debt=EXCLUDED.total_debt, cash_and_equivalents=EXCLUDED.cash_and_equivalents
            """, (
                asset_id, hoje(),
                safe_num(inc.get("totalRevenue")),
                safe_num(inc.get("grossProfit")),
                safe_num(inc.get("cleanEbitda") or inc.get("ebit")),
                safe_num(inc.get("netIncome")),
                safe_num(bal.get("totalStockholderEquity")),
                safe_num(bal.get("totalAssets")),
                safe_num(bal.get("totalLiab") or bal.get("totalDebt")),
                safe_num(bal.get("cash")),
                None,  # operating_cash_flow indisponível no plano atual
                None,  # free_cash_flow indisponível no plano atual
                None,  # capex indisponível no plano atual
                source_id
            ))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro financial_snapshot: {e}")

# ── STEP 6: Indicadores ───────────────────────────────────────
def calcular_indicadores_derivados(dados):
    """Calcula ROE, Margem Líquida e Crescimento de Receita a partir
    dos módulos disponíveis no plano (sem financialData)."""
    ks = dados.get("defaultKeyStatistics") or {}
    inc_hist = dados.get("incomeStatementHistory") or []

    resultado = {}

    # Margem líquida: já vem pronta no defaultKeyStatistics
    resultado["FIN_NET_MARGIN"] = safe_num(ks.get("profitMargins"))

    # ROE = Lucro Líquido / Patrimônio Líquido
    net_income = safe_num(ks.get("netIncomeToCommon"))
    book_value = safe_num(ks.get("bookValue"))
    shares     = safe_num(ks.get("sharesOutstanding"))
    if net_income is not None and book_value and shares:
        equity = book_value * shares
        resultado["FIN_ROE"] = net_income / equity if equity else None
    else:
        resultado["FIN_ROE"] = None

    # Crescimento de Receita = (receita atual - receita anterior) / receita anterior
    if len(inc_hist) >= 2:
        rev_atual    = safe_num(inc_hist[0].get("totalRevenue"))
        rev_anterior = safe_num(inc_hist[1].get("totalRevenue"))
        if rev_atual is not None and rev_anterior:
            resultado["FIN_REVENUE_GROWTH"] = (rev_atual - rev_anterior) / rev_anterior
        else:
            resultado["FIN_REVENUE_GROWTH"] = None
    else:
        resultado["FIN_REVENUE_GROWTH"] = None

    # ROIC aproximado: usamos ROE como proxy por ora (sem dívida detalhada confiável)
    resultado["FIN_ROIC"] = resultado["FIN_ROE"]

    return resultado


def salvar_indicadores(asset_id, dados, source_id):
    ks = dados.get("defaultKeyStatistics") or {}
    inc_hist = dados.get("incomeStatementHistory") or []
    inc = inc_hist[0] if inc_hist else {}
    derivados = calcular_indicadores_derivados(dados)

    indicadores = [
        ("FIN_ROE",            derivados.get("FIN_ROE"),            "%"),
        ("FIN_ROIC",           derivados.get("FIN_ROIC"),           "%"),
        ("FIN_NET_MARGIN",     derivados.get("FIN_NET_MARGIN"),     "%"),
        ("FIN_REVENUE",        safe_num(inc.get("totalRevenue")),   "R$"),
        ("FIN_REVENUE_GROWTH", derivados.get("FIN_REVENUE_GROWTH"), "%"),
        ("VAL_PE",             safe_num(ks.get("trailingPE")),      "x"),
        ("VAL_PVP",            safe_num(ks.get("priceToBook")),     "x"),
        ("VAL_EV_EBITDA",      safe_num(ks.get("enterpriseToEbitda")), "x"),
        ("VAL_DIVIDEND_YIELD", safe_num(ks.get("dividendYield")),   "%"),
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
                """, (asset_id, code, hoje(), value, unit, source_id))
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[COLLECTOR] ⚠️ Erro indicador {code}: {e}")

    return derivados

# ── STEP 7: Calcular Quality Score ───────────────────────────
# FCO removido do MVP (indisponível no plano atual)
BENCHMARKS = {
    "FIN_ROE":            {"min": -0.10, "max": 0.40},
    "FIN_ROIC":           {"min": -0.05, "max": 0.35},
    "FIN_NET_MARGIN":     {"min": -0.10, "max": 0.30},
    "FIN_REVENUE_GROWTH": {"min": -0.20, "max": 0.50},
}
PESOS = {
    "FIN_ROE":            0.30,
    "FIN_ROIC":           0.25,
    "FIN_NET_MARGIN":     0.25,
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

    score_final = score_total / peso_total
    confianca   = (peso_total / 1.0) * 100

    return {
        "score":      round(score_final, 2),
        "confidence": round(confianca, 2),
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

if __name__ == "__main__":
    run_collector()
