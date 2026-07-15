# ============================================================
# JANUS FISCAL — ROTAS FLASK v1.0 (Fase 2)
# Integração CARTEIRA → PERFORMANCE → FISCAL numa venda só, conforme
# o documento "JANUS FISCAL — Módulo de Venda de Ativos", seção 2.
#
# Uso no servidor.py:
#   from fiscal_routes import registrar_rotas_fiscal
#   registrar_rotas_fiscal(app, requer_auth, uid)
# ============================================================

from datetime import datetime, timezone, timedelta
from flask import jsonify, request

import db
import fiscal_engine as fe

TZ_BR = timezone(timedelta(hours=-3))
def hoje(): return datetime.now(TZ_BR).date()


def _serializar_apuracao(a):
    """Aceita tanto um dataclass ApuracaoMensal (recém-calculado) quanto
    um dict vindo do banco (já salvo) — normaliza os dois formatos."""
    if a is None:
        return None
    if isinstance(a, dict):
        d = dict(a)
        d.pop("id", None)
        d.pop("usuario_id", None)
        return d
    return {
        "ano_mes": a.ano_mes,
        "total_vendas": round(a.total_vendas_acoes, 2),
        "ganho_bruto": round(a.ganho_bruto, 2),
        "perda_bruta": round(a.perda_bruta, 2),
        "resultado_liquido": round(a.resultado_liquido_mes, 2),
        "isento": a.isento,
        "prejuizo_anterior": round(a.prejuizo_anterior, 2),
        "prejuizo_compensado": round(a.prejuizo_compensado, 2),
        "base_tributavel": round(a.base_tributavel, 2),
        "ir_calculado": round(a.ir_calculado, 2),
        "irrf_disponivel": round(a.irrf_disponivel, 2),
        "irrf_utilizado": round(a.irrf_utilizado, 2),
        "imposto_devido": round(a.imposto_devido, 2),
        "prejuizo_novo_saldo": round(a.prejuizo_novo_saldo, 2),
        "versao_regras": a.versao_regras,
        "status": a.status,
    }


