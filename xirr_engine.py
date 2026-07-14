# ============================================================
# JANUS PERFORMANCE — MOTOR DE XIRR v1.0
# Fase 1 do módulo Janus Performance.
#
# Implementa o solver em camadas especificado nos documentos:
#   "JANUS PERFORMANCE" (spec funcional) e
#   "Tratamento de Exceções na XIRR" (spec de robustez numérica).
#
# Sem dependências externas (sem scipy/numpy) — bisseção e Newton-Raphson
# implementados em Python puro, propositalmente, porque o resto do projeto
# não usa essas libs e elas adicionariam peso de build no Render à toa.
#
# Princípio central (doc de exceções, seção 1): a ausência de XIRR é um
# ESTADO MATEMÁTICO POSSÍVEL, não um erro. O motor nunca "força" uma
# resposta numérica duvidosa — ele classifica o motivo e devolve um
# status estruturado, sempre.
# ============================================================

from enum import Enum
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import List, Optional, Tuple

VERSAO_MOTOR = "1.0"
TZ_BR = timezone(timedelta(hours=-3))


# ── Status (doc de exceções, seção 11) ────────────────────────
class XirrStatus(Enum):
    VALID                   = "VALID"
    INSUFFICIENT_CASH_FLOWS = "INSUFFICIENT_CASH_FLOWS"
    NO_SIGN_CHANGE          = "NO_SIGN_CHANGE"
    NO_REAL_ROOT            = "NO_REAL_ROOT"
    MULTIPLE_ROOTS          = "MULTIPLE_ROOTS"
    NON_CONVERGENCE         = "NON_CONVERGENCE"
    OUT_OF_ALLOWED_RANGE    = "OUT_OF_ALLOWED_RANGE"
    NUMERICAL_INSTABILITY   = "NUMERICAL_INSTABILITY"
    SHORT_PERIOD_WARNING    = "SHORT_PERIOD_WARNING"


# ── Estruturas de entrada/saída ───────────────────────────────
@dataclass
class FluxoCaixa:
    """Um fluxo de caixa datado. valor < 0 = saída (aporte/compra);
    valor > 0 = entrada (retirada/venda/provento/patrimônio atual)."""
    data: date
    valor: float
    descricao: str = ""


@dataclass
class ConfiguracaoXirr:
    """Parâmetros numéricos do motor — todos com valor default, mas
    pensados pra serem ajustáveis sem mexer no código do solver."""
    limite_inferior: float = -0.9999          # nunca -100% exato (indefinido matematicamente)
    limite_superior_inicial: float = 10.0      # 1.000% a.a. — ponto de partida da busca
    limite_superior_absoluto: float = 1000.0   # 100.000% a.a. — teto absoluto, além disso é OUT_OF_ALLOWED_RANGE
    max_expansoes: int = 3                     # expande o limite superior em até 3x (10x cada vez) se não achar raiz
    tolerancia_residuo_relativa: float = 1e-6  # resíduo aceito = tolerância * escala do fluxo (ver _tolerancia_absoluta)
    tolerancia_raiz: float = 1e-9              # precisão da taxa em si (bisseção/Newton param)
    max_iter_bisseccao: int = 200
    max_iter_newton: int = 50
    dedup_raizes_delta: float = 1e-4           # brackets a menos de 0.01pp são a mesma raiz (resolução de grade)
    dias_alerta_forte: int = 30                # <= 30 dias: alerta forte (doc seção 9)
    dias_alerta_informativo: int = 90          # 31-90 dias: alerta informativo


@dataclass
class XirrResult:
    status: XirrStatus
    taxa: Optional[float] = None                       # taxa anualizada, ex.: 0.124 = 12,4% a.a.
    residuo: Optional[float] = None
    n_raizes_encontradas: int = 0
    intervalo_pesquisado: Optional[Tuple[float, float]] = None
    metodo_utilizado: Optional[str] = None
    versao_motor: str = VERSAO_MOTOR
    data_calculo: Optional[str] = None
    dias_periodo: Optional[int] = None
    severidade_aviso_periodo: Optional[str] = None      # None | "informativo" | "forte"
    mensagem_tecnica: str = ""
    mensagem_usuario: str = ""

    @property
    def valido(self) -> bool:
        """True quando existe uma taxa utilizável (VALID ou SHORT_PERIOD_WARNING —
        este último ainda tem taxa válida, só carrega um aviso de anualização)."""
        return self.status in (XirrStatus.VALID, XirrStatus.SHORT_PERIOD_WARNING) and self.taxa is not None


