# ============================================================
# JANUS FISCAL — TESTES DO MOTOR DE APURAÇÃO
# Valida contra os exemplos numéricos exatos do documento "JANUS FISCAL
# — Módulo de Venda de Ativos" (seções 5, 9, 12), mais casos de borda
# e fuzzing.
#
# Rodar com: python3 test_fiscal_engine.py
# ============================================================

import random
import sys

from fiscal_engine import (
    processar_compra, processar_venda, apurar_mes,
    PosicaoFiscal, ConfiguracaoFiscal, ErroValidacaoFiscal,
    calcular_vencimento_darf, gerar_darf,
)

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


# ── Exemplos exatos do documento ──────────────────────────────
@teste("DOC seção 5: custo médio 100@20 + 100@24 = R$22, venda 80@27 -> custo base R$1.760")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    pos = processar_compra(pos, "PETR4", 100, 24.0)
    assert pos.custo_medio == 22.0
    venda = processar_venda(pos, 80, 27.0)
    assert venda.custo_base == 1760.0
    assert venda.posicao_remanescente.quantidade == 120
    assert venda.posicao_remanescente.custo_medio == 22.0, "custo médio deve ser preservado na posição remanescente"


@teste("DOC seção 9: prejuízo R$6.000 compensa ganho R$9.000 -> base R$3.000")
def _():
    pos = processar_compra(None, "TEST3", 1000, 21.0)
    venda = processar_venda(pos, 1000, 30.0)
    assert venda.resultado_liquido == 9000.0
    apuracao = apurar_mes([venda], prejuizo_anterior=6000.0, irrf_disponivel=0.0, ano_mes="2026-05")
    assert apuracao.prejuizo_compensado == 6000.0
    assert apuracao.base_tributavel == 3000.0


@teste("DOC seção 12: fechamento completo -> IR R$525 (vendas 27.500, ganho 6.800, perda 1.300, prej.ant 2.000)")
def _():
    pos_a = processar_compra(None, "AAAA3", 1000, 10.0)
    venda_ganho = processar_venda(pos_a, 1000, 16.8)
    pos_b = processar_compra(None, "BBBB3", 1000, 12.0)
    venda_perda = processar_venda(pos_b, 1000, 10.7)

    apuracao = apurar_mes([venda_ganho, venda_perda], prejuizo_anterior=2000.0, irrf_disponivel=0.0, ano_mes="2026-06")
    assert apuracao.total_vendas_acoes == 27500.0
    assert apuracao.ganho_bruto == 6800.0
    assert apuracao.perda_bruta == 1300.0
    assert apuracao.resultado_liquido_mes == 5500.0
    assert apuracao.base_tributavel == 3500.0
    assert apuracao.ir_calculado == 525.0
    assert not apuracao.isento


# ── Validações de entrada (seção 3.2, RF22) ───────────────────
@teste("venda maior que a posição disponível -> ErroValidacaoFiscal, não venda a descoberto silenciosa")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    try:
        processar_venda(pos, 150, 25.0)
        assert False, "deveria ter lançado ErroValidacaoFiscal"
    except ErroValidacaoFiscal:
        pass


@teste("quantidade zero ou negativa na compra -> erro")
def _():
    for qtd in (0, -10):
        try:
            processar_compra(None, "PETR4", qtd, 20.0)
            assert False, f"deveria ter rejeitado quantidade {qtd}"
        except ErroValidacaoFiscal:
            pass


@teste("preço zero ou negativo na venda -> erro")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    for preco in (0, -5):
        try:
            processar_venda(pos, 50, preco)
            assert False, f"deveria ter rejeitado preço {preco}"
        except ErroValidacaoFiscal:
            pass


@teste("vender sem nenhuma posição -> erro, não posição fantasma")
def _():
    try:
        processar_venda(None, 10, 20.0)
        assert False
    except ErroValidacaoFiscal:
        pass


@teste("prejuízo anterior negativo é rejeitado (é saldo, não dívida)")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    venda = processar_venda(pos, 50, 25.0)
    try:
        apurar_mes([venda], prejuizo_anterior=-100.0, irrf_disponivel=0.0, ano_mes="2026-01")
        assert False
    except ErroValidacaoFiscal:
        pass


