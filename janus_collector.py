# ============================================================
# JANUS INDEX – DATA COLLECTOR v1.0 (Python)
# Responsabilidade: buscar dados da Brapi e salvar no Supabase
# Segue o padrão do projeto: requests + db.py
# ============================================================

import os, time, requests
from datetime import datetime, timezone, timedelta
import db

TOKEN_BRAPI  = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE   = "https://brapi.dev/api"
BRAPI_MODULES = "defaultKeyStatistics,financialData,balanceSheetHistory,cashflowStatementHistory"
DELAY_MS     = 0.5   # segundos entre requisições
MAX_ATIVOS   = 100   # máximo de ativos por execução

TZ_BRASILIA  = timezone(timedelta(hours=-3))
def agora():    return datetime.now(TZ_BRASILIA)
def hoje():     return agora().strftime("%Y-%m-%d")
def safe_num(v):
    try:    return float(v) if v is not None else None
    except: return None

# ── Fonte de dados ────────────────────────────────────────────
def get_source_id():
    """Busca ou cria o registro da Brapi em data_sources."""
    try:
        res = db.supabase.table("data_sources") \
            .select("source_id") \
            .eq("source_name", "Brapi") \
            .execute()
        if res.data:
            return res.data[0]["source_id"]
        novo = db.supabase.table("data_sources").insert({
            "source_name":    "Brapi",
            "source_type":    "API",
            "url":            "https://brapi.dev",
            "reliability":    95.0,
            "is_primary":     True
        }).execute()
        return novo.data[0]["source_id"]
    except Exception as e:
        print(f"[COLLECTOR] Erro ao buscar source_id: {e}")
        return None

# ── Log de ingestão ───────────────────────────────────────────
def iniciar_log(job_name, source_id):
    try:
        res = db.supabase.table("data_ingestion_logs").insert({
            "source_id":   source_id,
            "job_name":    job_name,
            "started_at":  agora().isoformat(),
            "status":      "RUNNING",
            "records_processed": 0
        }).execute()
        return res.data[0]["ingestion_log_id"]
    except Exception as e:
        print(f"[COLLECTOR] Erro ao iniciar log: {e}")
        return None

def finalizar_log(log_id, status, records, error_msg=None):
    if not log_id: return
    try:
        db.supabase.table("data_ingestion_logs").update({
            "finished_at":       agora().isoformat(),
            "status":            status,
            "records_processed": records,
            "error_message":     error_msg
        }).eq("ingestion_log_id", log_id).execute()
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
        # Upsert company
        company_id = None
        if stock.get("name") or stock.get("sector"):
            res = db.supabase.table("companies").upsert({
                "trading_name": stock.get("name", ticker),
                "sector":       stock.get("sector"),
                "updated_at":   agora().isoformat()
            }, on_conflict="trading_name").execute()
            if res.data:
                company_id = res.data[0]["company_id"]

        # Upsert asset
        res = db.supabase.table("assets").upsert({
            "ticker":     ticker,
            "company_id": company_id,
            "asset_type": "ACAO",
            "currency":   "BRL",
            "country":    "BR",
            "status":     "ATIVO",
            "updated_at": agora().isoformat()
        }, on_conflict="ticker").execute()

        if res.data:
            return res.data[0]["asset_id"]
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro upsert {ticker}: {e}")
    return None

# ── STEP 3: Dados completos do ativo na Brapi ────────────────
def buscar_dados_ativo(ticker):
    url = f"{BRAPI_BASE}/quote/{ticker}?modules={BRAPI_MODULES}&token={TOKEN_BRAPI}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            results = r.json().get("results", [])
            return results[0] if results else None
        if r.status_code == 429:
            print("[COLLECTOR] ⏳ Rate limit, aguardando 30s...")
            time.sleep(30)
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro dados {ticker}: {e}")
    return None