# ── Núcleo matemático ──────────────────────────────────────────
def _fator_desconto(r: float, anos: float) -> Optional[float]:
    """(1+r)^anos — retorna None se matematicamente indefinido (base <= 0).
    anos é tipicamente fracionário (dias/365), então nunca se pode deixar
    r <= -100% chegar aqui: número negativo elevado a expoente fracionário
    não tem solução real."""
    base = 1.0 + r
    if base <= 0:
        return None
    try:
        return base ** anos
    except (OverflowError, ValueError):
        return None


def _npv(fluxos: List[FluxoCaixa], r: float, data_ref: date) -> Optional[float]:
    """Valor presente líquido dos fluxos à taxa r, descontado a partir de data_ref."""
    total = 0.0
    for fc in fluxos:
        anos = (fc.data - data_ref).days / 365.0
        fator = _fator_desconto(r, anos)
        if fator is None or fator == 0:
            return None
        total += fc.valor / fator
    return total


def _grade_pesquisa(limite_inferior: float, limite_superior: float) -> List[float]:
    """Pontos de r para varrer em busca de troca de sinal do NPV.
    Mais denso perto de -100% (onde a função é muito sensível) e no
    intervalo comum (-50% a +200%); mais esparso nos extremos."""
    pontos = set()
    negativos = [-0.9999, -0.999, -0.995, -0.99, -0.98, -0.95, -0.90,
                 -0.85, -0.80, -0.70, -0.60, -0.50, -0.40, -0.30,
                 -0.20, -0.10, -0.05, -0.02, 0.0]
    for v in negativos:
        if v >= limite_inferior:
            pontos.add(v)

    r = 0.02
    while r <= min(limite_superior, 2.0):
        pontos.add(round(r, 6))
        r += 0.02
    while r <= min(limite_superior, 10.0):
        pontos.add(round(r, 6))
        r += 0.10
    while r <= limite_superior:
        pontos.add(round(r, 6))
        r += 1.0
    pontos.add(limite_superior)
    return sorted(pontos)


def _encontrar_brackets(fluxos: List[FluxoCaixa], data_ref: date,
                         limite_inferior: float, limite_superior: float) -> List[Tuple[float, float]]:
    """Varre a grade de busca e devolve os intervalos [r_lo, r_hi] onde o
    NPV muda de sinal (candidatos a raiz). Um toque exato em zero conta
    como UM evento só — tratá-lo tanto como "chegada" (troca de sinal
    terminando em zero) quanto como "acerto exato" separadamente faria a
    mesma raiz ser contada duas vezes."""
    grade = _grade_pesquisa(limite_inferior, limite_superior)
    avaliados = [(r, _npv(fluxos, r, data_ref)) for r in grade]

    brackets = []
    anterior = None
    for r, v in avaliados:
        if v is None:
            anterior = None
            continue
        if v == 0:
            brackets.append((r, r))
            anterior = (r, v)
            continue
        if anterior is not None and anterior[1] is not None and anterior[1] != 0:
            r_ant, v_ant = anterior
            if (v_ant < 0) != (v < 0):
                brackets.append((r_ant, r))
        anterior = (r, v)
    return brackets


def _deduplicar_brackets(brackets: List[Tuple[float, float]], delta: float) -> List[Tuple[float, float]]:
    """Brackets cujo ponto médio está a menos de `delta` um do outro são
    tratados como a mesma raiz (evita contar 2x por causa da resolução
    da grade, não porque existem 2 raízes de verdade)."""
    if not brackets:
        return []
    pontos_medios = sorted(brackets, key=lambda b: (b[0] + b[1]) / 2)
    dedup = [pontos_medios[0]]
    for b in pontos_medios[1:]:
        m_atual = (b[0] + b[1]) / 2
        m_ultimo = (dedup[-1][0] + dedup[-1][1]) / 2
        if abs(m_atual - m_ultimo) > delta:
            dedup.append(b)
    return dedup


