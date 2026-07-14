# ============================================================
# JANUS PERFORMANCE — BENCHMARKS COLLECTOR v1.0
# Fase 0 do módulo Janus Performance: coleta CDI, SELIC e IPCA
# via API do Banco Central (SGS) e grava fator diário acumulável.
#
# Séries SGS confirmadas (Banco Central, documentação oficial):
#   CDI diário            -> série 12
#   SELIC diária (efetiva) -> série 11
#   IPCA (variação % mensal) -> série 433
#
# Endpoint: https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados
#           ?formato=json&dataInicial=DD/MM/AAAA&dataFinal=DD/MM/AAAA
#
# Desde 26/03/2025 o BCB exige filtro de data OBRIGATÓRIO, com limite
# de 10 anos por requisição — por isso o backfill histórico pagina
# em janelas de até 9 anos (folga de segurança).
#
# IPCA é publicado MENSALMENTE (não existe "IPCA diário" real). Para
# ter uma curva compatível com CDI/SELIC (que são diários), o valor
# mensal é interpolado em fator diário composto (pro-rata geométrico),
# não simples/linear — ver interpolar_ipca_diario().
# ============================================================

import os, time, requests
from datetime import datetime, date, timedelta, timezone
import psycopg2, psycopg2.extras

BCB_BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
SERIES = {
    "CDI":   12,   # taxa diária, % ao dia
    "SELIC": 11,   # taxa diária efetiva, % ao dia
    "IPCA":  433,  # variação % no mês
}
JANELA_ANOS_MAX = 9  # BCB limita a 10 anos por chamada; 9 dá folga de segurança
TIMEOUT = 30
TZ_BR = timezone(timedelta(hours=-3))

def agora():    return datetime.now(TZ_BR)
def hoje_str(): return agora().strftime("%Y-%m-%d")

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")


# ── Init da tabela ───────────────────────────────────────────
def init_tabela(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_diario (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                codigo_benchmark TEXT NOT NULL,
                valor_indice NUMERIC,
                taxa_diaria NUMERIC,
                fator_diario NUMERIC NOT NULL,
                fonte TEXT,
                data_hora_atualizacao TEXT,
                UNIQUE(data, codigo_benchmark)
            );
            CREATE INDEX IF NOT EXISTS idx_benchmark_data
                ON benchmark_diario(codigo_benchmark, data);
        """)
    conn.commit()


# ── Busca bruta na API do BCB ────────────────────────────────
def _parse_valor(v):
    """BCB às vezes retorna número com vírgula decimal dependendo do
    encoding/proxy no meio do caminho — trata os dois formatos."""
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    if ',' in s and '.' not in s:
        s = s.replace(',', '.')
    try: return float(s)
    except: return None

def buscar_serie_bcb(codigo_serie, data_inicial, data_final):
    """
    Busca uma série do SGS entre duas datas (inclusive), paginando
    automaticamente em janelas de até 9 anos (limite do BCB é 10).
    Retorna lista de {"data": date, "valor": float}, ordenada por data.
    """
    resultado = []
    inicio = data_inicial
    janela_dias = JANELA_ANOS_MAX * 365  # aritmética simples de dias evita
                                          # o caso de borda de 29/fev cair
                                          # num ano não-bissexto ao somar anos
    while inicio <= data_final:
        fim_janela = min(data_final, inicio + timedelta(days=janela_dias))

        url = BCB_BASE.format(codigo=codigo_serie)
        params = {
            "formato": "json",
            "dataInicial": inicio.strftime("%d/%m/%Y"),
            "dataFinal": fim_janela.strftime("%d/%m/%Y"),
        }
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                for item in r.json():
                    try:
                        d = datetime.strptime(item["data"], "%d/%m/%Y").date()
                    except Exception:
                        continue
                    valor = _parse_valor(item.get("valor"))
                    if valor is not None:
                        resultado.append({"data": d, "valor": valor})
            else:
                print(f"[BENCHMARKS] ⚠️ Série {codigo_serie} status {r.status_code}: {r.text[:150]}", flush=True)
        except Exception as e:
            print(f"[BENCHMARKS] ⚠️ Erro série {codigo_serie} ({inicio}–{fim_janela}): {e}", flush=True)

        time.sleep(0.3)  # cortesia com a API do BCB
        inicio = fim_janela + timedelta(days=1)

    resultado.sort(key=lambda x: x["data"])
    return resultado


# ── IPCA: interpolação de mensal para fator diário ───────────
def interpolar_ipca_diario(pontos_mensais):
    """
    pontos_mensais: lista de {"data": date (dia 1 do mês de referência), "valor": float (% no mês)}
    Gera fator diário composto (pro-rata geométrico) para cada dia corrido
    dentro de cada mês de referência: fator_dia = (1 + valor_mes/100) ** (1/n_dias_do_mes).

    Isso é uma APROXIMAÇÃO — o IPCA não tem valor diário real. É a prática
    padrão de mercado para poder comparar com séries diárias (CDI/SELIC)
    sem distorcer no fim do mês.
    """
    saida = []
    for p in pontos_mensais:
        ano, mes = p["data"].year, p["data"].month
        variacao_mes = p["valor"] / 100.0  # ex.: 0.44 -> 0.0044

        if mes == 12:
            proximo_mes = date(ano + 1, 1, 1)
        else:
            proximo_mes = date(ano, mes + 1, 1)
        n_dias = (proximo_mes - date(ano, mes, 1)).days

        fator_mensal = 1.0 + variacao_mes
        # fator diário composto: (1+i_mes)^(1/n) - geometricamente correto,
        # NÃO usar i_mes/n (isso seria simples/linear e distorce o acumulado)
        fator_dia = fator_mensal ** (1.0 / n_dias)
        taxa_dia_pct = (fator_dia - 1.0) * 100.0

        for d in range(n_dias):
            dia = date(ano, mes, 1) + timedelta(days=d)
            saida.append({
                "data": dia,
                "valor_indice": None,
                "taxa_diaria": round(taxa_dia_pct, 8),
                "fator_diario": round(fator_dia, 10),
            })
    return saida


# ── Salvar em lote ────────────────────────────────────────────
def salvar_benchmark_lote(conn, codigo_benchmark, pontos, fonte="BCB-SGS"):
    """
    pontos: lista de dicts com pelo menos 'data' e 'taxa_diaria' (CDI/SELIC)
    OU 'data', 'taxa_diaria', 'fator_diario' já calculado (IPCA interpolado).
    Grava em lote com execute_values — mesmo padrão já validado no
    Janus Index e no Dividend Engine (evita N INSERTs individuais).
    """
    if not pontos: return 0
    now = agora().isoformat()
    rows = []
    for p in pontos:
        taxa = p.get("taxa_diaria")
        fator = p.get("fator_diario")
        if fator is None and taxa is not None:
            fator = 1.0 + taxa / 100.0
        if fator is None:
            continue
        rows.append((
            p["data"], codigo_benchmark,
            p.get("valor_indice"), taxa, fator,
            fonte, now
        ))
    if not rows: return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO benchmark_diario
                (data, codigo_benchmark, valor_indice, taxa_diaria, fator_diario, fonte, data_hora_atualizacao)
            VALUES %s
            ON CONFLICT (data, codigo_benchmark) DO UPDATE SET
                valor_indice = EXCLUDED.valor_indice,
                taxa_diaria  = EXCLUDED.taxa_diaria,
                fator_diario = EXCLUDED.fator_diario,
                data_hora_atualizacao = EXCLUDED.data_hora_atualizacao
        """, rows)
    conn.commit()
    return len(rows)