def registrar_rotas_fiscal(app, requer_auth, uid):

    def _recalcular_apuracao_mes(usuario_id, ano_mes, cfg=None):
        """
        Recalcula a apuração de um mês do zero, a partir de todas as
        vendas COMUNS registradas nele — é barato o suficiente pra
        recalcular sempre (não centenas de vendas por mês) e evita
        qualquer risco de apuração salva ficar dessincronizada de uma
        venda editada/excluída depois.
        """
        vendas_raw = db.db_listar_vendas_fiscais_do_mes(usuario_id, ano_mes)
        if not vendas_raw:
            return None
        vendas_obj = [
            fe.ResultadoVenda(
                ticker=v["ticker"], quantidade_vendida=v["quantidade"],
                preco_unitario=v["preco_unitario"], valor_bruto=v["valor_bruto"],
                custo_base=v.get("custo_base") or 0.0, custos_venda=v.get("custos") or 0.0,
                irrf=v.get("irrf") or 0.0, resultado_liquido=v.get("resultado_liquido") or 0.0,
                posicao_remanescente=None,  # não usado por apurar_mes
            )
            for v in vendas_raw
        ]
        prejuizo_anterior = db.db_obter_apuracao_mes_anterior(usuario_id, ano_mes)
        irrf_disponivel = sum(v.get("irrf") or 0.0 for v in vendas_raw)
        apuracao = fe.apurar_mes(vendas_obj, prejuizo_anterior, irrf_disponivel, ano_mes, cfg or fe.ConfiguracaoFiscal())
        db.db_salvar_apuracao_mensal(usuario_id, ano_mes, apuracao.__dict__)
        return apuracao

    @app.route("/api/fiscal/posicao/<ticker>", methods=["GET"])
    @requer_auth
    def api_fiscal_posicao(ticker):
        ticker = ticker.upper()
        pos = db.db_obter_posicao_fiscal(uid(), ticker)
        origem = "posicao_fiscal"
        if not pos:
            pos = db.db_bootstrap_posicao_fiscal_da_carteira(uid(), ticker)
            origem = "bootstrap_carteira"
        if not pos:
            return jsonify({"erro": f"{ticker} não encontrado na Carteira nem em posições fiscais."}), 404
        pos["origem"] = origem
        return jsonify(pos)

    @app.route("/api/fiscal/vender", methods=["POST"])
    @requer_auth
    def api_fiscal_vender():
        d = request.json or {}
        ticker = (d.get("ticker") or "").upper().strip()
        data_operacao = d.get("data_operacao")
        custos = float(d.get("custos") or 0)
        irrf = float(d.get("irrf") or 0)

        if not ticker:
            return jsonify({"erro": "ticker é obrigatório"}), 400
        if not data_operacao:
            return jsonify({"erro": "data_operacao é obrigatória"}), 400
        try:
            quantidade = float(d.get("quantidade"))
            preco_unitario = float(d.get("preco_unitario"))
        except (TypeError, ValueError):
            return jsonify({"erro": "quantidade e preco_unitario devem ser numéricos"}), 400

        # 1) posição fiscal atual — se for a primeira venda deste ativo,
        #    parte do preço médio já cadastrado na Carteira (seção 6: sem
        #    reconstrução retroativa de histórico anterior a este módulo)
        pos_dict = db.db_obter_posicao_fiscal(uid(), ticker)
        if not pos_dict:
            pos_dict = db.db_bootstrap_posicao_fiscal_da_carteira(uid(), ticker)
        if not pos_dict:
            return jsonify({"erro": f"{ticker} não encontrado na Carteira — não é possível vender o que não existe."}), 400

        posicao = fe.PosicaoFiscal(
            ticker=ticker, quantidade=pos_dict["quantidade"],
            custo_medio=pos_dict["custo_medio"], custo_total=pos_dict["custo_total"],
        )

        # 2) motor fiscal — determinístico, isolado, já testado (Fase 1)
        try:
            resultado = fe.processar_venda(posicao, quantidade, preco_unitario, custos=custos, irrf=irrf)
        except fe.ErroValidacaoFiscal as e:
            return jsonify({"erro": str(e)}), 400

        # 3) persiste a posição fiscal atualizada
        db.db_salvar_posicao_fiscal(
            uid(), ticker, resultado.posicao_remanescente.quantidade,
            resultado.posicao_remanescente.custo_medio, resultado.posicao_remanescente.custo_total,
        )

        # 4) categoria/corretora herdadas da posição atual na Carteira
        posicoes_carteira = db.db_listar_carteira(uid())
        pos_carteira = next(
            (p for p in posicoes_carteira if p["ticker"] == ticker and p.get("status") == "confirmada"), None
        )
        categoria_id = pos_carteira.get("categoria_id") if pos_carteira else None
        corretora = d.get("corretora") or (pos_carteira.get("corretora") if pos_carteira else None)

        # 5) registra no livro-razão fiscal (fonte de verdade da apuração de IR)
        op_fiscal_id = db.db_registrar_operacao_fiscal(
            uid(), ticker, "VENDA", "COMUM", data_operacao, quantidade, preco_unitario,
            resultado.valor_bruto, custos=custos, irrf=irrf,
            custo_base=resultado.custo_base, resultado_liquido=resultado.resultado_liquido,
            categoria_id=categoria_id, observacao="Venda registrada via Janus Fiscal",
        )

        # 6) alimenta o Performance (fecha a lacuna: sem isso, o ganho
        #    realizado sumiria do XIRR assim que o ativo saísse da Carteira)
        db.db_criar_operacao(
            uid(), ticker, "VENDA", data_operacao, quantidade, preco_unitario,
            corretora=corretora, categoria_id=categoria_id,
            observacao="Fiscal — venda registrada",
        )

        # 7) atualiza a Carteira — reduz quantidade preservando o custo
        #    médio fiscal remanescente; remove se zerou (seção 2)
        if pos_carteira:
            nova_qtd = resultado.posicao_remanescente.quantidade
            if nova_qtd <= 0:
                db.db_remover_posicao(uid(), ticker)
            else:
                db.db_salvar_posicao(
                    uid(), ticker, pos_carteira["nome"], pos_carteira["cor"],
                    pos_carteira.get("setor_id"), pos_carteira.get("setor_nome"),
                    resultado.posicao_remanescente.custo_medio, nova_qtd,
                    pos_carteira["data_compra"], pos_carteira.get("corretora"), categoria_id,
                )

        # 8) recalcula a apuração do mês da venda
        ano_mes = str(data_operacao)[:7]
        apuracao = _recalcular_apuracao_mes(uid(), ano_mes)

        return jsonify({
            "ok": True,
            "operacao_fiscal_id": op_fiscal_id,
            "venda": {
                "valor_bruto": round(resultado.valor_bruto, 2),
                "custo_base": round(resultado.custo_base, 2),
                "custos": round(custos, 2),
                "resultado_liquido": round(resultado.resultado_liquido, 2),
            },
            "posicao_remanescente": {
                "quantidade": resultado.posicao_remanescente.quantidade,
                "custo_medio": round(resultado.posicao_remanescente.custo_medio, 2),
            },
            "apuracao_mes": _serializar_apuracao(apuracao),
        })

    @app.route("/api/fiscal/apuracao/<ano_mes>", methods=["GET"])
    @requer_auth
    def api_fiscal_apuracao(ano_mes):
        salva = db.db_obter_apuracao_mensal(uid(), ano_mes)
        if salva:
            return jsonify(_serializar_apuracao(salva))
        apuracao = _recalcular_apuracao_mes(uid(), ano_mes)
        if apuracao is None:
            return jsonify({
                "erro": "sem_vendas",
                "mensagem": f"Nenhuma venda registrada em {ano_mes}.",
            }), 200
        return jsonify(_serializar_apuracao(apuracao))

    @app.route("/api/fiscal/central", methods=["GET"])
    @requer_auth
    def api_fiscal_central():
        """Resumo do mês atual — a base do futuro Dashboard Fiscal
        (seção 10): status de isenção, IR devido, progresso do limite."""
        ano_mes = request.args.get("ano_mes") or hoje().strftime("%Y-%m")
        salva = db.db_obter_apuracao_mensal(uid(), ano_mes)
        apuracao = salva if salva else _recalcular_apuracao_mes(uid(), ano_mes)
        serializado = _serializar_apuracao(apuracao)

        cfg = fe.ConfiguracaoFiscal()
        total_vendas = serializado["total_vendas"] if serializado else 0.0
        pct_limite = round(min(100.0, (total_vendas / cfg.limite_isencao_mensal_acoes) * 100), 1) if cfg.limite_isencao_mensal_acoes else 0.0

        return jsonify({
            "ano_mes": ano_mes,
            "limite_isencao_mensal": cfg.limite_isencao_mensal_acoes,
            "percentual_limite_usado": pct_limite,
            "apuracao": serializado,
        })

    @app.route("/api/fiscal/vendas", methods=["GET"])
    @requer_auth
    def api_fiscal_vendas():
        ano_mes = request.args.get("ano_mes")
        ticker = request.args.get("ticker")
        vendas = db.db_listar_operacoes_fiscais(uid(), ticker=ticker, ano_mes=ano_mes)
        return jsonify(vendas)
