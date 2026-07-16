# ============================================================
# JANUS FISCAL — MOTOR DE APURAÇÃO v1.0
# Fase 1: motor determinístico isolado — custo médio ponderado e
# apuração mensal de ações comuns (swing trade). Day trade fica pra
# Fase 3 (precisa de algoritmo de casamento compra/venda ainda a
# definir — ver ressalva na revisão do documento).
#
# Princípios do documento "JANUS FISCAL — Módulo de Venda de Ativos":
#   - RF22: nunca inventar valores ausentes (custos/IRRF faltando geram
#     alerta, não um número silenciosamente assumido como zero-por-padrão
#     onde isso mudaria o resultado fiscal).
#   - RF03: custo médio ponderado (não FIFO) — método exigido pela
#     legislação brasileira pra pessoa física.
#   - RF17/seção 7/22: nada de alíquota/limite hard-coded — tudo entra
#     via ConfiguracaoFiscal, parametrizável e versionável.
#   - RF24: motor 100% determinístico, sem envolvimento de IA.
#
# Sem dependências externas — mesmo princípio do xirr_engine.py.
# ============================================================

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


# ── Configuração fiscal versionada (seção 7, 22) ────────────────
@dataclass
class ConfiguracaoFiscal:
    """
    Nenhum valor aqui deve ser tratado como verdade eterna — a spec é
    explícita que limite/alíquota vêm de uma tabela de regras versionada
    com vigência e fonte normativa (seção 7, 22). Os defaults abaixo são
    os valores de referência citados no documento (seção 8: 15%; seção 7:
    R$20.000), mas o motor NUNCA deve hard-codar isso na lógica — sempre
    recebe via esta configuração, injetável.
    """
    limite_isencao_mensal_acoes: float = 20000.0
    aliquota_swing_trade: float = 0.15
    aliquota_day_trade: float = 0.20  # não usado na Fase 1, reservado pra Fase 3
    minimo_recolhimento_darf: float = 10.0  # seção 8/13: abaixo disso, acumula pro mês seguinte
    versao_regras: str = "2026.1"
    fonte_normativa: str = "Receita Federal — Renda Variável (referência julho/2026)"


# ── Posição fiscal (custo médio ponderado) ───────────────────────
@dataclass
class PosicaoFiscal:
    ticker: str
    quantidade: float
    custo_medio: float   # por ação
    custo_total: float   # quantidade * custo_medio (guardado explicitamente pra evitar erro de arredondamento acumulado)


class ErroValidacaoFiscal(Exception):
    """Erros de validação de entrada (seção 3.2) — venda maior que a
    posição disponível, quantidade/preço inválidos, etc. Nunca deve ser
    silenciosamente ignorado ou "corrigido" pelo motor."""
    pass


def processar_compra(posicao_atual: Optional[PosicaoFiscal], ticker: str,
                      quantidade: float, preco_unitario: float,
                      custos: float = 0.0) -> PosicaoFiscal:
    """
    Seção 5: custos de COMPRA (corretagem, taxas, emolumentos admitidos)
    incorporam o custo de aquisição — aumentam o custo médio.

    Exemplo do documento: compra 100@R$20 + compra 100@R$24 (sem custos)
    -> custo médio R$22.
    """
    if quantidade <= 0:
        raise ErroValidacaoFiscal("Quantidade da compra deve ser maior que zero.")
    if preco_unitario <= 0:
        raise ErroValidacaoFiscal("Preço unitário da compra deve ser maior que zero.")
    if custos < 0:
        raise ErroValidacaoFiscal("Custos não podem ser negativos.")

    custo_total_operacao = quantidade * preco_unitario + custos

    if posicao_atual is None or posicao_atual.quantidade == 0:
        nova_quantidade = quantidade
        novo_custo_total = custo_total_operacao
    else:
        if posicao_atual.ticker != ticker:
            raise ErroValidacaoFiscal(
                f"Posição informada é de {posicao_atual.ticker}, mas a compra é de {ticker}."
            )
        nova_quantidade = posicao_atual.quantidade + quantidade
        novo_custo_total = posicao_atual.custo_total + custo_total_operacao

    novo_custo_medio = novo_custo_total / nova_quantidade if nova_quantidade > 0 else 0.0

    return PosicaoFiscal(
        ticker=ticker,
        quantidade=nova_quantidade,
        custo_medio=novo_custo_medio,
        custo_total=novo_custo_total,
    )


