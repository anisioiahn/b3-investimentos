# ============================================================
# JANUS PERFORMANCE — TESTES DO MÓDULO DE CÁLCULO
# Cobre simular_saldo_benchmark() e calcular_performance().
# Rodar com: python3 test_janus_performance.py
# ============================================================

from datetime import date, timedelta
import sys

from xirr_engine import FluxoCaixa, XirrStatus
import janus_performance as jp

_falhas = []
_total = 0

def teste(nome):
    def wrap(fn):
        def executar():
            global _total
            _total += 1
            try:
                fn()
                print(f"✅ {nome}")
            except AssertionError as e:
                _falhas.append(nome)
                print(f"❌ {nome} — {e}")
            except Exception as e:
                _falhas.append(nome)
                print(f"❌ {nome} — ERRO INESPERADO: {type(e).__name__}: {e}")
        executar()
        return fn
    return wrap


@teste("fator constante — bate exatamente com juros compostos manuais")
def _():
    fator = 1.0004
    inicio = date(2026, 1, 1)
    final = inicio + timedelta(days=100)
    fatores = {inicio + timedelta(days=i): fator for i in range(150)}
    fluxos = [FluxoCaixa(inicio, -1000.0)]
    saldo, dias_sem = jp.simular_saldo_benchmark(fluxos, fatores, final)
    esperado = 1000.0 * (fator ** 100)
    assert abs(saldo - esperado) < 0.0001, f"{saldo} != {esperado}"
    assert dias_sem == 0


@teste("preenchimento de fim de semana/feriado carrega o último fator útil")
def _():
    fatores = {date(2026, 1, 2): 1.0004, date(2026, 1, 5): 1.0004}
    fluxos = [FluxoCaixa(date(2026, 1, 2), -1000.0)]
    saldo, _ = jp.simular_saldo_benchmark(fluxos, fatores, date(2026, 1, 5))
    esperado = 1000.0 * (1.0004 ** 3)  # 3 dias decorridos
    assert abs(saldo - esperado) < 0.0001


@teste("retirada reduz o saldo simulado corretamente")
def _():
    fatores = {date(2026, 1, 1) + timedelta(days=i): 1.0 for i in range(10)}
    fluxos = [
        FluxoCaixa(date(2026, 1, 1), -1000.0),
        FluxoCaixa(date(2026, 1, 5), 400.0),
    ]
    saldo, _ = jp.simular_saldo_benchmark(fluxos, fatores, date(2026, 1, 9))
    assert abs(saldo - 600.0) < 0.01


@teste("cenário completo — investidor bateu o benchmark (25% vs 12%)")
def _():
    aporte, hoje = date(2025, 7, 13), date(2026, 7, 13)
    fator = (1.12) ** (1 / 365)
    fatores = {aporte + timedelta(days=i): fator for i in range(370)}
    fluxos = [FluxoCaixa(aporte, -10000.0)]
    r = jp.calcular_performance(fluxos, valor_atual=12500.0, fatores_diarios=fatores, data_referencia=hoje)
    assert r.xirr.status == XirrStatus.VALID
    assert abs(r.xirr.taxa - 0.25) < 1e-9
    assert abs(r.saldo_benchmark - 11200) < 0.01
    assert r.diferenca_absoluta > 0
    assert abs(r.diferenca_percentual - (1300 / 11200)) < 1e-6


@teste("cenário completo — investidor perdeu do benchmark")
def _():
    aporte, hoje = date(2025, 7, 13), date(2026, 7, 13)
    fator = (1.12) ** (1 / 365)
    fatores = {aporte + timedelta(days=i): fator for i in range(370)}
    fluxos = [FluxoCaixa(aporte, -10000.0)]
    r = jp.calcular_performance(fluxos, valor_atual=10800.0, fatores_diarios=fatores, data_referencia=hoje)
    assert r.diferenca_absoluta < 0
    assert r.diferenca_percentual < 0


@teste("sem valor atual não trava — devolve INSUFFICIENT_CASH_FLOWS")
def _():
    fluxos = [FluxoCaixa(date(2025, 1, 1), -1000.0)]
    fatores = {date(2025, 1, 1): 1.0}
    r = jp.calcular_performance(fluxos, valor_atual=0, fatores_diarios=fatores,
                                 data_referencia=date(2026, 1, 1))
    assert r.xirr.status == XirrStatus.INSUFFICIENT_CASH_FLOWS


@teste("dias sem fator de benchmark é sinalizado (coletor sem dado pro período)")
def _():
    fluxos = [FluxoCaixa(date(2020, 1, 1), -1000.0)]
    fatores = {date(2026, 1, 1): 1.0004}  # não cobre 2020
    r = jp.calcular_performance(fluxos, valor_atual=1200.0, fatores_diarios=fatores,
                                 data_referencia=date(2026, 7, 13))
    assert r.dias_sem_fator > 0


@teste("múltiplos aportes em datas diferentes são simulados corretamente")
def _():
    inicio = date(2026, 1, 1)
    fim = date(2026, 4, 1)  # 90 dias
    fator = 1.0002
    fatores = {inicio + timedelta(days=i): fator for i in range(120)}
    fluxos = [
        FluxoCaixa(date(2026, 1, 1), -5000.0),
        FluxoCaixa(date(2026, 2, 1), -3000.0),
    ]
    saldo, _ = jp.simular_saldo_benchmark(fluxos, fatores, fim)
    dias_primeiro = (fim - date(2026, 1, 1)).days
    dias_segundo = (fim - date(2026, 2, 1)).days
    esperado = 5000 * (fator ** dias_primeiro) + 3000 * (fator ** dias_segundo)
    assert abs(saldo - esperado) < 0.01, f"{saldo} != {esperado}"


@teste("lista de fluxos vazia não trava")
def _():
    saldo, dias = jp.simular_saldo_benchmark([], {}, date(2026, 1, 1))
    assert saldo == 0.0
    assert dias == 0


if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"RESULTADO: {_total - len(_falhas)}/{_total} testes passaram")
    if _falhas:
        print(f"FALHARAM: {_falhas}")
        sys.exit(1)
    print("🎉 TODOS OS TESTES PASSARAM")
    sys.exit(0)
