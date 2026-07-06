# ============================================================
# JANUS AGENDA COLLECTOR v1.0
# Coleta eventos futuros da Camada 1:
#   - Dividendos futuros (ex-date e pagamento)
#   - Resultados de empresas (earningsDate)
#   - Vencimento de opções (3ª sexta-feira do mês)
# ============================================================

import os, time, requests, calendar
from datetime import datetime, timezone, timedelta, date
import psycopg2, psycopg2.extras

TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"
TZ_BR = timezone(timedelta(hours=-3))

def agora(): return datetime.now(TZ_BR)
def hoje():  return agora().date()

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── Vencimento de opções (3ª sexta do mês) ───────────────────
def calcular_vencimentos_opcoes(meses=3):
    """Retorna as próximas N datas de vencimento de opções."""
    hoje_d = hoje()
    vencimentos = []
    ano, mes = hoje_d.year, hoje_d.month
    for _ in range(meses + 1):
        # Acha a 3ª sexta-feira do mês
        cal = calendar.monthcalendar(ano, mes)
        sextas = [s[4] for s in cal if s[4] > 0]  # índice 4 = sexta
        if len(sextas) >= 3:
            terceira_sexta = date(ano, mes, sextas[2])
            if terceira_sexta >= hoje_d:
                vencimentos.append(terceira_sexta)
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return vencimentos[:meses]

# ── Busca dividendos futuros da Brapi ────────────────────────
def buscar_dividendos_futuros(tickers, on_progress=None):
    """Busca próximos dividendos dos ativos via Brapi."""
    eventos = []
    LOTE = 10
    total = len(tickers)
    for i in range(0, total, LOTE):
        lote = tickers[i:i+LOTE]
        if on_progress:
            on_progress(round(i/total*100), f"Dividendos: {','.join(lote[:3])}...")
        try:
            joined = ','.join(lote)
            url = f"{BRAPI_BASE}/quote/{joined}?modules=defaultKeyStatistics&dividends=true&token={TOKEN_BRAPI}"
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
            if r.status_code != 200:
                time.sleep(1)
                continue
            results = r.json().get("results", [])
            for d in results:
                ticker = d.get("symbol","")
                ks = d.get("defaultKeyStatistics") or {}
                hist = d.get("dividendsData", {}).get("cashDividends", []) or []
                # Ex-date futuro
                ex_date = ks.get("exDividendDate")
                last_val = ks.get("lastDividendValue")
                if ex_date:
                    try:
                        dt = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
                        if dt >= hoje():
                            eventos.append({
                                "ticker": ticker,
                                "tipo": "DIVIDENDO",
                                "titulo": f"{ticker} — Ex-date dividendo",
                                "descricao": f"Valor: R$ {last_val:.4f}" if last_val else "Valor a confirmar",
                                "data_evento": dt,
                                "impacto": "ALTO",
                                "valor": last_val
                            })
                    except: pass
                # Próximos pagamentos do histórico
                for pag in (hist or []):
                    try:
                        pay_date = pag.get("paymentDate","")[:10]
                        if not pay_date: continue
                        dt_pay = datetime.strptime(pay_date, "%Y-%m-%d").date()
                        if dt_pay >= hoje():
                            val = pag.get("rate") or pag.get("value") or 0
                            eventos.append({
                                "ticker": ticker,
                                "tipo": "DIVIDENDO",
                                "titulo": f"{ticker} — Pagamento dividendo",
                                "descricao": f"R$ {val:.4f} por ação",
                                "data_evento": dt_pay,
                                "impacto": "MEDIO",
                                "valor": val
                            })
                    except: pass
        except Exception as e:
            print(f"[AGENDA] Erro dividendos {lote}: {e}", flush=True)
        time.sleep(0.3)
    return eventos

