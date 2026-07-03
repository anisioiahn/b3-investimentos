# ============================================================
# JANUS DIVIDEND ENGINE v1.0
# Coleta histórico de dividendos e calcula o Janus Dividend Score
#
# Janus Dividend Score (0-100):
#   Dividend Yield        15%
#   Crescimento           20%
#   Consistência          20%
#   Payout sustentável    20%
#   Cobertura (proxy)     15%
#   Anos pagando          10%
# ============================================================

import os, time, requests
from datetime import datetime, timezone, timedelta
import psycopg2, psycopg2.extras

TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"
LOTE        = 20   # 20 tickers por chamada (dividendos são mais leves que fundamentalistas)
DELAY       = 0.3  # delay entre lotes

TZ_BR = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BR)
def hoje():  return agora().strftime("%Y-%m-%d")

def safe_num(v):
    try:    return float(v) if v is not None else None
    except: return None

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── Busca dados de dividendos na Brapi ───────────────────────
def buscar_dividendos_lote(tickers):
    """Busca dividendos de até 20 tickers por chamada."""
    joined = ",".join(tickers)
    url = f"{BRAPI_BASE}/quote/{joined}?modules=defaultKeyStatistics&dividends=true&token={TOKEN_BRAPI}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            return {d["symbol"]: d for d in r.json().get("results", []) if d.get("symbol")}
        elif r.status_code == 429:
            print("[DIVIDEND] ⏳ Rate limit, aguardando 30s...", flush=True)
            time.sleep(30)
            r2 = requests.get(url, headers=headers, timeout=30)
            if r2.status_code == 200:
                return {d["symbol"]: d for d in r2.json().get("results", []) if d.get("symbol")}
        else:
            print(f"[DIVIDEND] ⚠️ Status {r.status_code}: {r.text[:150]}", flush=True)
    except Exception as e:
        print(f"[DIVIDEND] ⚠️ Erro lote {joined[:50]}: {e}", flush=True)
    return {}

# ── Calcula indicadores de dividendos ────────────────────────
def calcular_indicadores_dividendos(dados):
    """Extrai e calcula indicadores de dividendos a partir dos dados da Brapi."""
    ks   = dados.get("defaultKeyStatistics") or {}
    hist = dados.get("dividendsData", {}).get("cashDividends", []) or []

    # Dados diretos da Brapi
    dy_12m           = safe_num(ks.get("dividendYield"))
    dy_5y            = safe_num(ks.get("fiveYearAvgDividendYield"))
    trailing_rate    = safe_num(ks.get("trailingAnnualDividendRate"))
    payout           = safe_num(ks.get("payoutRatio"))
    last_div_value   = safe_num(ks.get("lastDividendValue"))
    last_div_date    = ks.get("lastDividendDate")

    # Análise do histórico
    payments_per_year = 0
    years_paying      = 0
    growing_dividends = False
    consistency       = 0.0
    avg_yield         = dy_12m

    if hist:
        # Ordena por data mais recente primeiro
        hist_sorted = sorted(hist, key=lambda x: x.get("paymentDate",""), reverse=True)

        # Pagamentos por ano (baseado nos últimos 12 meses)
        from datetime import datetime as dt
        agora_ts = agora()
        ultimo_ano = [
            h for h in hist_sorted
            if h.get("paymentDate") and
            (agora_ts - dt.strptime(h["paymentDate"][:10], "%Y-%m-%d").replace(tzinfo=TZ_BR)).days <= 365
        ]
        payments_per_year = len(ultimo_ano)

        # Anos pagando consecutivos
        if hist_sorted:
            anos_com_pagamento = set()
            for h in hist_sorted:
                try:
                    ano = int(h.get("paymentDate","")[:4])
                    if ano > 0: anos_com_pagamento.add(ano)
                except: pass
            if anos_com_pagamento:
                ano_atual = agora_ts.year
                years_paying = 0
                for ano in range(ano_atual, ano_atual - 20, -1):
                    if ano in anos_com_pagamento:
                        years_paying += 1
                    else:
                        break

        # Consistência: % de anos com pagamento nos últimos 10 anos
        anos_esperados = min(10, max(1, len(set(
            int(h["paymentDate"][:4]) for h in hist_sorted
            if h.get("paymentDate") and len(h["paymentDate"]) >= 4
        ))))
        if anos_esperados > 0:
            consistency = min(100, years_paying / anos_esperados * 100)

        # Dividendos crescentes: compara média dos últimos 3 anos vs 3 anteriores
        if len(hist_sorted) >= 4:
            try:
                ano_atual = agora_ts.year
                recentes = sum(h.get("rate", 0) or 0 for h in hist_sorted
                    if h.get("paymentDate") and int(h["paymentDate"][:4]) >= ano_atual - 2)
                anteriores = sum(h.get("rate", 0) or 0 for h in hist_sorted
                    if h.get("paymentDate") and ano_atual - 5 <= int(h["paymentDate"][:4]) <= ano_atual - 3)
                if anteriores > 0:
                    growing_dividends = recentes > anteriores
            except: pass

    return {
        "last_dividend_date":    last_div_date,
        "last_dividend_value":   last_div_value,
        "dividend_yield_12m":    dy_12m,
        "dividend_yield_5y":     dy_5y,
        "trailing_annual_rate":  trailing_rate,
        "payout_ratio":          payout,
        "payments_per_year":     payments_per_year,
        "years_paying":          years_paying,
        "growing_dividends":     growing_dividends,
        "dividend_consistency":  consistency,
        "average_yield":         avg_yield,
        "historico":             hist,
    }