# ── Regras de isenção e prejuízo (seção 7, 9) ─────────────────
@teste("venda dentro do limite de isenção + lucro -> isento, sem imposto, prejuízo intacto")
def _():
    pos = processar_compra(None, "PETR4", 500, 20.0)  # custo total 10.000
    venda = processar_venda(pos, 500, 22.0)  # vende 11.000 (< 20.000), lucro 1.000
    apuracao = apurar_mes([venda], prejuizo_anterior=3000.0, irrf_disponivel=0.0, ano_mes="2026-01")
    assert apuracao.isento
    assert apuracao.ir_calculado == 0.0
    assert apuracao.prejuizo_novo_saldo == 3000.0, "isenção não deve consumir prejuízo à toa"


@teste("mês de prejuízo puro -> sem imposto, só acumula saldo de prejuízo, mesmo com venda alta")
def _():
    pos = processar_compra(None, "PETR4", 1000, 30.0)  # custo total 30.000
    venda = processar_venda(pos, 1000, 25.0)  # vende 25.000 (> 20.000!), prejuízo 5.000
    apuracao = apurar_mes([venda], prejuizo_anterior=1000.0, irrf_disponivel=0.0, ano_mes="2026-01")
    assert not apuracao.isento, "prejuízo nunca é 'isento', o conceito não se aplica"
    assert apuracao.ir_calculado == 0.0
    assert apuracao.prejuizo_novo_saldo == 6000.0  # 1000 anterior + 5000 novo


@teste("venda acima do limite com lucro -> tributável mesmo sem prejuízo anterior")
def _():
    pos = processar_compra(None, "PETR4", 1000, 20.0)
    venda = processar_venda(pos, 1000, 25.0)  # vende 25.000, lucro 5.000
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0, ano_mes="2026-01")
    assert not apuracao.isento
    assert apuracao.base_tributavel == 5000.0
    assert apuracao.ir_calculado == 750.0  # 15% de 5000


# ── IRRF (seção 11) ────────────────────────────────────────────
@teste("IRRF disponível parcial abate o IR devido, sem zerar indevidamente")
def _():
    pos = processar_compra(None, "PETR4", 1000, 20.0)
    venda = processar_venda(pos, 1000, 25.0)  # lucro 5.000, IR=750
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=200.0, ano_mes="2026-01")
    assert apuracao.ir_calculado == 750.0
    assert apuracao.irrf_utilizado == 200.0
    assert apuracao.imposto_devido == 550.0


@teste("IRRF disponível maior que o IR devido -> abate só até zero, sem crédito negativo")
def _():
    pos = processar_compra(None, "PETR4", 1000, 20.0)
    venda = processar_venda(pos, 1000, 25.0)  # lucro 5.000, IR=750
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=2000.0, ano_mes="2026-01")
    assert apuracao.irrf_utilizado == 750.0, "não deve 'usar' mais IRRF do que o IR devido"
    assert apuracao.imposto_devido == 0.0


# ── Custo médio e posições (seção 5, 6) ────────────────────────
@teste("venda total zera a posição corretamente (custo_medio e custo_total voltam a zero)")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    venda = processar_venda(pos, 100, 25.0)
    assert venda.posicao_remanescente.quantidade == 0
    assert venda.posicao_remanescente.custo_medio == 0.0
    assert venda.posicao_remanescente.custo_total == 0.0


@teste("nova compra após posição zerada inicia do zero, não herda custo médio antigo")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    venda = processar_venda(pos, 100, 25.0)  # zera
    nova_pos = processar_compra(venda.posicao_remanescente, "PETR4", 50, 40.0)
    assert nova_pos.custo_medio == 40.0, "não deve misturar com o custo médio da posição anterior já zerada"


@teste("custos de COMPRA aumentam o custo médio (seção 5)")
def _():
    pos_sem_custo = processar_compra(None, "PETR4", 100, 20.0, custos=0.0)
    pos_com_custo = processar_compra(None, "PETR4", 100, 20.0, custos=100.0)
    assert pos_com_custo.custo_medio > pos_sem_custo.custo_medio
    assert pos_com_custo.custo_medio == 21.0  # (2000+100)/100