# ── Ponto de entrada ──────────────────────────────────────────
def run_benchmarks_collector(dias_historico=3650, on_progress=None):
    """
    dias_historico: quantos dias corridos pra trás buscar no primeiro run
    (default ~10 anos). Em runs seguintes (cron diário), o ideal é chamar
    com dias_historico pequeno (ex: 10) pra só atualizar os dias recentes —
    ON CONFLICT já trata re-execução sem duplicar.
    """
    def prog(pct, msg):
        print(f"[BENCHMARKS] {pct}% {msg}", flush=True)
        if on_progress:
            try: on_progress(pct, msg)
            except: pass

    print("[BENCHMARKS] 🚀 Benchmarks Collector v1.0 iniciando...", flush=True)
    conn = get_conn()
    try:
        init_tabela(conn)
        data_final = agora().date()
        data_inicial = data_final - timedelta(days=dias_historico)

        total_gravados = {}

        # CDI e SELIC — diários, direto da API
        for i, codigo in enumerate(["CDI", "SELIC"]):
            prog(10 + i * 30, f"Buscando {codigo} ({data_inicial} a {data_final})...")
            pontos = buscar_serie_bcb(SERIES[codigo], data_inicial, data_final)
            formatados = [{"data": p["data"], "taxa_diaria": p["valor"]} for p in pontos]
            n = salvar_benchmark_lote(conn, codigo, formatados)
            total_gravados[codigo] = n
            print(f"[BENCHMARKS] ✅ {codigo}: {n} dias gravados", flush=True)

        # IPCA — mensal, interpolado pra diário
        prog(70, f"Buscando IPCA ({data_inicial} a {data_final})...")
        pontos_ipca = buscar_serie_bcb(SERIES["IPCA"], data_inicial, data_final)
        pontos_ipca_diario = interpolar_ipca_diario(pontos_ipca)
        n_ipca = salvar_benchmark_lote(conn, "IPCA", pontos_ipca_diario, fonte="BCB-SGS (interpolado)")
        total_gravados["IPCA"] = n_ipca
        print(f"[BENCHMARKS] ✅ IPCA: {n_ipca} dias gravados (interpolado de {len(pontos_ipca)} pontos mensais)", flush=True)

        prog(100, "Concluído!")
        print(f"[BENCHMARKS] ✅ Benchmarks Collector finalizado! {total_gravados}", flush=True)
        return total_gravados

    except Exception as e:
        print(f"[BENCHMARKS] ❌ Erro fatal: {e}", flush=True)
        return {}
    finally:
        conn.close()

if __name__ == "__main__":
    import sys
    # Uso:
    #   python benchmarks_collector.py            -> backfill completo (~10 anos, rodar manualmente 1x)
    #   python benchmarks_collector.py 15         -> só os últimos 15 dias (uso do cron diário)
    dias = int(sys.argv[1]) if len(sys.argv) > 1 else 3650
    run_benchmarks_collector(dias_historico=dias)