# ── Janus Dividend Score ──────────────────────────────────────
def calcular_janus_dividend_score(ind):
    """
    Calcula o Janus Dividend Score (0-100) com os pesos definidos.
    
    Pesos:
        Dividend Yield        15%
        Crescimento           20%
        Consistência          20%
        Payout sustentável    20%
        Cobertura (proxy ROE) 15%
        Anos pagando          10%
    """
    scores = {}

    # 1. Dividend Yield (15%) — normaliza entre 0% e 15%
    dy = ind.get("dividend_yield_12m")
    if dy is not None:
        scores["yield"] = min(100, max(0, (dy / 0.15) * 100))
    
    # 2. Crescimento dos dividendos (20%)
    growing = ind.get("growing_dividends")
    dy_5y   = ind.get("dividend_yield_5y")
    dy_12m  = ind.get("dividend_yield_12m")
    if growing is not None:
        base_crescimento = 70 if growing else 30
        # Bônus se DY atual > média 5 anos
        if dy_12m and dy_5y and dy_5y > 0:
            bonus = min(30, max(-30, (dy_12m - dy_5y) / dy_5y * 100))
            scores["growth"] = min(100, max(0, base_crescimento + bonus))
        else:
            scores["growth"] = base_crescimento

    # 3. Consistência dos pagamentos (20%) — já vem em %
    consistency = ind.get("dividend_consistency")
    if consistency is not None:
        scores["consistency"] = min(100, max(0, consistency))

    # 4. Payout sustentável (20%) — ideal entre 30% e 70%
    payout = ind.get("payout_ratio")
    if payout is not None:
        p = payout * 100  # converte de decimal para %
        if p <= 0:
            scores["payout"] = 0
        elif p <= 30:
            scores["payout"] = p / 30 * 60  # cresce até 60
        elif p <= 70:
            scores["payout"] = 100  # zona ideal
        elif p <= 100:
            scores["payout"] = max(0, 100 - (p - 70) / 30 * 60)
        else:
            scores["payout"] = 0  # payout > 100% insustentável

    # 5. Cobertura (15%) — usamos payments_per_year como proxy
    # Mais pagamentos por ano = mais distribuição = melhor cobertura
    ppy = ind.get("payments_per_year") or 0
    if ppy >= 12:       scores["coverage"] = 100
    elif ppy >= 6:      scores["coverage"] = 85
    elif ppy >= 4:      scores["coverage"] = 70
    elif ppy >= 2:      scores["coverage"] = 55
    elif ppy >= 1:      scores["coverage"] = 40
    else:               scores["coverage"] = 0

    # 6. Anos pagando (10%) — normaliza entre 0 e 20 anos
    years = ind.get("years_paying") or 0
    scores["years"] = min(100, (years / 20) * 100)

    # Pesos
    PESOS = {
        "yield":       0.15,
        "growth":      0.20,
        "consistency": 0.20,
        "payout":      0.20,
        "coverage":    0.15,
        "years":       0.10,
    }

    score_total = 0.0
    peso_total  = 0.0
    for key, peso in PESOS.items():
        if key in scores:
            score_total += scores[key] * peso
            peso_total  += peso

    if peso_total == 0:
        return None, scores

    final = round(score_total / peso_total, 2)
    return final, scores