@dataclass
class ResultadoVenda:
    ticker: str
    quantidade_vendida: float
    preco_unitario: float
    valor_bruto: float
    custo_base: float          # quantidade_vendida * custo_médio ANTES da venda
    custos_venda: float        # reduz o lucro, NÃO altera o custo médio (seção 5)
    irrf: float
    resultado_liquido: float   # valor_bruto - custo_base - custos_venda
    posicao_remanescente: PosicaoFiscal


def processar_venda(posicao_atual: PosicaoFiscal, quantidade: float,
                     preco_unitario: float, custos: float = 0.0,
                     irrf: float = 0.0) -> ResultadoVenda:
    """
    Seção 5: custo base da parcela vendida = quantidade * custo médio
    vigente. Seção 6: posição remanescente preserva o custo médio (não
    recalcula na venda, só na compra). Seção 3.2: venda maior que a
    posição disponível é erro, não "venda a descoberto" silenciosa.

    Exemplo do documento: posição a custo médio R$22, venda de 80@R$27
    -> custo base R$1.760.
    """
    if quantidade <= 0:
        raise ErroValidacaoFiscal("Quantidade da venda deve ser maior que zero.")
    if preco_unitario <= 0:
        raise ErroValidacaoFiscal("Preço unitário da venda deve ser maior que zero.")
    if custos < 0 or irrf < 0:
        raise ErroValidacaoFiscal("Custos e IRRF não podem ser negativos.")
    if posicao_atual is None or posicao_atual.quantidade <= 0:
        raise ErroValidacaoFiscal("Não há posição disponível para vender.")
    if quantidade > posicao_atual.quantidade:
        raise ErroValidacaoFiscal(
            f"Venda de {quantidade} unidades excede a posição disponível "
            f"({posicao_atual.quantidade}). Venda a descoberto não suportada."
        )

    valor_bruto = quantidade * preco_unitario
    custo_base = quantidade * posicao_atual.custo_medio
    resultado_liquido = valor_bruto - custo_base - custos

    nova_quantidade = posicao_atual.quantidade - quantidade
    novo_custo_total = posicao_atual.custo_total - custo_base
    if nova_quantidade <= 1e-9:  # zerou a posição (tolerância de ponto flutuante)
        nova_quantidade = 0.0
        novo_custo_medio = 0.0
        novo_custo_total = 0.0
    else:
        novo_custo_medio = posicao_atual.custo_medio  # preservado — seção 6

    posicao_remanescente = PosicaoFiscal(
        ticker=posicao_atual.ticker,
        quantidade=nova_quantidade,
        custo_medio=novo_custo_medio,
        custo_total=novo_custo_total,
    )

    return ResultadoVenda(
        ticker=posicao_atual.ticker,
        quantidade_vendida=quantidade,
        preco_unitario=preco_unitario,
        valor_bruto=valor_bruto,
        custo_base=custo_base,
        custos_venda=custos,
        irrf=irrf,
        resultado_liquido=resultado_liquido,
        posicao_remanescente=posicao_remanescente,
    )


# ── Apuração mensal (seções 8, 9, 12) ────────────────────────────
@dataclass
class ApuracaoMensal:
    ano_mes: str
    total_vendas_acoes: float
    ganho_bruto: float
    perda_bruta: float
    resultado_liquido_mes: float
    isento: bool
    prejuizo_anterior: float
    prejuizo_compensado: float
    base_tributavel: float
    ir_calculado: float
    irrf_disponivel: float
    irrf_utilizado: float
    imposto_devido: float               # calculado ESTE mês, antes do mínimo legal
    imposto_acumulado_anterior: float   # vindo de meses anteriores abaixo do mínimo
    imposto_a_pagar_agora: float        # 0 se ainda abaixo do mínimo — é o valor real do DARF
    imposto_acumulado_novo_saldo: float # o que fica pendente pro próximo mês
    prejuizo_novo_saldo: float
    versao_regras: str
    status: str = "Calculado"


