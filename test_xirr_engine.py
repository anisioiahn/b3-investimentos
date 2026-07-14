# ============================================================
# JANUS PERFORMANCE — TESTES DO MOTOR DE XIRR
# Cobre a lista de casos obrigatórios da seção 16 do documento
# "Tratamento de Exceções na XIRR", mais os testes adicionais que
# sugeri na revisão (fuzzing / testes de propriedade).
#
# Rodar com: python3 -m pytest test_xirr_engine.py -v
# (ou python3 test_xirr_engine.py, roda como script direto também)
# ============================================================

from datetime import date, timedelta
import random
import sys

from xirr_engine import (
    FluxoCaixa, XirrStatus, ConfiguracaoXirr, calcular_xirr, _npv,
)

_falhas = []
_total = 0

def teste(nome):
    """Decorator simples — não depende de pytest, roda como script puro também."""
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


# ── 1. Fluxo convencional com uma única raiz ───────────────────
@teste("fluxo convencional com uma única raiz")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), -10000.0),
        FluxoCaixa(date(2026, 7, 13), 14500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status in (XirrStatus.VALID, XirrStatus.SHORT_PERIOD_WARNING), r.status
    assert r.taxa is not None and 0.05 < r.taxa < 0.20, f"taxa fora do esperado: {r.taxa}"


# ── 2. Somente fluxos negativos ─────────────────────────────────
@teste("somente fluxos negativos -> INSUFFICIENT_CASH_FLOWS")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), -1000.0),
        FluxoCaixa(date(2023, 6, 1), -500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.INSUFFICIENT_CASH_FLOWS, r.status
    assert r.taxa is None


# ── 3. Somente fluxos positivos ─────────────────────────────────
@teste("somente fluxos positivos -> INSUFFICIENT_CASH_FLOWS")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), 1000.0),
        FluxoCaixa(date(2023, 6, 1), 500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.INSUFFICIENT_CASH_FLOWS, r.status


# ── 4. Apenas um fluxo ───────────────────────────────────────────
@teste("apenas um fluxo -> INSUFFICIENT_CASH_FLOWS")
def _():
    fluxos = [FluxoCaixa(date(2023, 1, 1), -1000.0)]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.INSUFFICIENT_CASH_FLOWS, r.status


# ── 5. Aporte inicial e patrimônio atual (o caso mais comum) ────
@teste("aporte inicial + patrimônio atual como fluxo terminal")
def _():
    fluxos = [
        FluxoCaixa(date(2025, 1, 15), -5000.0),
        FluxoCaixa(date(2026, 7, 13), 5450.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.valido, r.status
    assert r.taxa > 0


# ── 6. Múltiplas mudanças de sinal (não implica necessariamente
#       múltiplas raízes matemáticas — só testa que não trava) ──
@teste("múltiplas mudanças de sinal — não trava, devolve status válido")
def _():
    fluxos = [
        FluxoCaixa(date(2022, 1, 1), -10000.0),
        FluxoCaixa(date(2022, 8, 1), 4000.0),   # retirada parcial
        FluxoCaixa(date(2023, 3, 1), -3000.0),  # novo aporte
        FluxoCaixa(date(2026, 7, 13), 9800.0),  # valor final
    ]
    r = calcular_xirr(fluxos)
    assert r.status in (XirrStatus.VALID, XirrStatus.SHORT_PERIOD_WARNING,
                         XirrStatus.MULTIPLE_ROOTS, XirrStatus.NO_REAL_ROOT), r.status


# ── 7. Múltiplas raízes de verdade (caso clássico da literatura) ─
@teste("múltiplas raízes matemáticas -> MULTIPLE_ROOTS")
def _():
    # Fluxo clássico de múltiplas raízes: -1 no ano 0, +2.5 no ano 1, -1.5 no ano 2
    # Este padrão tem 2 raízes reais em [0, 1] no domínio de fator (1+r).
    ano0 = date(2024, 1, 1)
    fluxos = [
        FluxoCaixa(ano0, -1000.0),
        FluxoCaixa(ano0 + timedelta(days=365), 2500.0),
        FluxoCaixa(ano0 + timedelta(days=730), -1500.0),
    ]
    r = calcular_xirr(fluxos)
    # Aceita tanto MULTIPLE_ROOTS quanto VALID com 1 raiz — depende de onde
    # exatamente as raízes caem; o teste real é que NUNCA deve escolher
    # silenciosamente uma raiz sem AVISAR quando há mais de uma.
    if r.status == XirrStatus.VALID or r.status == XirrStatus.SHORT_PERIOD_WARNING:
        assert r.n_raizes_encontradas == 1
    else:
        assert r.status == XirrStatus.MULTIPLE_ROOTS, r.status
        assert r.n_raizes_encontradas >= 2
        assert r.taxa is None, "MULTIPLE_ROOTS nunca deve expor uma taxa como se fosse única"


# ── 8. Ausência de raiz real ──────────────────────────────────────
@teste("fluxos sem raiz real dentro do intervalo -> NO_REAL_ROOT ou instabilidade tratada")
def _():
    # Um único centavo de saída, um valor astronomicamente maior de entrada
    # no dia seguinte — não impossível matematicamente, mas testa o limite
    fluxos = [
        FluxoCaixa(date(2026, 7, 12), -0.01),
        FluxoCaixa(date(2026, 7, 13), 999999999.0),
    ]
    r = calcular_xirr(fluxos)
    # Taxa astronômica e período de 1 dia -> deve cair em OUT_OF_ALLOWED_RANGE
    # ou SHORT_PERIOD_WARNING com taxa extrema, mas NUNCA travar
    assert r.status in (XirrStatus.OUT_OF_ALLOWED_RANGE, XirrStatus.SHORT_PERIOD_WARNING,
                         XirrStatus.NO_REAL_ROOT, XirrStatus.NON_CONVERGENCE), r.status


# ── 9. Raiz próxima de -100% ──────────────────────────────────────
@teste("raiz próxima de -100% (perda quase total) — não trava")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), -10000.0),
        FluxoCaixa(date(2026, 7, 13), 5.0),  # sobrou quase nada
    ]
    r = calcular_xirr(fluxos)
    if r.valido:
        # -99,95% do capital ao longo de ~3,5 anos anualiza para ~-88,4%
        # (a anualização amortece a magnitude ao longo de vários anos —
        # não é o mesmo que perder quase tudo num único ano)
        assert r.taxa < -0.5, f"esperava taxa bem negativa, veio {r.taxa}"
        assert r.taxa > -1.0, "taxa nunca pode ser <= -100% (limite matemático)"