def _bisseccao(fluxos: List[FluxoCaixa], data_ref: date, r_lo: float, r_hi: float,
                tol: float, max_iter: int) -> Optional[float]:
    """Método principal — sempre converge dado um bracket válido (mudança
    de sinal confirmada nas pontas)."""
    v_lo = _npv(fluxos, r_lo, data_ref)
    v_hi = _npv(fluxos, r_hi, data_ref)
    if v_lo is None or v_hi is None:
        return None
    if v_lo == 0: return r_lo
    if v_hi == 0: return r_hi
    if (v_lo < 0) == (v_hi < 0):
        return None  # não é um bracket válido (sem troca de sinal)

    for _ in range(max_iter):
        r_mid = (r_lo + r_hi) / 2
        v_mid = _npv(fluxos, r_mid, data_ref)
        if v_mid is None:
            # ponto médio caiu numa região indefinida — encolhe pro lado seguro
            r_hi = r_mid
            continue
        if abs(v_mid) < tol or (r_hi - r_lo) < 1e-12:
            return r_mid
        if (v_mid < 0) == (v_lo < 0):
            r_lo, v_lo = r_mid, v_mid
        else:
            r_hi, v_hi = r_mid, v_mid
    return (r_lo + r_hi) / 2  # melhor esforço após esgotar iterações


def _newton_refinar(fluxos: List[FluxoCaixa], data_ref: date, r0: float,
                     tol: float, max_iter: int) -> Optional[float]:
    """Auxiliar — só usado para REFINAR uma raiz já localizada pela
    bisseção (nunca como método de busca isolado, conforme a spec)."""
    r = r0
    for _ in range(max_iter):
        v = _npv(fluxos, r, data_ref)
        if v is None:
            return None
        if abs(v) < tol:
            return r
        h = 1e-6
        v_h = _npv(fluxos, r + h, data_ref)
        if v_h is None:
            return None
        deriv = (v_h - v) / h
        if abs(deriv) < 1e-14:
            return None  # derivada quase nula — Newton fica instável aqui, desiste
        r_novo = r - v / deriv
        if r_novo <= -1.0:
            return None  # cruzaria a barreira matemática — rejeita
        if abs(r_novo - r) < tol:
            return r_novo
        r = r_novo
    return None  # não convergiu dentro do limite de iterações


# ── Validação de pré-condições (doc de exceções, seção 2) ──────
def _validar_fluxos_minimos(fluxos: List[FluxoCaixa]) -> Tuple[bool, str]:
    if len(fluxos) < 2:
        return False, "Menos de 2 fluxos financeiros informados."
    datas = set(fc.data for fc in fluxos)
    if len(datas) < 2:
        return False, "Todos os fluxos estão na mesma data — não há período para anualizar."
    tem_negativo = any(fc.valor < 0 for fc in fluxos)
    tem_positivo = any(fc.valor > 0 for fc in fluxos)
    if not tem_negativo:
        return False, "Não há nenhum fluxo de saída (aporte/compra) — apenas entradas."
    if not tem_positivo:
        return False, "Não há nenhum fluxo de entrada (retirada/venda/patrimônio atual) — apenas saídas."
    return True, ""


def _tolerancia_absoluta(fluxos: List[FluxoCaixa], tolerancia_relativa: float) -> float:
    """Converte a tolerância relativa numa tolerância absoluta de resíduo,
    escalada pelo volume financeiro dos fluxos — evita usar o mesmo épsilon
    fixo tanto pra uma posição de R$ 100 quanto pra uma de R$ 1.000.000."""
    escala = sum(abs(fc.valor) for fc in fluxos) / len(fluxos)
    return max(1e-6, escala * tolerancia_relativa)


def _checar_estabilidade(fluxos: List[FluxoCaixa], data_ref: date, raiz: float,
                          cfg: ConfiguracaoXirr) -> bool:
    """Doc seção 8: 'a solução deve permanecer estável em novas avaliações
    próximas'. Avalia o NPV numa pequena vizinhança da raiz e confirma que
    o comportamento é o esperado de uma raiz simples (sinal troca uma vez
    só, suavemente) — não uma região numericamente instável/quase-plana
    onde pequenas perturbações produzem outro quase-zero."""
    h = max(1e-5, abs(raiz) * 1e-4)
    v_esq = _npv(fluxos, raiz - h, data_ref)
    v_dir = _npv(fluxos, raiz + h, data_ref)
    if v_esq is None or v_dir is None:
        return False
    # Numa raiz simples bem comportada, v_esq e v_dir devem ter sinais opostos
    # entre si (a função atravessa o zero uma vez, de forma monotônica local)
    return (v_esq < 0) != (v_dir < 0)


