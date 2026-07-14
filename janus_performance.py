# ============================================================
# JANUS PERFORMANCE — CÁLCULO DE PERFORMANCE v1.0
# Fase 2: XIRR da carteira geral vs CDI.
#
# A comparação com o benchmark NÃO usa uma taxa anual fixa (ex: "CDI do
# ano"). Em vez disso, simula uma "carteira sombra": os MESMOS fluxos de
# caixa (mesmas datas, mesmos valores de aporte/retirada) são aplicados
# dia a dia sobre o fator acumulado do benchmark. O saldo final dessa
# carteira sombra é comparável diretamente ao valor atual da carteira
# real — é a forma correta de responder "será que eu teria saído melhor
# só deixando esse dinheiro no CDI?".
# ============================================================

from dataclasses import dataclass
from datetime import date, timedelta, datetime, timezone
from typing import List, Dict, Optional

from xirr_engine import FluxoCaixa, XirrResult, XirrStatus, calcular_xirr

TZ_BR = timezone(timedelta(hours=-3))
def hoje() -> date: return datetime.now(TZ_BR).date()


@dataclass
class ResultadoPerformance:
    xirr: XirrResult
    saldo_benchmark: Optional[float]           # quanto teria hoje se os mesmos fluxos fossem no benchmark
    valor_atual_real: Optional[float]
    diferenca_absoluta: Optional[float]        # valor_atual_real - saldo_benchmark
    diferenca_percentual: Optional[float]      # diferenca_absoluta / saldo_benchmark
    codigo_benchmark: str
    dias_sem_fator: int = 0                    # dias dentro do período sem fator no banco (alerta de qualidade de dado)


def _preencher_fatores_faltantes(fatores: Dict[date, float], data_inicial: date, data_final: date) -> tuple:
    """
    O BCB só publica CDI/SELIC em dia útil. Fins de semana e feriados não
    têm entrada própria no banco. Convenção padrão de mercado: nesses
    dias, usa-se o último fator de dia útil conhecido (o dinheiro não
    'para de render' no fim de semana, só não há divulgação nova).

    Devolve (fatores_completos, dias_sem_fator_algum) — o segundo valor
    serve de alerta de qualidade de dado: se muitos dias não tiverem
    fator nem por preenchimento, o coletor de benchmarks provavelmente
    não rodou ainda para aquele período.
    """
    completos = {}
    ultimo_fator = None
    dias_sem_fator = 0
    d = data_inicial
    while d <= data_final:
        if d in fatores:
            ultimo_fator = fatores[d]
            completos[d] = ultimo_fator
        elif ultimo_fator is not None:
            completos[d] = ultimo_fator  # fim de semana/feriado — carrega o último dia útil
        else:
            dias_sem_fator += 1  # nem isso — não há dado nenhum ainda pra essa data
        d += timedelta(days=1)
    return completos, dias_sem_fator


def simular_saldo_benchmark(fluxos: List[FluxoCaixa], fatores_diarios: Dict[date, float],
                             data_final: Optional[date] = None) -> tuple:
    """
    Simula a 'carteira sombra': aplica os mesmos aportes/retiradas da
    carteira real sobre o fator acumulado diário do benchmark.

    fluxos: mesma convenção do xirr_engine — valor < 0 = aporte/compra
            (dinheiro saindo do bolso, entrando na carteira sombra),
            valor > 0 = retirada/venda (dinheiro saindo da carteira
            sombra, voltando pro bolso).
    fatores_diarios: dict {data: fator_diario} já com fins de
            semana/feriados preenchidos (ver _preencher_fatores_faltantes).

    Devolve (saldo_final, dias_sem_fator).
    """
    if not fluxos:
        return 0.0, 0

    data_final = data_final or hoje()
    fluxos_ordenados = sorted(fluxos, key=lambda f: f.data)
    data_inicial = fluxos_ordenados[0].data

    fatores_completos, dias_sem_fator = _preencher_fatores_faltantes(
        fatores_diarios, data_inicial, data_final
    )

    saldo = 0.0
    idx_fluxo = 0
    d = data_inicial
    while d < data_final:
        # aplica os fluxos que caem neste dia ANTES de capitalizar —
        # dinheiro que entra hoje já rende a partir de hoje
        while idx_fluxo < len(fluxos_ordenados) and fluxos_ordenados[idx_fluxo].data == d:
            saldo += -fluxos_ordenados[idx_fluxo].valor  # compra(-) vira depósito(+) na sombra
            idx_fluxo += 1
        fator = fatores_completos.get(d, 1.0)
        saldo *= fator
        d += timedelta(days=1)

    # Fluxos que caem exatamente em data_final entram no saldo sem
    # capitalizar mais — dá zero dias decorridos, igual à convenção do
    # xirr_engine ((data_final - data_inicial).days). Isso mantém as duas
    # contas (XIRR e simulação de benchmark) usando a MESMA contagem de
    # dias, o que importa pra comparação ser justa.
    while idx_fluxo < len(fluxos_ordenados) and fluxos_ordenados[idx_fluxo].data == data_final:
        saldo += -fluxos_ordenados[idx_fluxo].valor
        idx_fluxo += 1

    return saldo, dias_sem_fator


def calcular_performance(fluxos: List[FluxoCaixa], valor_atual: float,
                          fatores_diarios: Dict[date, float],
                          codigo_benchmark: str = "CDI",
                          data_referencia: Optional[date] = None) -> ResultadoPerformance:
    """
    Ponto de entrada principal: recebe os fluxos de caixa reais (sem o
    valor atual ainda) + o valor atual da posição/carteira, e devolve
    o XIRR pessoal junto com a comparação contra o benchmark.

    data_referencia: data considerada "hoje" para o cálculo — default é
    a data real atual, mas pode ser fixada para recalcular retroativamente
    (auditoria, reprocessamento) ou para testes determinísticos.
    """
    data_ref = data_referencia or hoje()
    fluxos_completos = list(fluxos)
    if valor_atual and valor_atual > 0:
        fluxos_completos.append(FluxoCaixa(data_ref, valor_atual))

    xirr = calcular_xirr(fluxos_completos)

    saldo_benchmark = None
    diferenca_abs = None
    diferenca_pct = None
    dias_sem_fator = 0

    if fluxos:  # a simulação de benchmark usa só os fluxos de aporte/retirada, não o valor atual
        saldo_benchmark, dias_sem_fator = simular_saldo_benchmark(fluxos, fatores_diarios, data_ref)
        if saldo_benchmark and saldo_benchmark > 0 and valor_atual is not None:
            diferenca_abs = valor_atual - saldo_benchmark
            diferenca_pct = diferenca_abs / saldo_benchmark

    return ResultadoPerformance(
        xirr=xirr,
        saldo_benchmark=saldo_benchmark,
        valor_atual_real=valor_atual,
        diferenca_absoluta=diferenca_abs,
        diferenca_percentual=diferenca_pct,
        codigo_benchmark=codigo_benchmark,
        dias_sem_fator=dias_sem_fator,
    )
