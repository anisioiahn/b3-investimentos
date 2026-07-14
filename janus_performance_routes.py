# ============================================================
# JANUS PERFORMANCE — ROTAS FLASK v1.0
# Fase 2: CRUD de operações + XIRR/comparação CDI da carteira geral.
#
# Uso no servidor.py:
#   from janus_performance_routes import registrar_rotas_performance
#   registrar_rotas_performance(app, requer_auth, uid, obter_valor_atual_carteira)
#
# obter_valor_atual_carteira: função (uid) -> float que devolve o valor
# atual total da carteira confirmada, a preço de mercado. Injetada pelo
# servidor.py (que já sabe calcular isso via o cache de cotações) — este
# módulo não precisa conhecer esse detalhe internamente.
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
        },
        "valor_atual_real": resultado.valor_atual_real,
        "diferenca_absoluta": round(resultado.diferenca_absoluta, 2) if resultado.diferenca_absoluta is not None else None,
        "diferenca_percentual": round(resultado.diferenca_percentual * 100, 4) if resultado.diferenca_percentual is not None else None,
        "bateu_benchmark": (resultado.diferenca_absoluta > 0) if resultado.diferenca_absoluta is not None else None,
    }


def registrar_rotas_performance(app, requer_auth, uid, obter_valor_atual_carteira):

    @app.route("/api/performance/operacoes", methods=["GET"])
    @requer_auth
    def api_performance_listar_operacoes():
        ticker = request.args.get("ticker")
        ops = db.db_listar_operacoes(uid(), ticker)
        return jsonify(ops)

    @app.route("/api/performance/operacoes", methods=["POST"])
    @requer_auth
    def api_performance_criar_operacao():
        d = request.json or {}
        ticker = (d.get("ticker") or "").upper().strip()
        tipo = (d.get("tipo") or "").upper().strip()
        data_operacao = d.get("data_operacao")
        quantidade = d.get("quantidade")
        preco_unitario = d.get("preco_unitario")

        if not ticker:
            return jsonify({"erro": "ticker é obrigatório"}), 400
        if tipo not in ("COMPRA", "VENDA"):
            return jsonify({"erro": "tipo deve ser COMPRA ou VENDA"}), 400
        if not data_operacao:
            return jsonify({"erro": "data_operacao é obrigatória"}), 400
        try:
            quantidade = float(quantidade)
            preco_unitario = float(preco_unitario)
        except (TypeError, ValueError):
            return jsonify({"erro": "quantidade e preco_unitario devem ser numéricos"}), 400
        if quantidade <= 0 or preco_unitario <= 0:
            return jsonify({"erro": "quantidade e preco_unitario devem ser maiores que zero"}), 400

        novo_id = db.db_criar_operacao(
            uid(), ticker, tipo, data_operacao, quantidade, preco_unitario,
            corretora=d.get("corretora"),
            categoria_id=d.get("categoria_id"),
            observacao=d.get("observacao"),
        )
        if novo_id is None:
            return jsonify({"erro": "Não foi possível salvar a operação"}), 500
        return jsonify({"ok": True, "id": novo_id})

    @app.route("/api/performance/operacoes/<int:operacao_id>", methods=["DELETE"])
    @requer_auth
    def api_performance_excluir_operacao(operacao_id):
        ok = db.db_excluir_operacao(uid(), operacao_id)
        if not ok:
            return jsonify({"erro": "Operação não encontrada"}), 404
        return jsonify({"ok": True})

    @app.route("/api/performance/carteira", methods=["GET"])
    @requer_auth
    def api_performance_carteira():
        """
        XIRR da carteira geral (todas as operações do usuário, todos os
        tickers) comparado contra o CDI via simulação de carteira sombra.
        """
        operacoes = db.db_listar_operacoes(uid())
        if not operacoes:
            return jsonify({
                "erro": "sem_operacoes",
                "mensagem": "Nenhuma operação registrada ainda. Adicione suas compras e "
                            "vendas para calcular a rentabilidade pessoal da carteira.",
            }), 200

        fluxos = [
            FluxoCaixa(
                data=op["data_operacao"],
                valor=-op["valor_total"] if op["tipo"] == "COMPRA" else op["valor_total"],
                descricao=f"{op['tipo']} {op['ticker']}",
            )
            for op in operacoes
        ]

        try:
            valor_atual = obter_valor_atual_carteira(uid())
        except Exception as e:
            print(f"[PERFORMANCE] Erro ao obter valor atual da carteira: {e}", flush=True)
            valor_atual = 0.0

        data_inicial = min(f.data for f in fluxos)
        fatores = db.db_buscar_fatores_benchmark("CDI", data_inicial, hoje())

        resultado = jp.calcular_performance(
            fluxos, valor_atual, fatores, codigo_benchmark="CDI", data_referencia=hoje()
        )
        return jsonify(_serializar_resultado(resultado))