def apurar_mes(vendas_do_mes: List[ResultadoVenda], prejuizo_anterior: float,
                irrf_disponivel: float, ano_mes: str,
                cfg: Optional[ConfiguracaoFiscal] = None,
                imposto_acumulado_anterior: float = 0.0) -> ApuracaoMensal:
    """
    Seção 7: isenção é sobre o TOTAL VENDIDO no mês (alienação), não
    sobre o lucro — "referência de R$20.000,00 é limite de vendas, e não
    limite de lucro". Um mês pode vender pouco mas ainda assim é preciso
    checar o total antes de aplicar a isenção.

    Seção 9: prejuízo compensa até o limite do ganho do mês (nunca gera
    "prejuízo negativo"), e prejuízo NÃO utilizado num mês isento
    permanece intacto pro próximo mês tributável (isenção não "gasta"
    saldo de prejuízo à toa).

    Seção 8/13: imposto abaixo do mínimo legal de recolhimento (default
    R$10) NÃO gera DARF neste mês — acumula com o próximo mês até
    atingir o mínimo. `imposto_acumulado_anterior` é o saldo pendente
    de meses anteriores; `imposto_a_pagar_agora` é o valor real do DARF
    deste mês (0 se ainda não atingiu o mínimo).

    Validado contra os 2 exemplos numéricos do documento:
      seção 9  -> prejuízo 6.000, ganho 9.000, compensa 6.000, base 3.000
      seção 12 -> vendas 27.500, ganhos 6.800, perdas 1.300,
                  prejuízo anterior 2.000, base 3.500, IR 525 (15%)
    """
    cfg = cfg or ConfiguracaoFiscal()
    if prejuizo_anterior < 0:
        raise ErroValidacaoFiscal("Prejuízo anterior não pode ser negativo (é um saldo, não uma dívida).")
    if irrf_disponivel < 0:
        raise ErroValidacaoFiscal("IRRF disponível não pode ser negativo.")
    if imposto_acumulado_anterior < 0:
        raise ErroValidacaoFiscal("Imposto acumulado anterior não pode ser negativo.")

    total_vendas = sum(v.valor_bruto for v in vendas_do_mes)
    ganho_bruto = sum(v.resultado_liquido for v in vendas_do_mes if v.resultado_liquido > 0)
    perda_bruta = sum(-v.resultado_liquido for v in vendas_do_mes if v.resultado_liquido < 0)
    resultado_liquido = ganho_bruto - perda_bruta

    isento = total_vendas <= cfg.limite_isencao_mensal_acoes and resultado_liquido > 0

    if resultado_liquido <= 0:
        # Mês de prejuízo (ou zero): não há base tributável — só engorda
        # o saldo de prejuízo compensável. Isenção não é sequer relevante
        # aqui (não existe "isenção de imposto negativo").
        prejuizo_compensado = 0.0
        base_tributavel = 0.0
        ir_calculado = 0.0
        prejuizo_novo_saldo = prejuizo_anterior + abs(resultado_liquido)
    elif isento:
        # Ganho dentro do limite de alienação — isento, e o saldo de
        # prejuízo permanece intocado (não tem porque "gastar" prejuízo
        # compensando um ganho que já não pagaria imposto de qualquer jeito).
        prejuizo_compensado = 0.0
        base_tributavel = 0.0
        ir_calculado = 0.0
        prejuizo_novo_saldo = prejuizo_anterior
    else:
        prejuizo_compensado = min(prejuizo_anterior, resultado_liquido)
        base_tributavel = resultado_liquido - prejuizo_compensado
        ir_calculado = round(base_tributavel * cfg.aliquota_swing_trade, 2)
        prejuizo_novo_saldo = prejuizo_anterior - prejuizo_compensado

    irrf_utilizado = min(irrf_disponivel, ir_calculado)
    imposto_devido = round(max(0.0, ir_calculado - irrf_utilizado), 2)

    # Mínimo legal de recolhimento (seção 8/13): soma o pendente de meses
    # anteriores; só "libera" o DARF quando o total atinge o mínimo.
    imposto_pendente_total = round(imposto_devido + imposto_acumulado_anterior, 2)
    if imposto_pendente_total < cfg.minimo_recolhimento_darf:
        imposto_a_pagar_agora = 0.0
        imposto_acumulado_novo_saldo = imposto_pendente_total
    else:
        imposto_a_pagar_agora = imposto_pendente_total
        imposto_acumulado_novo_saldo = 0.0

    return ApuracaoMensal(
        ano_mes=ano_mes,
        total_vendas_acoes=total_vendas,
        ganho_bruto=ganho_bruto,
        perda_bruta=perda_bruta,
        resultado_liquido_mes=resultado_liquido,
        isento=isento,
        prejuizo_anterior=prejuizo_anterior,
        prejuizo_compensado=prejuizo_compensado,
        base_tributavel=base_tributavel,
        ir_calculado=ir_calculado,
        irrf_disponivel=irrf_disponivel,
        irrf_utilizado=irrf_utilizado,
        imposto_devido=imposto_devido,
        imposto_acumulado_anterior=imposto_acumulado_anterior,
        imposto_a_pagar_agora=imposto_a_pagar_agora,
        imposto_acumulado_novo_saldo=imposto_acumulado_novo_saldo,
        prejuizo_novo_saldo=round(prejuizo_novo_saldo, 2),
        versao_regras=cfg.versao_regras,
    )