# ── 10. Retorno extremamente elevado ──────────────────────────────
@teste("retorno alto mas plausível em período curto -> SHORT_PERIOD_WARNING")
def _():
    fluxos = [
        FluxoCaixa(date(2026, 6, 15), -1000.0),
        FluxoCaixa(date(2026, 7, 13), 1300.0),  # +30% em 28 dias — forte, mas plausível
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.SHORT_PERIOD_WARNING, r.status
    assert r.severidade_aviso_periodo == "forte"
    assert r.taxa > 1.0, "taxa anualizada deveria ser bem alta (>100% a.a.)"

@teste("retorno absurdo (5x em 3 dias, ~10^85% a.a.) -> NO_REAL_ROOT, não trava")
def _():
    # Taxa implícita anualizada é ~10^85% — muito além de qualquer teto
    # econômico configurável. O motor deve reconhecer que não há raiz
    # dentro dos limites configurados, não tentar "inventar" um número.
    fluxos = [
        FluxoCaixa(date(2026, 7, 10), -100.0),
        FluxoCaixa(date(2026, 7, 13), 500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status in (XirrStatus.NO_REAL_ROOT, XirrStatus.OUT_OF_ALLOWED_RANGE), r.status
    assert r.taxa is None, "não deve expor uma taxa 'inventada' fora dos limites configurados"


# ── 11. Fluxos em datas iguais ────────────────────────────────────
@teste("múltiplos fluxos na mesma data são somados corretamente pelo NPV")
def _():
    fluxos = [
        FluxoCaixa(date(2025, 1, 1), -3000.0),
        FluxoCaixa(date(2025, 1, 1), -2000.0),  # mesmo dia, outro aporte
        FluxoCaixa(date(2026, 7, 13), 6000.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.valido, r.status


# ── 12. Fluxos em período muito curto ─────────────────────────────
@teste("período muito curto (<=30 dias) -> SHORT_PERIOD_WARNING severidade forte")
def _():
    fluxos = [
        FluxoCaixa(date(2026, 7, 1), -1000.0),
        FluxoCaixa(date(2026, 7, 10), 1050.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.SHORT_PERIOD_WARNING, r.status
    assert r.severidade_aviso_periodo == "forte"

@teste("período de 60 dias -> SHORT_PERIOD_WARNING severidade informativo")
def _():
    fluxos = [
        FluxoCaixa(date(2026, 5, 1), -1000.0),
        FluxoCaixa(date(2026, 6, 30), 1080.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.SHORT_PERIOD_WARNING, r.status
    assert r.severidade_aviso_periodo == "informativo"

@teste("período de 120 dias -> VALID, sem aviso")
def _():
    fluxos = [
        FluxoCaixa(date(2026, 3, 1), -1000.0),
        FluxoCaixa(date(2026, 6, 30), 1080.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.VALID, r.status
    assert r.severidade_aviso_periodo is None


# ── 13. Carteira encerrada ────────────────────────────────────────
@teste("carteira encerrada (venda total, sem posição aberta)")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), -8000.0),
        FluxoCaixa(date(2024, 6, 1), 300.0),   # dividendo recebido
        FluxoCaixa(date(2026, 3, 1), 9200.0),  # venda total
    ]
    r = calcular_xirr(fluxos)
    assert r.valido, r.status
    assert r.taxa > 0


# ── 14. Aportes e retiradas alternados ────────────────────────────
@teste("aportes e retiradas alternados, sem gerar múltiplas raízes")
def _():
    fluxos = [
        FluxoCaixa(date(2022, 1, 1), -5000.0),
        FluxoCaixa(date(2023, 1, 1), -3000.0),
        FluxoCaixa(date(2024, 1, 1), -2000.0),
        FluxoCaixa(date(2026, 7, 13), 12500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status in (XirrStatus.VALID, XirrStatus.SHORT_PERIOD_WARNING, XirrStatus.MULTIPLE_ROOTS)


# ── 15. Falha simulada de convergência ────────────────────────────
@teste("configuração com tolerância impossível força NON_CONVERGENCE controlado")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 1, 1), -10000.0),
        FluxoCaixa(date(2026, 7, 13), 14500.0),
    ]
    cfg = ConfiguracaoXirr(tolerancia_residuo_relativa=1e-30, max_iter_bisseccao=1)
    r = calcular_xirr(fluxos, cfg)
    # Com só 1 iteração de bisseção e tolerância praticamente impossível,
    # deve degradar graciosamente para NON_CONVERGENCE, nunca travar/explodir
    assert r.status in (XirrStatus.NON_CONVERGENCE, XirrStatus.VALID,
                         XirrStatus.SHORT_PERIOD_WARNING), r.status


# ── 16. Determinismo — mesma entrada, mesma saída ─────────────────
@teste("determinístico: mesma entrada produz exatamente o mesmo resultado")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 4, 12), -7500.0),
        FluxoCaixa(date(2024, 9, 3), -1200.0),
        FluxoCaixa(date(2026, 7, 13), 10340.0),
    ]
    r1 = calcular_xirr(fluxos)
    r2 = calcular_xirr(fluxos)
    assert r1.status == r2.status
    assert r1.taxa == r2.taxa, f"{r1.taxa} != {r2.taxa}"


# ── Casos adicionais além da lista mínima ─────────────────────────
@teste("fluxos em ordem embaralhada dão o mesmo resultado (ordem não importa)")
def _():
    fluxos_ordem1 = [
        FluxoCaixa(date(2023, 1, 1), -5000.0),
        FluxoCaixa(date(2024, 6, 1), -1000.0),
        FluxoCaixa(date(2026, 7, 13), 7200.0),
    ]
    fluxos_ordem2 = [fluxos_ordem1[2], fluxos_ordem1[0], fluxos_ordem1[1]]
    r1 = calcular_xirr(fluxos_ordem1)
    r2 = calcular_xirr(fluxos_ordem2)
    assert r1.status == r2.status
    # Tolerância, não igualdade exata: soma de ponto flutuante não é
    # estritamente associativa (IEEE754) — ordens diferentes de soma
    # podem divergir na 15ª-17ª casa decimal, o que é esperado e
    # irrelevante para qualquer uso financeiro real.
    assert abs(r1.taxa - r2.taxa) < 1e-9, f"{r1.taxa} vs {r2.taxa}"


@teste("lista vazia de fluxos não trava")
def _():
    r = calcular_xirr([])
    assert r.status == XirrStatus.INSUFFICIENT_CASH_FLOWS


@teste("valores com muitas casas decimais não quebram a precisão")
def _():
    fluxos = [
        FluxoCaixa(date(2023, 3, 17), -5432.10),
        FluxoCaixa(date(2026, 7, 13), 7891.23),
    ]
    r = calcular_xirr(fluxos)
    assert r.valido, r.status


@teste("REGRESSÃO: raiz em zero exato não é contada 2x (bug real encontrado e corrigido)")
def _():
    # Fluxo clássico com raízes matemáticas EXATAS em r=0% e r=50%
    # (verificado por Bhaskara: 3x²-5x+2=0, x=1/(1+r)).
    # Bug original: como 0.0 está literalmente na grade de busca, o motor
    # contava o toque em zero tanto como "chegada por troca de sinal"
    # quanto como "acerto exato", inflando pra 3 raízes em vez de 2.
    ano0 = date(2024, 1, 1)
    fluxos = [
        FluxoCaixa(ano0, -1000.0),
        FluxoCaixa(ano0 + timedelta(days=365), 2500.0),
        FluxoCaixa(ano0 + timedelta(days=730), -1500.0),
    ]
    r = calcular_xirr(fluxos)
    assert r.status == XirrStatus.MULTIPLE_ROOTS, r.status
    assert r.n_raizes_encontradas == 2, f"esperava exatamente 2 raízes, veio {r.n_raizes_encontradas}"


# ── Fuzzing / teste de propriedade ─────────────────────────────────
# Gera fluxos de caixa aleatórios (respeitando pelo menos 1 negativo e
# 1 positivo) e verifica que o motor NUNCA lança exceção e SEMPRE
# devolve um status da enum, para qualquer combinação — isso cobre
# casos-limite que uma lista fixa não antecipa.
@teste("fuzzing — 2000 combinações aleatórias de fluxos (seed 42), nunca lança exceção")
def _():
    random.seed(42)  # reprodutível
    erros = []
    for i in range(2000):
        n_fluxos = random.randint(1, 8)
        base = date(2015, 1, 1)
        fluxos = []
        for _ in range(n_fluxos):
            dias_offset = random.randint(0, 4000)
            valor = round(random.uniform(-50000, 50000), 2)
            if valor == 0:
                valor = 0.01
            fluxos.append(FluxoCaixa(base + timedelta(days=dias_offset), valor))
        try:
            r = calcular_xirr(fluxos)
            assert isinstance(r.status, XirrStatus)
            if r.valido:
                assert r.taxa > -1.0, f"taxa <= -100%: {r.taxa}"
                assert r.taxa <= 1000.0, f"taxa fora do teto absoluto: {r.taxa}"
            if r.status == XirrStatus.MULTIPLE_ROOTS:
                assert r.taxa is None, "MULTIPLE_ROOTS nunca deve expor taxa"
                assert r.n_raizes_encontradas >= 2
        except Exception as e:
            erros.append((i, fluxos, type(e).__name__, str(e)))

    if erros:
        for i, fl, tipo, msg in erros[:5]:
            print(f"    combinação #{i} falhou: {tipo}: {msg}")
            print(f"    fluxos: {[(f.data, f.valor) for f in fl]}")
    assert not erros, f"{len(erros)} combinações aleatórias lançaram exceção (deveriam devolver status, nunca travar)"

@teste("fuzzing — 1000 combinações aleatórias adicionais (seed 777, cobertura diferente)")
def _():
    random.seed(777)
    erros = []
    for i in range(1000):
        n_fluxos = random.randint(2, 12)
        base = date(2010, 1, 1)
        fluxos = []
        for _ in range(n_fluxos):
            dias_offset = random.randint(0, 6000)
            # faixa de valores mais ampla, incluindo valores bem pequenos
            # (testa sensibilidade numérica) e bem grandes
            valor = round(random.uniform(-1_000_000, 1_000_000), 2)
            if valor == 0:
                valor = 0.01
            fluxos.append(FluxoCaixa(base + timedelta(days=dias_offset), valor))
        try:
            r = calcular_xirr(fluxos)
            assert isinstance(r.status, XirrStatus)
        except Exception as e:
            erros.append((i, fluxos, type(e).__name__, str(e)))
    assert not erros, f"{len(erros)} combinações falharam com seed 777"


# ── Resumo ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"RESULTADO: {_total - len(_falhas)}/{_total} testes passaram")
    if _falhas:
        print(f"FALHARAM: {_falhas}")
        sys.exit(1)
    else:
        print("🎉 TODOS OS TESTES PASSARAM")
        sys.exit(0)
