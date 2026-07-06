# ============================================================
# JANUS AGENDA MACRO — Camada 2
# Popula datas de eventos macroeconômicos conhecidos
# COPOM, IPCA, IGP-M, Payroll EUA, FED
# ============================================================

import os
from datetime import date, timedelta
import calendar
import psycopg2, psycopg2.extras

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── Datas conhecidas 2026 ────────────────────────────────────

# COPOM 2026 — datas oficiais divulgadas pelo BCB
COPOM_2026 = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 5,  7),
    date(2026, 6, 18),
    date(2026, 7, 30),
    date(2026, 9, 17),
    date(2026, 10, 29),
    date(2026, 12,  9),
]

# FED (FOMC) 2026 — datas oficiais do Federal Reserve
FED_2026 = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 4, 30),
    date(2026, 6, 11),
    date(2026, 7, 30),
    date(2026, 9, 17),
    date(2026, 10, 29),
    date(2026, 12, 10),
]

def calcular_ipca(ano, mes):
    """IPCA é divulgado no 9º dia útil do mês seguinte."""
    # Mês seguinte
    if mes == 12:
        ano_ref, mes_ref = ano + 1, 1
    else:
        ano_ref, mes_ref = ano, mes + 1
    # Conta dias úteis (seg-sex, sem feriados nacionais simplificado)
    dias_uteis = 0
    d = date(ano_ref, mes_ref, 1)
    while True:
        if d.weekday() < 5:  # seg=0, sex=4
            dias_uteis += 1
            if dias_uteis == 9:
                return d
        d += timedelta(days=1)

def calcular_igpm(ano, mes):
    """IGP-M é divulgado no último dia útil do mês."""
    ultimo = date(ano, mes, calendar.monthrange(ano, mes)[1])
    while ultimo.weekday() >= 5:
        ultimo -= timedelta(days=1)
    return ultimo

def calcular_payroll(ano, mes):
    """Payroll EUA — 1ª sexta-feira do mês."""
    d = date(ano, mes, 1)
    while d.weekday() != 4:  # 4 = sexta
        d += timedelta(days=1)
    return d

def popular_agenda_macro():
    """Insere todos os eventos macro no banco."""
    print("[MACRO] 📅 Populando agenda macro 2026...", flush=True)
    conn = get_conn()

    import db as janus_db
    janus_db.db_init_agenda_tables(conn)

    eventos = []
    hoje = date.today()

    # COPOM
    for dt in COPOM_2026:
        eventos.append({
            'ticker': None, 'tipo': 'MACRO',
            'titulo': 'COPOM — Decisão de Juros',
            'descricao': 'Reunião do Comitê de Política Monetária — decisão sobre a Selic',
            'data_evento': dt, 'impacto': 'ALTO', 'valor': None, 'fonte': 'BCB'
        })

    # FED
    for dt in FED_2026:
        eventos.append({
            'ticker': None, 'tipo': 'MACRO',
            'titulo': 'FED — Decisão de Juros EUA',
            'descricao': 'Reunião do FOMC — decisão sobre a taxa de juros americana',
            'data_evento': dt, 'impacto': 'ALTO', 'valor': None, 'fonte': 'FED'
        })

    # IPCA, IGP-M e Payroll para cada mês do ano
    for mes in range(1, 13):
        # IPCA
        dt_ipca = calcular_ipca(2026, mes)
        eventos.append({
            'ticker': None, 'tipo': 'MACRO',
            'titulo': f'IPCA — Inflação {_nome_mes(mes)}/2026',
            'descricao': f'Divulgação do IPCA de {_nome_mes(mes)} pelo IBGE (9º dia útil)',
            'data_evento': dt_ipca, 'impacto': 'ALTO', 'valor': None, 'fonte': 'IBGE'
        })

        # IGP-M
        dt_igpm = calcular_igpm(2026, mes)
        eventos.append({
            'ticker': None, 'tipo': 'MACRO',
            'titulo': f'IGP-M — {_nome_mes(mes)}/2026',
            'descricao': f'Divulgação do IGP-M de {_nome_mes(mes)} pela FGV',
            'data_evento': dt_igpm, 'impacto': 'MEDIO', 'valor': None, 'fonte': 'FGV'
        })

        # Payroll
        dt_payroll = calcular_payroll(2026, mes)
        eventos.append({
            'ticker': None, 'tipo': 'MACRO',
            'titulo': f'Payroll EUA — {_nome_mes(mes)}/2026',
            'descricao': 'Relatório de empregos nos EUA (Non-Farm Payrolls)',
            'data_evento': dt_payroll, 'impacto': 'ALTO', 'valor': None, 'fonte': 'BLS'
        })

    # Salva no banco
    salvos = 0
    for ev in eventos:
        try:
            janus_db.db_salvar_agenda_item(
                conn, ev['ticker'], ev['tipo'], ev['titulo'],
                ev['data_evento'], ev['descricao'],
                ev['impacto'], ev['valor'], ev['fonte']
            )
            salvos += 1
            status = "✅" if ev['data_evento'] >= hoje else "📦"
            print(f"[MACRO] {status} {ev['data_evento']} — {ev['titulo']}", flush=True)
        except Exception as e:
            print(f"[MACRO] ❌ Erro: {e}", flush=True)

    conn.close()
    print(f"[MACRO] ✅ {salvos} eventos macro salvos!", flush=True)

def _nome_mes(mes):
    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    return nomes[mes-1]

if __name__ == "__main__":
    popular_agenda_macro()
