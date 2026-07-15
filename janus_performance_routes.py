# ============================================================
# JANUS PERFORMANCE — ROTAS FLASK v2.0
# Fase 2 revisada: sincronização automática a partir da Carteira
# (sem cadastro manual de operação) + drilldown de 3 níveis:
# Carteira geral -> Categoria -> Ativo.
#
# Uso no servidor.py:
#   from janus_performance_routes import registrar_rotas_performance
#   registrar_rotas_performance(app, requer_auth, uid, obter_valor_atual_carteira)
#
# obter_valor_atual_carteira: função (uid, categoria_id='TODAS', ticker=None) -> float
# que devolve o valor atual a preço de mercado, filtrado pelo nível de
# drilldown pedido. Injetada pelo servidor.py, que já sabe calcular isso
# via o cache de cotações — este módulo não conhece esse detalhe.
# ============================================================

from datetime import datetime, timezone, timedelta
from flask import jsonify, request

import db
from xirr_engine import FluxoCaixa, XirrStatus
import janus_performance as jp

TZ_BR = timezone(timedelta(hours=-3))
def hoje(): return datetime.now(TZ_BR).date()


def _serializar_resultado(resultado: jp.ResultadoPerformance) -> dict:
    x = resultado.xirr
    return {
        "xirr": {
            "status": x.status.value,
            "taxa": x.taxa,
            "taxa_pct": round(x.taxa * 100, 4) if x.taxa is not None else None,
            "valido": x.valido,
            "dias_periodo": x.dias_periodo,
            "severidade_aviso_periodo": x.severidade_aviso_periodo,
            "mensagem_usuario": x.mensagem_usuario,
            "mensagem_tecnica": x.mensagem_tecnica,
            "metodo_utilizado": x.metodo_utilizado,
            "n_raizes_encontradas": x.n_raizes_encontradas,
            "versao_motor": x.versao_motor,
            "data_calculo": x.data_calculo,
        },
        "benchmark": {
            "codigo": resultado.codigo_benchmark,
            "saldo_simulado": round(resultado.saldo_benchmark, 2) if resultado.saldo_benchmark is not None else None,
            "dias_sem_fator": resultado.dias_sem_fator,
            "ganho_perda_absoluto": round(resultado.benchmark_ganho_perda_absoluto, 2) if resultado.benchmark_ganho_perda_absoluto is not None else None,
            "ganho_perda_percentual": round(resultado.benchmark_ganho_perda_percentual * 100, 4) if resultado.benchmark_ganho_perda_percentual is not None else None,
        },
        "valor_atual_real": resultado.valor_atual_real,
        "diferenca_absoluta": round(resultado.diferenca_absoluta, 2) if resultado.diferenca_absoluta is not None else None,
        "diferenca_percentual": round(resultado.diferenca_percentual * 100, 4) if resultado.diferenca_percentual is not None else None,
        "bateu_benchmark": (resultado.diferenca_absoluta > 0) if resultado.diferenca_absoluta is not None else None,
        # ── Auditoria: números crus, verificáveis sem entender XIRR ──
        "auditoria": {
            "data_inicial": resultado.data_inicial.isoformat() if resultado.data_inicial else None,
            "data_final": resultado.data_final.isoformat() if resultado.data_final else None,
            "valor_total_investido": round(resultado.valor_total_investido, 2) if resultado.valor_total_investido is not None else None,
            "valor_total_recebido": round(resultado.valor_total_recebido, 2) if resultado.valor_total_recebido is not None else None,
            "valor_atual": round(resultado.valor_atual_real, 2) if resultado.valor_atual_real is not None else None,
            "ganho_perda_absoluto": round(resultado.ganho_perda_absoluto, 2) if resultado.ganho_perda_absoluto is not None else None,
            "ganho_perda_percentual": round(resultado.ganho_perda_percentual * 100, 4) if resultado.ganho_perda_percentual is not None else None,
        },
    }


def _resultado_vazio(mensagem):
    return {"erro": "sem_operacoes", "mensagem": mensagem}