@teste("custos de VENDA reduzem o lucro mas NÃO alteram o custo médio remanescente (seção 5)")
def _():
    pos = processar_compra(None, "PETR4", 200, 20.0)  # custo médio 20
    venda_sem_custo = processar_venda(pos, 100, 25.0, custos=0.0)
    venda_com_custo = processar_venda(pos, 100, 25.0, custos=50.0)
    assert venda_com_custo.resultado_liquido == venda_sem_custo.resultado_liquido - 50.0
    assert venda_com_custo.posicao_remanescente.custo_medio == venda_sem_custo.posicao_remanescente.custo_medio == 20.0


@teste("comprar ticker diferente do que já está na posição é rejeitado (evita mistura de ativos)")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    try:
        processar_compra(pos, "VALE3", 50, 60.0)
        assert False
    except ErroValidacaoFiscal:
        pass


# ── Mínimo legal de recolhimento (seção 8, 13) ─────────────────
@teste("imposto abaixo do mínimo (R$10) não gera DARF, acumula pro próximo mês")
def _():
    pos = processar_compra(None, "PETR4", 1000, 20.0)
    venda = processar_venda(pos, 1000, 20.05)  # lucro de R$50 -> IR de R$7,50 (abaixo de R$10)
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0, ano_mes="2026-01")
    assert apuracao.imposto_devido == 7.5
    assert apuracao.imposto_a_pagar_agora == 0.0, "abaixo do mínimo não deveria gerar DARF ainda"
    assert apuracao.imposto_acumulado_novo_saldo == 7.5
    darf = gerar_darf(apuracao)
    assert darf is None, "sem DARF quando ainda está abaixo do mínimo"


@teste("imposto acumulado de meses anteriores + este mês atinge o mínimo -> libera o DARF")
def _():
    pos = processar_compra(None, "PETR4", 1000, 20.0)
    venda = processar_venda(pos, 1000, 20.05)  # IR de R$7,50 de novo
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0,
                           ano_mes="2026-02", imposto_acumulado_anterior=7.5)
    assert apuracao.imposto_a_pagar_agora == 15.0, "7.5 acumulado + 7.5 novo = 15, acima do mínimo"
    assert apuracao.imposto_acumulado_novo_saldo == 0.0
    darf = gerar_darf(apuracao)
    assert darf is not None
    assert darf.valor == 15.0
    assert darf.codigo_receita == "6015"


@teste("mês isento não soma nada ao acumulado, saldo anterior permanece intacto")
def _():
    pos = processar_compra(None, "PETR4", 500, 20.0)
    venda = processar_venda(pos, 500, 21.0)  # venda de 10.500, dentro do limite -> isento
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0,
                           ano_mes="2026-01", imposto_acumulado_anterior=5.0)
    assert apuracao.isento
    assert apuracao.imposto_devido == 0.0
    assert apuracao.imposto_acumulado_novo_saldo == 5.0, "isenção não deve mexer no saldo acumulado de outro mês"


@teste("imposto negativo acumulado é rejeitado")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    venda = processar_venda(pos, 100, 25.0)
    try:
        apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0,
                   ano_mes="2026-01", imposto_acumulado_anterior=-5.0)
        assert False
    except ErroValidacaoFiscal:
        pass


# ── DARF: vencimento ─────────────────────────────────────────────
@teste("vencimento do DARF é sempre no mês seguinte ao da apuração")
def _():
    venc = calcular_vencimento_darf("2026-06")
    assert venc.startswith("2026-07"), f"deveria vencer em julho, veio {venc}"


@teste("vencimento em dezembro rola pro ano seguinte corretamente")
def _():
    venc = calcular_vencimento_darf("2026-12")
    assert venc.startswith("2027-01"), f"deveria vencer em janeiro/2027, veio {venc}"


@teste("vencimento do DARF nunca cai em fim de semana")
def _():
    from datetime import date
    for ano_mes in ["2026-01","2026-02","2026-03","2026-04","2026-05","2026-06",
                     "2026-07","2026-08","2026-09","2026-10","2026-11","2026-12"]:
        venc = calcular_vencimento_darf(ano_mes)
        d = date.fromisoformat(venc)
        assert d.weekday() < 5, f"{ano_mes} -> {venc} caiu em fim de semana"