# ── DARF (seção 8) ────────────────────────────────────────────
@dataclass
class DARF:
    ano_mes_referencia: str
    codigo_receita: str
    valor: float
    data_vencimento: str  # ISO (YYYY-MM-DD)
    competencia: str      # MM/AAAA, formato usado no Sicalc


def calcular_vencimento_darf(ano_mes: str) -> str:
    """
    Último dia útil do mês seguinte ao da apuração (seção 8). LIMITAÇÃO
    CONHECIDA: só recua sobre fins de semana, não sobre feriados
    nacionais/estaduais — um calendário de feriados fica pra um
    refinamento futuro (a data aqui pode ficar 1-3 dias adiantada em
    meses com feriado no fim do mês; sempre conferir a data oficial
    antes de pagar).
    """
    from datetime import date, timedelta
    ano, mes = (int(x) for x in ano_mes.split('-'))
    ano_venc, mes_venc = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    primeiro_dia_seguinte = date(ano_venc + 1, 1, 1) if mes_venc == 12 else date(ano_venc, mes_venc + 1, 1)
    ultimo_dia = primeiro_dia_seguinte - timedelta(days=1)
    while ultimo_dia.weekday() >= 5:  # 5=sábado, 6=domingo
        ultimo_dia -= timedelta(days=1)
    return ultimo_dia.isoformat()


def gerar_darf(apuracao: ApuracaoMensal, codigo_receita: str = "6015") -> Optional[DARF]:
    """
    Devolve None se não há nada a pagar ainda (isento, prejuízo, ou
    abaixo do mínimo legal ainda acumulando — ver imposto_a_pagar_agora).
    """
    if apuracao.imposto_a_pagar_agora <= 0:
        return None
    ano, mes = (int(x) for x in apuracao.ano_mes.split('-'))
    return DARF(
        ano_mes_referencia=apuracao.ano_mes,
        codigo_receita=codigo_receita,
        valor=apuracao.imposto_a_pagar_agora,
        data_vencimento=calcular_vencimento_darf(apuracao.ano_mes),
        competencia=f"{mes:02d}/{ano:04d}",
    )