def registrar_rotas_performance(app, requer_auth, uid, obter_valor_atual_carteira):

    def _calcular(categoria_id='TODAS', ticker=None):
        """Núcleo compartilhado pelos 3 níveis de drilldown — monta os
        fluxos de caixa das operações no filtro pedido, busca o valor
        atual no mesmo filtro, e roda o motor de performance."""
        operacoes = db.db_listar_operacoes(uid(), ticker=ticker, categoria_id=categoria_id)
        if not operacoes:
            return None

        fluxos = [
            FluxoCaixa(
                data=op["data_operacao"],
                valor=-op["valor_total"] if op["tipo"] == "COMPRA" else op["valor_total"],
                descricao=f"{op['tipo']} {op['ticker']}",
            )
            for op in operacoes
        ]

        try:
            valor_atual = obter_valor_atual_carteira(uid(), categoria_id=categoria_id, ticker=ticker)
        except Exception as e:
            print(f"[PERFORMANCE] Erro ao obter valor atual: {e}", flush=True)
            valor_atual = 0.0

        data_inicial = min(f.data for f in fluxos)
        fatores = db.db_buscar_fatores_benchmark("CDI", data_inicial, hoje())

        return jp.calcular_performance(
            fluxos, valor_atual, fatores, codigo_benchmark="CDI", data_referencia=hoje()
        )

    @app.route("/api/performance/sincronizar", methods=["POST"])
    @requer_auth
    def api_performance_sincronizar():
        n = db.db_sincronizar_operacoes_da_carteira(uid())
        return jsonify({"ok": True, "n_operacoes": n})

    @app.route("/api/performance/carteira", methods=["GET"])
    @requer_auth
    def api_performance_carteira():
        """Nível 1 — visão geral, todas as operações."""
        resultado = _calcular()
        if resultado is None:
            return jsonify(_resultado_vazio(
                "Nenhuma operação sincronizada ainda. Clique em \"Atualizar Performance\" "
                "para calcular a partir da sua Carteira."
            )), 200
        return jsonify(_serializar_resultado(resultado))

    @app.route("/api/performance/categorias", methods=["GET"])
    @requer_auth
    def api_performance_categorias():
        """Nível 2 (lista) — categorias com operação, cada uma com seu
        próprio resumo de performance, para a tela de drilldown."""
        categorias = db.db_listar_categorias_com_operacoes(uid())
        saida = []
        for cat in categorias:
            resultado = _calcular(categoria_id=cat["id"])
            item = {"id": cat["id"], "nome": cat["nome"], "cor": cat["cor"], "icone": cat["icone"]}
            item["resultado"] = _serializar_resultado(resultado) if resultado else None
            saida.append(item)
        return jsonify(saida)

    @app.route("/api/performance/categoria/<id_categoria>", methods=["GET"])
    @requer_auth
    def api_performance_categoria(id_categoria):
        """Nível 2 (detalhe) — performance da categoria + lista de ativos
        dentro dela, cada um com seu próprio resumo (nível 3 em miniatura)."""
        cat_id = None if id_categoria == "geral" else int(id_categoria)
        resultado = _calcular(categoria_id=cat_id)
        if resultado is None:
            return jsonify(_resultado_vazio("Nenhuma operação nesta categoria.")), 200

        tickers = db.db_listar_tickers_com_operacoes(uid(), categoria_id=cat_id)
        ativos = []
        for t in tickers:
            r_ativo = _calcular(ticker=t)
            ativos.append({"ticker": t, "resultado": _serializar_resultado(r_ativo) if r_ativo else None})

        resp = _serializar_resultado(resultado)
        resp["ativos"] = ativos
        return jsonify(resp)

    @app.route("/api/performance/ativo/<ticker>", methods=["GET"])
    @requer_auth
    def api_performance_ativo(ticker):
        """Nível 3 — performance de um único ativo."""
        resultado = _calcular(ticker=ticker.upper())
        if resultado is None:
            return jsonify(_resultado_vazio(f"Nenhuma operação registrada para {ticker.upper()}.")), 200
        return jsonify(_serializar_resultado(resultado))

    @app.route("/api/performance/indicadores-macro", methods=["GET"])
    @requer_auth
    def api_performance_indicadores_macro():
        """CDI/SELIC/IPCA acumulados em 12 meses — para o card de
        Indicadores Econômicos do Cockpit. Não faz coleta nova, só soma
        os fatores diários já gravados por benchmarks_collector.py."""
        saida = {}
        for codigo in ("CDI", "SELIC", "IPCA"):
            saida[codigo] = db.db_calcular_acumulado_benchmark(codigo, dias=365)
        return jsonify(saida)

    # ── Mantidas por compatibilidade / uso futuro (ex: módulo de vendas) ──
    @app.route("/api/performance/operacoes", methods=["GET"])
    @requer_auth
    def api_performance_listar_operacoes():
        ticker = request.args.get("ticker")
        ops = db.db_listar_operacoes(uid(), ticker=ticker)
        return jsonify(ops)

    @app.route("/api/performance/operacoes/<int:operacao_id>", methods=["DELETE"])
    @requer_auth
    def api_performance_excluir_operacao(operacao_id):
        ok = db.db_excluir_operacao(uid(), operacao_id)
        if not ok:
            return jsonify({"erro": "Operação não encontrada"}), 404
        return jsonify({"ok": True})