@teste("gerar_darf devolve None quando não há imposto a pagar")
def _():
    pos = processar_compra(None, "PETR4", 100, 20.0)
    venda = processar_venda(pos, 100, 18.0)  # prejuízo
    apuracao = apurar_mes([venda], prejuizo_anterior=0.0, irrf_disponivel=0.0, ano_mes="2026-01")
    assert gerar_darf(apuracao) is None


# ── Determinismo ────────────────────────────────────────────────
@teste("determinístico: mesma sequência de operações produz exatamente o mesmo resultado")
def _():
    def rodar():
        pos = processar_compra(None, "PETR4", 100, 20.0)
        pos = processar_compra(pos, "PETR4", 50, 22.0)
        venda = processar_venda(pos, 80, 25.0)
        return apurar_mes([venda], prejuizo_anterior=500.0, irrf_disponivel=10.0, ano_mes="2026-03")
    a1 = rodar()
    a2 = rodar()
    assert a1.ir_calculado == a2.ir_calculado
    assert a1.imposto_devido == a2.imposto_devido


# ── Fuzzing / teste de propriedade ────────────────────────────
@teste("fuzzing — 1000 sequências aleatórias de compra/venda, nunca quebra os invariantes")
def _():
    random.seed(42)
    erros = []
    for i in range(1000):
        try:
            pos = None
            vendas_geradas = []
            n_operacoes = random.randint(1, 10)
            for _ in range(n_operacoes):
                if pos is None or pos.quantidade == 0 or random.random() < 0.6:
                    qtd = round(random.uniform(1, 1000), 2)
                    preco = round(random.uniform(0.5, 500), 2)
                    custos = round(random.uniform(0, 50), 2)
                    pos = processar_compra(pos, "FUZZ3", qtd, preco, custos)
                    # invariante: custo médio nunca negativo, custo_total consistente
                    assert pos.custo_medio >= 0
                    assert pos.quantidade > 0
                else:
                    qtd_max = pos.quantidade
                    qtd = round(random.uniform(0.01, qtd_max), 2)
                    preco = round(random.uniform(0.5, 500), 2)
                    custos = round(random.uniform(0, 50), 2)
                    venda = processar_venda(pos, qtd, preco, custos)
                    # invariante: valor bruto - custo base - custos == resultado líquido
                    assert abs(venda.resultado_liquido - (venda.valor_bruto - venda.custo_base - venda.custos_venda)) < 1e-6
                    assert venda.posicao_remanescente.quantidade >= 0
                    pos = venda.posicao_remanescente
                    vendas_geradas.append(venda)

            if vendas_geradas:
                prejuizo_ant = round(random.uniform(0, 5000), 2)
                irrf_disp = round(random.uniform(0, 1000), 2)
                apuracao = apurar_mes(vendas_geradas, prejuizo_ant, irrf_disp, "2026-01")
                # invariantes gerais da apuração
                assert apuracao.ir_calculado >= 0
                assert apuracao.imposto_devido >= 0
                assert apuracao.imposto_devido <= apuracao.ir_calculado + 1e-6
                assert apuracao.prejuizo_novo_saldo >= 0
                assert apuracao.irrf_utilizado <= apuracao.irrf_disponivel + 1e-6
                assert apuracao.irrf_utilizado <= apuracao.ir_calculado + 1e-6

        except ErroValidacaoFiscal:
            pass  # erros de validação esperados (ex: venda>posição) não são falha do teste
        except Exception as e:
            erros.append((i, type(e).__name__, str(e)))

    if erros:
        for i, tipo, msg in erros[:5]:
            print(f"    combinação #{i} falhou: {tipo}: {msg}")
    assert not erros, f"{len(erros)} combinações aleatórias violaram um invariante"


if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"RESULTADO: {_total - len(_falhas)}/{_total} testes passaram")
    if _falhas:
        print(f"FALHARAM: {_falhas}")
        sys.exit(1)
    print("🎉 TODOS OS TESTES PASSARAM")
    sys.exit(0)