# ── Busca earnings date da Brapi ─────────────────────────────
def buscar_earnings_futuros(tickers, on_progress=None):
    """Busca próximas datas de resultado das empresas."""
    eventos = []
    LOTE = 10
    total = len(tickers)
    for i in range(0, total, LOTE):
        lote = tickers[i:i+LOTE]
        if on_progress:
            on_progress(round(i/total*50 + 50), f"Balanços: {','.join(lote[:3])}...")
        try:
            joined = ','.join(lote)
            url = f"{BRAPI_BASE}/quote/{joined}?modules=defaultKeyStatistics,calendarEvents&token={TOKEN_BRAPI}"
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30)
            if r.status_code != 200:
                time.sleep(1)
                continue
            results = r.json().get("results", [])
            for d in results:
                ticker = d.get("symbol","")
                ks = d.get("defaultKeyStatistics") or {}
                cal_ev = d.get("calendarEvents") or {}
                # Earnings date
                earnings = cal_ev.get("earnings", {})
                if earnings:
                    dates = earnings.get("earningsDate", [])
                    for ed in (dates if isinstance(dates, list) else [dates]):
                        try:
                            dt = datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
                            if dt >= hoje():
                                eventos.append({
                                    "ticker": ticker,
                                    "tipo": "BALANCO",
                                    "titulo": f"{ticker} — Resultado esperado",
                                    "descricao": "Divulgação de resultados trimestrais",
                                    "data_evento": dt,
                                    "impacto": "ALTO",
                                    "valor": None
                                })
                        except: pass
        except Exception as e:
            print(f"[AGENDA] Erro earnings {lote}: {e}", flush=True)
        time.sleep(0.3)
    return eventos

# ── MAIN ─────────────────────────────────────────────────────
def run_agenda_collector(on_progress=None):
    def prog(pct, msg):
        print(f"[AGENDA] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, msg)
            except: pass

    print("[AGENDA] 📅 Agenda Collector v1.0 iniciando...", flush=True)
    conn = get_conn()
    try:
        import db as janus_db
        janus_db.db_init_agenda_tables(conn)

        # Busca apenas ativos que já têm histórico de dividendos
        # (muito mais rápido que buscar todos os 500+)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT a.ticker
                FROM assets a
                INNER JOIN dividend_profile dp ON dp.asset_id = a.asset_id
                WHERE a.status = 'ATIVO'
                  AND dp.dividend_yield_12m > 0
                ORDER BY a.ticker
                LIMIT 200
            """)
            tickers = [r['ticker'] for r in cur.fetchall()]

        # Se não tiver dividend_profile ainda, pega todos
        if not tickers:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT ticker FROM assets
                    WHERE status = 'ATIVO' AND asset_type = 'ACAO'
                    ORDER BY ticker LIMIT 100
                """)
                tickers = [r['ticker'] for r in cur.fetchall()]

        prog(0, f"Iniciando coleta de {len(tickers)} ativos...")

        # 1. Vencimentos de opções (calculado localmente)
        vencimentos = calcular_vencimentos_opcoes(meses=3)
        for venc in vencimentos:
            janus_db.db_salvar_agenda_item(
                conn, None, 'OPCOES',
                f'Vencimento de Opções — {venc.strftime("%B/%Y")}',
                venc, 'Vencimento mensal de opções na B3 (3ª sexta-feira)',
                'MEDIO', None, 'AUTO'
            )
        prog(5, f"{len(vencimentos)} vencimentos de opções calculados")

        # 2. Dividendos futuros
        prog(10, "Buscando dividendos futuros...")
        eventos_div = buscar_dividendos_futuros(tickers,
            on_progress=lambda p, m: prog(10 + int(p*0.4), m))
        for ev in eventos_div:
            janus_db.db_salvar_agenda_item(
                conn, ev['ticker'], ev['tipo'], ev['titulo'],
                ev['data_evento'], ev['descricao'],
                ev['impacto'], ev['valor'], 'BRAPI'
            )
        prog(50, f"{len(eventos_div)} eventos de dividendos encontrados")

        # 3. Earnings futuros
        prog(50, "Buscando datas de balanços...")
        eventos_earn = buscar_earnings_futuros(tickers,
            on_progress=lambda p, m: prog(50 + int(p*0.4), m))
        for ev in eventos_earn:
            janus_db.db_salvar_agenda_item(
                conn, ev['ticker'], ev['tipo'], ev['titulo'],
                ev['data_evento'], ev['descricao'],
                ev['impacto'], ev['valor'], 'BRAPI'
            )
        prog(90, f"{len(eventos_earn)} datas de balanços encontradas")

        total_eventos = len(vencimentos) + len(eventos_div) + len(eventos_earn)
        prog(100, f"Concluído! {total_eventos} eventos na agenda")
        print(f"[AGENDA] ✅ Agenda atualizada: {total_eventos} eventos", flush=True)

    except Exception as e:
        print(f"[AGENDA] ❌ Erro fatal: {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    run_agenda_collector()