# ── STEP 4: Market snapshot ───────────────────────────────────
def salvar_market_snapshot(asset_id, dados, source_id):
    try:
        db.supabase.table("market_snapshots").upsert({
            "asset_id":       asset_id,
            "reference_date": hoje(),
            "open_price":     safe_num(dados.get("regularMarketOpen")),
            "high_price":     safe_num(dados.get("regularMarketDayHigh")),
            "low_price":      safe_num(dados.get("regularMarketDayLow")),
            "close_price":    safe_num(dados.get("regularMarketPrice")),
            "last_price":     safe_num(dados.get("regularMarketPrice")),
            "volume":         safe_num(dados.get("regularMarketVolume")),
            "market_cap":     safe_num(dados.get("marketCap")),
            "beta":           safe_num(dados.get("beta")),
            "source_id":      source_id
        }, on_conflict="asset_id,reference_date").execute()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro market_snapshot: {e}")

# ── STEP 5: Financial snapshot ────────────────────────────────
def salvar_financial_snapshot(asset_id, dados, source_id):
    try:
        fin = dados.get("financialData") or {}
        bal = ((dados.get("balanceSheetHistory") or {}).get("balanceSheetStatements") or [{}])[0]
        cf  = ((dados.get("cashflowStatementHistory") or {}).get("cashflowStatements") or [{}])[0]

        db.supabase.table("financial_snapshots").upsert({
            "asset_id":             asset_id,
            "reference_date":       hoje(),
            "period_type":          "TTM",
            "revenue":              safe_num((fin.get("totalRevenue") or {}).get("raw")),
            "gross_profit":         safe_num((fin.get("grossProfits") or {}).get("raw")),
            "ebitda":               safe_num((fin.get("ebitda") or {}).get("raw")),
            "net_income":           safe_num((fin.get("netIncomeToCommon") or {}).get("raw")),
            "equity":               safe_num((bal.get("totalStockholderEquity") or {}).get("raw")),
            "total_assets":         safe_num((bal.get("totalAssets") or {}).get("raw")),
            "total_debt":           safe_num((bal.get("totalDebt") or (fin.get("totalDebt") or {})).get("raw")),
            "cash_and_equivalents": safe_num((bal.get("cash") or (fin.get("totalCash") or {})).get("raw")),
            "operating_cash_flow":  safe_num((fin.get("operatingCashflow") or (cf.get("totalCashFromOperatingActivities") or {})).get("raw")),
            "free_cash_flow":       safe_num((fin.get("freeCashflow") or {}).get("raw")),
            "capex":                safe_num((cf.get("capitalExpenditures") or {}).get("raw")),
            "source_id":            source_id,
            "data_version":         "1.0"
        }, on_conflict="asset_id,reference_date,period_type").execute()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro financial_snapshot: {e}")