# ── MAIN ─────────────────────────────────────────────────────
def run_dividend_collector(on_progress=None):
    """
    Coleta dividendos de todos os ativos com asset_id no banco
    e calcula o Janus Dividend Score para cada um.
    """
    def prog(pct, atual, total, msg):
        print(f"[DIVIDEND] {pct}% ({atual}/{total}) {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, atual, total, msg)
            except: pass

    print("[DIVIDEND] 🚀 Janus Dividend Engine v1.0 iniciando...", flush=True)
    conn = get_conn()
    try:
        # Inicializa tabelas
        import db as janus_db
        janus_db.db_init_dividend_tables(conn)

        # Busca todos os ativos
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.asset_id, a.ticker
                FROM assets a
                WHERE a.status = 'ATIVO' AND a.asset_type = 'ACAO'
                ORDER BY a.ticker
            """)
            ativos = [dict(r) for r in cur.fetchall()]

        total = len(ativos)
        prog(0, 0, total, f"Iniciando coleta de {total} ativos...")

        processados = 0
        erros = 0
        com_score = 0

        # Processa em lotes
        total_lotes = (total + LOTE - 1) // LOTE

        for i in range(0, total, LOTE):
            lote = ativos[i:i+LOTE]
            lote_tickers = [a["ticker"] for a in lote]
            lote_num = i // LOTE + 1
            pct = round(i / total * 100)
            prog(pct, i, total, f"Lote {lote_num}/{total_lotes}: {', '.join(lote_tickers[:5])}...")

            time.sleep(DELAY)
            dados_lote = buscar_dividendos_lote(lote_tickers)

            for ativo in lote:
                ticker   = ativo["ticker"]
                asset_id = ativo["asset_id"]
                dados    = dados_lote.get(ticker)

                if not dados:
                    erros += 1
                    continue

                try:
                    # Calcula indicadores
                    ind = calcular_indicadores_dividendos(dados)

                    # Só processa se tiver algum dado de dividendo
                    if not ind["dividend_yield_12m"] and not ind["last_dividend_value"] and not ind["historico"]:
                        continue  # empresa não paga dividendos

                    # Calcula Janus Dividend Score
                    score, sub_scores = calcular_janus_dividend_score(ind)
                    ind["janus_dividend_score"] = score
                    ind["score_yield"]       = sub_scores.get("yield")
                    ind["score_growth"]      = sub_scores.get("growth")
                    ind["score_consistency"] = sub_scores.get("consistency")
                    ind["score_payout"]      = sub_scores.get("payout")
                    ind["score_coverage"]    = sub_scores.get("coverage")

                    # Salva histórico de pagamentos
                    if ind["historico"]:
                        janus_db.db_salvar_dividend_history(conn, asset_id, ticker, ind["historico"])

                    # Salva perfil
                    janus_db.db_salvar_dividend_profile(conn, asset_id, ticker, ind)

                    processados += 1
                    if score is not None:
                        com_score += 1
                        print(f"[DIVIDEND] ✅ {ticker} → Score: {score:.1f} | DY: {(ind['dividend_yield_12m'] or 0)*100:.1f}% | {ind['years_paying']} anos", flush=True)

                except Exception as e:
                    print(f"[DIVIDEND] ❌ Erro {ticker}: {e}", flush=True)
                    erros += 1

        prog(100, total, total, f"Concluído! {processados} ativos processados, {com_score} com score")
        print(f"[DIVIDEND] ✅ Dividend Engine finalizado!", flush=True)
        print(f"[DIVIDEND]    Ativos:      {total}", flush=True)
        print(f"[DIVIDEND]    Processados: {processados}", flush=True)
        print(f"[DIVIDEND]    Com Score:   {com_score}", flush=True)
        print(f"[DIVIDEND]    Erros:       {erros}", flush=True)

    except Exception as e:
        print(f"[DIVIDEND] ❌ Erro fatal: {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    run_dividend_collector()