# ── Orquestração principal (doc de exceções, seção 4, passos 1-7) ─
def calcular_xirr(fluxos: List[FluxoCaixa], config: Optional[ConfiguracaoXirr] = None) -> XirrResult:
    """
    Ponto de entrada público do motor. Sempre devolve um XirrResult —
    nunca lança exceção para condições matemáticas esperadas (fluxos
    insuficientes, ausência de raiz, múltiplas raízes, não-convergência).
    """
    cfg = config or ConfiguracaoXirr()
    agora_iso = datetime.now(TZ_BR).isoformat()

    # Passo 1 — validar sinais e quantidade de fluxos
    ok, motivo = _validar_fluxos_minimos(fluxos)
    if not ok:
        return XirrResult(
            status=XirrStatus.INSUFFICIENT_CASH_FLOWS,
            data_calculo=agora_iso,
            mensagem_tecnica=motivo,
            mensagem_usuario="Rentabilidade ainda não disponível. É necessária ao menos uma "
                              "avaliação posterior do patrimônio.",
        )

    data_ref = min(fc.data for fc in fluxos)
    data_final = max(fc.data for fc in fluxos)
    dias_periodo = (data_final - data_ref).days

    # Passos 2-3 — procurar intervalos com troca de sinal, expandindo o
    # limite superior progressivamente se nada for encontrado de cara
    limite_sup = cfg.limite_superior_inicial
    brackets: List[Tuple[float, float]] = []
    for _ in range(cfg.max_expansoes + 1):
        brackets = _encontrar_brackets(fluxos, data_ref, cfg.limite_inferior, limite_sup)
        if brackets:
            break
        limite_sup = min(limite_sup * 10, cfg.limite_superior_absoluto)
        if limite_sup >= cfg.limite_superior_absoluto:
            brackets = _encontrar_brackets(fluxos, data_ref, cfg.limite_inferior, limite_sup)
            break

    if not brackets:
        return XirrResult(
            status=XirrStatus.NO_REAL_ROOT,
            intervalo_pesquisado=(cfg.limite_inferior, limite_sup),
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica=f"Nenhuma troca de sinal do VPL encontrada no intervalo "
                              f"[{cfg.limite_inferior:.4f}, {limite_sup:.4f}].",
            mensagem_usuario="A rentabilidade pessoal não pôde ser calculada para este período "
                              "porque o padrão dos fluxos financeiros não produz uma taxa interna "
                              "de retorno válida.",
        )

    # Passo 5 — deduplica candidatos muito próximos (resolução de grade,
    # não múltiplas raízes de verdade) e classifica como MULTIPLE_ROOTS
    # apenas se sobrar mais de uma raiz genuinamente distinta
    brackets_dedup = _deduplicar_brackets(brackets, cfg.dedup_raizes_delta)

    if len(brackets_dedup) > 1:
        return XirrResult(
            status=XirrStatus.MULTIPLE_ROOTS,
            n_raizes_encontradas=len(brackets_dedup),
            intervalo_pesquisado=(cfg.limite_inferior, limite_sup),
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica=f"{len(brackets_dedup)} raízes distintas encontradas: {brackets_dedup}",
            mensagem_usuario="Não foi possível determinar uma rentabilidade pessoal única porque "
                              "os fluxos financeiros produzem mais de uma taxa possível. Consulte "
                              "a rentabilidade da estratégia e a evolução patrimonial para avaliar "
                              "o período.",
        )

    # Passo 4 (principal) — bisseção dentro do bracket único encontrado
    r_lo, r_hi = brackets_dedup[0]
    raiz_bisseccao = _bisseccao(fluxos, data_ref, r_lo, r_hi,
                                 tol=cfg.tolerancia_raiz, max_iter=cfg.max_iter_bisseccao)
    if raiz_bisseccao is None:
        return XirrResult(
            status=XirrStatus.NON_CONVERGENCE,
            intervalo_pesquisado=(r_lo, r_hi),
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica="Bisseção não convergiu dentro do bracket identificado.",
            mensagem_usuario="Não foi possível obter uma taxa pessoal confiável para os fluxos "
                              "deste período. Os demais indicadores de desempenho permanecem "
                              "disponíveis.",
        )

    # Passo 4 (auxiliar) — Newton-Raphson só para REFINAR a raiz já achada
    raiz_newton = _newton_refinar(fluxos, data_ref, raiz_bisseccao,
                                   tol=cfg.tolerancia_raiz / 100, max_iter=cfg.max_iter_newton)
    if raiz_newton is not None and -1.0 < raiz_newton:
        raiz_final = raiz_newton
        metodo = "bisseccao+newton"
    else:
        raiz_final = raiz_bisseccao
        metodo = "bisseccao"

    # Passo 6 — validar o resíduo da solução encontrada
    residuo = _npv(fluxos, raiz_final, data_ref)
    tol_abs = _tolerancia_absoluta(fluxos, cfg.tolerancia_residuo_relativa)
    if residuo is None or abs(residuo) > tol_abs:
        return XirrResult(
            status=XirrStatus.NON_CONVERGENCE,
            residuo=residuo,
            intervalo_pesquisado=(r_lo, r_hi),
            metodo_utilizado=metodo,
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica=f"Resíduo {residuo} acima da tolerância {tol_abs}.",
            mensagem_usuario="Não foi possível obter uma taxa pessoal confiável para os fluxos "
                              "deste período. Os demais indicadores de desempenho permanecem "
                              "disponíveis.",
        )

    # Passo 7 — rejeitar soluções fora dos limites econômicos configurados
    if raiz_final <= cfg.limite_inferior or raiz_final > cfg.limite_superior_absoluto:
        return XirrResult(
            status=XirrStatus.OUT_OF_ALLOWED_RANGE,
            taxa=raiz_final,
            residuo=residuo,
            intervalo_pesquisado=(cfg.limite_inferior, cfg.limite_superior_absoluto),
            metodo_utilizado=metodo,
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica=f"Taxa {raiz_final} fora do intervalo econômico configurado.",
            mensagem_usuario="A taxa calculada está fora dos limites configurados para o sistema "
                              "e não é apresentada como resultado confiável.",
        )

    # Checagem de estabilidade numérica (doc seção 8)
    if not _checar_estabilidade(fluxos, data_ref, raiz_final, cfg):
        return XirrResult(
            status=XirrStatus.NUMERICAL_INSTABILITY,
            taxa=raiz_final,
            residuo=residuo,
            intervalo_pesquisado=(r_lo, r_hi),
            metodo_utilizado=metodo,
            data_calculo=agora_iso,
            dias_periodo=dias_periodo,
            mensagem_tecnica="Solução instável em avaliação de vizinhança.",
            mensagem_usuario="Não foi possível obter uma taxa pessoal confiável para os fluxos "
                              "deste período. Os demais indicadores de desempenho permanecem "
                              "disponíveis.",
        )

    # Sucesso — classifica período curto conforme doc seção 9
    severidade = None
    status_final = XirrStatus.VALID
    if dias_periodo <= cfg.dias_alerta_forte:
        severidade = "forte"
        status_final = XirrStatus.SHORT_PERIOD_WARNING
    elif dias_periodo <= cfg.dias_alerta_informativo:
        severidade = "informativo"
        status_final = XirrStatus.SHORT_PERIOD_WARNING

    msg_usuario = ("Taxa anualizada calculada sobre um período curto. O resultado pode não "
                    "representar o desempenho esperado em um ano completo."
                    if severidade else
                    "Rentabilidade pessoal calculada com sucesso.")

    return XirrResult(
        status=status_final,
        taxa=raiz_final,
        residuo=residuo,
        n_raizes_encontradas=1,
        intervalo_pesquisado=(r_lo, r_hi),
        metodo_utilizado=metodo,
        data_calculo=agora_iso,
        dias_periodo=dias_periodo,
        severidade_aviso_periodo=severidade,
        mensagem_tecnica=f"Convergiu via {metodo}, resíduo={residuo:.2e}.",
        mensagem_usuario=msg_usuario,
    )