# ── STEP 6: Indicadores ───────────────────────────────────────
def salvar_indicadores(asset_id, dados, source_id):
    ks  = dados.get("defaultKeyStatistics") or {}
    fin = dados.get("financialData") or {}

    indicadores = [
        ("FIN_ROE",            (ks.get("returnOnEquity")  or {}).get("raw"), "%"),
        ("FIN_ROIC",           (ks.get("returnOnCapital") or {}).get("raw"), "%"),
        ("FIN_NET_MARGIN",     (fin.get("profitMargins")  or {}).get("raw"), "%"),
        ("FIN_FCO",            (fin.get("operatingCashflow") or {}).get("raw"), "R$"),
        ("FIN_REVENUE",        (fin.get("totalRevenue")   or {}).get("raw"), "R$"),
        ("FIN_REVENUE_GROWTH", (fin.get("revenueGrowth")  or {}).get("raw"), "%"),
        ("FIN_GROSS_MARGIN",   (fin.get("grossMargins")   or {}).get("raw"), "%"),
        ("FIN_EBITDA_MARGIN",  (fin.get("ebitdaMargins")  or {}).get("raw"), "%"),
        ("VAL_PE",             (ks.get("forwardPE")       or {}).get("raw"), "x"),
        ("VAL_PVP",            (ks.get("priceToBook")     or {}).get("raw"), "x"),
        ("VAL_EV_EBITDA",      (ks.get("enterpriseToEbitda") or {}).get("raw"), "x"),
        ("VAL_DIVIDEND_YIELD", (ks.get("dividendYield")   or {}).get("raw"), "%"),
    ]

    for code, value, unit in indicadores:
        if value is None: continue
        try:
            db.supabase.table("indicator_values").upsert({
                "asset_id":            asset_id,
                "indicator_code":      code,
                "reference_date":      hoje(),
                "raw_value":           safe_num(value),
                "unit":                unit,
                "period_type":         "TTM",
                "source_id":           source_id,
                "calculation_version": "1.0"
            }, on_conflict="asset_id,indicator_code,reference_date,period_type").execute()
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

    # Evidências individuais
    for ev in evidencias:
        trend = "UP" if ev["valor"] > 0 else ("DOWN" if ev["valor"] < 0 else "STABLE")
        try:
            db.supabase.table("evidences").insert({
                "asset_id":            asset_id,
                "evidence_code":       f"QUALITY_{ev['code']}",
                "engine_name":         "Quality",
                "score":               round(ev["score"], 2),
                "confidence":          round(confidence),
                "trend":               trend,
                "weight":              ev["peso"],
                "explanation":         f"{ev['code']} = {ev['valor']:.4f} → score normalizado: {ev['score']:.1f}",
                "methodology_version": "1.0",
                "reference_date":      ref_date
            }).execute()
        except Exception as e:
            print(f"[COLLECTOR] ⚠️ Erro ao salvar evidence {ev['code']}: {e}")

    # Engine score (Quality)
    try:
        db.supabase.table("engine_scores").upsert({
            "asset_id":            asset_id,
            "engine_name":         "Quality",
            "score":               score,
            "confidence":          confidence,
            "trend":               "UP" if score >= 50 else "DOWN",
            "reference_date":      ref_date,
            "engine_version":      "1.0",
            "methodology_version": "1.0"
        }, on_conflict="asset_id,engine_name,reference_date").execute()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro ao salvar engine_score: {e}")

    # Janus Score final
    try:
        db.supabase.table("janus_scores").upsert({
            "asset_id":            asset_id,
            "overall_score":       score,
            "confidence":          confidence,
            "classification":      classificar_score(score),
            "trend":               "UP" if score >= 50 else "DOWN",
            "reference_date":      ref_date,
            "methodology_version": "1.0",
            "engine_version":      "1.0"
        }, on_conflict="asset_id,reference_date").execute()
    except Exception as e:
        print(f"[COLLECTOR] ⚠️ Erro ao salvar janus_score: {e}")

    return score

# ── STEP 9: Ranking ───────────────────────────────────────────
def salvar_ranking(rankings):
    ref_date = hoje()
    rankings.sort(key=lambda x: x["score"], reverse=True)
    for i, item in enumerate(rankings):
        try:
            db.supabase.table("ranking_snapshots").upsert({
                "asset_id":            item["asset_id"],
                "reference_date":      ref_date,
                "janus_score":         item["score"],
                "quality_score":       item["score"],
                "general_position":    i + 1,
                "ranking_type":        "GERAL",
                "methodology_version": "1.0"
            }, on_conflict="asset_id,reference_date,ranking_type").execute()
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
                    "FIN_ROE":            safe_num((ks.get("returnOnEquity")   or {}).get("raw")),
                    "FIN_ROIC":           safe_num((ks.get("returnOnCapital")  or {}).get("raw")),
                    "FIN_NET_MARGIN":     safe_num((fin.get("profitMargins")   or {}).get("raw")),
                    "FIN_FCO":            safe_num((fin.get("operatingCashflow") or {}).get("raw")),
                    "FIN_REVENUE_GROWTH": safe_num((fin.get("revenueGrowth")   or {}).get("raw")),
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
