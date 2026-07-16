# ============================================================
# JANUS FISCAL — ROTAS FLASK v2.0 (Fase 2 + Central Fiscal completa)
# Integração CARTEIRA → PERFORMANCE → FISCAL numa venda só, com
# apuração mensal, fechamento/reabertura auditável de mês, DARF,
# extrato de vendas e export simplificado pro IRPF.
#
# Uso no servidor.py:
#   from fiscal_routes import registrar_rotas_fiscal
#   registrar_rotas_fiscal(app, requer_auth, uid)
# ============================================================

from datetime import datetime, date, timezone, timedelta
from flask import jsonify, request, Response

import db
import fiscal_engine as fe

TZ_BR = timezone(timedelta(hours=-3))
def hoje(): return datetime.now(TZ_BR).date()


def _serializar_apuracao(a):
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
        "imposto_acumulado_anterior": round(a.imposto_acumulado_anterior, 2),
        "imposto_a_pagar_agora": round(a.imposto_a_pagar_agora, 2),
        "imposto_acumulado_novo_saldo": round(a.imposto_acumulado_novo_saldo, 2),
        "prejuizo_novo_saldo": round(a.prejuizo_novo_saldo, 2),
        "versao_regras": a.versao_regras,
        "status": a.status,
    }


def _serializar_darf(d):
    if d is None:
        return None
    return {
        "ano_mes_referencia": d.ano_mes_referencia,
        "codigo_receita": d.codigo_receita,
        "valor": round(d.valor, 2),
        "data_vencimento": d.data_vencimento,
        "competencia": d.competencia,
    }


def registrar_rotas_fiscal(app, requer_auth, uid):

    def _calcular_apuracao(usuario_id, ano_mes, cfg=None):
        vendas_raw = db.db_listar_vendas_fiscais_do_mes(usuario_id, ano_mes)
        if not vendas_raw:
            return None
        vendas_obj = [
            fe.ResultadoVenda(
                ticker=v["ticker"], quantidade_vendida=v["quantidade"],
                preco_unitario=v["preco_unitario"], valor_bruto=v["valor_bruto"],
                custo_base=v.get("custo_base") or 0.0, custos_venda=v.get("custos") or 0.0,
                irrf=v.get("irrf") or 0.0, resultado_liquido=v.get("resultado_liquido") or 0.0,
                posicao_remanescente=None,
            )
            for v in vendas_raw
        ]
        saldos = db.db_obter_saldos_mes_anterior(usuario_id, ano_mes)
        irrf_disponivel = sum(v.get("irrf") or 0.0 for v in vendas_raw)

        if cfg is None:
            # Busca a regra vigente NO MÊS SENDO APURADO (dia 1), não hoje —
            # essencial pra reprocessar mês antigo com a regra que valia
            # naquela época, se a legislação mudou entre lá e agora.
            ano, mes = (int(x) for x in ano_mes.split('-'))
            data_referencia_regra = date(ano, mes, 1)
            cfg_dict = db.db_montar_configuracao_fiscal(data_referencia_regra)
            cfg = fe.ConfiguracaoFiscal(**cfg_dict)

        return fe.apurar_mes(
            vendas_obj, saldos["prejuizo_anterior"], irrf_disponivel, ano_mes, cfg,
            imposto_acumulado_anterior=saldos["imposto_acumulado_anterior"],
        )

    def _recalcular_e_salvar_mes(usuario_id, ano_mes, permitir_sobrescrever_fechado=False):
        apuracao = _calcular_apuracao(usuario_id, ano_mes)
        if apuracao is None:
            return None
        db.db_salvar_apuracao_mensal(usuario_id, ano_mes, apuracao.__dict__,
                                      permitir_sobrescrever_fechado=permitir_sobrescrever_fechado)
        return apuracao

    def _reprocessar_cascata(usuario_id, ano_mes_inicial, forcar_reabertura=False):
        recalculados = []
        bloqueados = []
        meses_a_processar = [ano_mes_inicial] + db.db_listar_meses_apos(usuario_id, ano_mes_inicial)
        for am in meses_a_processar:
            apuracao_atual = db.db_obter_apuracao_mensal(usuario_id, am)
            if apuracao_atual and apuracao_atual.get("status") == "Fechado":
                if not forcar_reabertura:
                    bloqueados.append(am)
                    break
                ok, _ = db.db_reabrir_mes_fiscal(
                    usuario_id, am,
                    motivo=f"Reaberto automaticamente por reprocessamento em cascata a partir de {ano_mes_inicial}",
                )
                if not ok:
                    bloqueados.append(am)
                    break

            nova = _recalcular_e_salvar_mes(usuario_id, am, permitir_sobrescrever_fechado=True)
            if nova is not None:
                recalculados.append(am)
                if apuracao_atual and apuracao_atual.get("status") in ("Fechado", "Reaberto"):
                    db.db_registrar_log_reprocessamento(
                        usuario_id, am, "REPROCESSOU",
                        motivo=f"Cascata a partir de {ano_mes_inicial}",
                        valores_antes=apuracao_atual, valores_depois=nova.__dict__,
                    )
                    db.db_salvar_apuracao_mensal(
                        usuario_id, am, {**nova.__dict__, "status": "Reprocessado"},
                        permitir_sobrescrever_fechado=True,
                    )
        return recalculados, bloqueados

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

        ano_mes = str(data_operacao)[:7]
        apuracao_existente = db.db_obter_apuracao_mensal(uid(), ano_mes)
        if apuracao_existente and apuracao_existente.get("status") == "Fechado":
            return jsonify({
                "erro": "mes_fechado",
                "mensagem": f"O mês {ano_mes} já está fechado. Reabra-o na Central Fiscal "
                            f"(com um motivo) antes de registrar uma venda nele.",
            }), 400

        pos_dict = db.db_obter_posicao_fiscal(uid(), ticker)
        if not pos_dict:
            pos_dict = db.db_bootstrap_posicao_fiscal_da_carteira(uid(), ticker)
        if not pos_dict:
            return jsonify({"erro": f"{ticker} não encontrado na Carteira — não é possível vender o que não existe."}), 400

        posicao = fe.PosicaoFiscal(
            ticker=ticker, quantidade=pos_dict["quantidade"],
            custo_medio=pos_dict["custo_medio"], custo_total=pos_dict["custo_total"],
        )
        try:
            resultado = fe.processar_venda(posicao, quantidade, preco_unitario, custos=custos, irrf=irrf)
        except fe.ErroValidacaoFiscal as e:
            return jsonify({"erro": str(e)}), 400

        db.db_salvar_posicao_fiscal(
            uid(), ticker, resultado.posicao_remanescente.quantidade,
            resultado.posicao_remanescente.custo_medio, resultado.posicao_remanescente.custo_total,
        )

        posicoes_carteira = db.db_listar_carteira(uid())
        pos_carteira = next(
            (p for p in posicoes_carteira if p["ticker"] == ticker and p.get("status") == "confirmada"), None
        )
        categoria_id = pos_carteira.get("categoria_id") if pos_carteira else None
        corretora = d.get("corretora") or (pos_carteira.get("corretora") if pos_carteira else None)

        op_fiscal_id = db.db_registrar_operacao_fiscal(
            uid(), ticker, "VENDA", "COMUM", data_operacao, quantidade, preco_unitario,
            resultado.valor_bruto, custos=custos, irrf=irrf,
            custo_base=resultado.custo_base, resultado_liquido=resultado.resultado_liquido,
            categoria_id=categoria_id, observacao="Venda registrada via Janus Fiscal",
        )

        db.db_criar_operacao(
            uid(), ticker, "VENDA", data_operacao, quantidade, preco_unitario,
            corretora=corretora, categoria_id=categoria_id,
            observacao="Fiscal — venda registrada",
        )

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

        _, bloqueados = _reprocessar_cascata(uid(), ano_mes, forcar_reabertura=False)
        apuracao_do_mes = db.db_obter_apuracao_mensal(uid(), ano_mes)

        resposta = {
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
            "apuracao_mes": _serializar_apuracao(apuracao_do_mes),
        }
        if bloqueados:
            resposta["aviso"] = (
                f"Meses já fechados à frente desta venda não foram recalculados "
                f"automaticamente: {', '.join(bloqueados)}. Reabra-os manualmente na "
                f"Central Fiscal se precisar refletir esta venda neles."
            )
        return jsonify(resposta)

    @app.route("/api/fiscal/apuracao/<ano_mes>", methods=["GET"])
    @requer_auth
    def api_fiscal_apuracao(ano_mes):
        salva = db.db_obter_apuracao_mensal(uid(), ano_mes)
        if salva:
            resp = _serializar_apuracao(salva)
        else:
            apuracao = _recalcular_e_salvar_mes(uid(), ano_mes)
            if apuracao is None:
                return jsonify({"erro": "sem_vendas", "mensagem": f"Nenhuma venda registrada em {ano_mes}."}), 200
            resp = _serializar_apuracao(apuracao)
        darf = None
        if resp and resp.get("imposto_a_pagar_agora", 0) > 0:
            darf = _serializar_darf(fe.gerar_darf(fe.ApuracaoMensal(
                ano_mes=resp["ano_mes"], total_vendas_acoes=resp["total_vendas"],
                ganho_bruto=resp["ganho_bruto"], perda_bruta=resp["perda_bruta"],
                resultado_liquido_mes=resp["resultado_liquido"], isento=resp["isento"],
                prejuizo_anterior=resp["prejuizo_anterior"], prejuizo_compensado=resp["prejuizo_compensado"],
                base_tributavel=resp["base_tributavel"], ir_calculado=resp["ir_calculado"],
                irrf_disponivel=resp["irrf_disponivel"], irrf_utilizado=resp["irrf_utilizado"],
                imposto_devido=resp["imposto_devido"], imposto_acumulado_anterior=resp["imposto_acumulado_anterior"],
                imposto_a_pagar_agora=resp["imposto_a_pagar_agora"],
                imposto_acumulado_novo_saldo=resp["imposto_acumulado_novo_saldo"],
                prejuizo_novo_saldo=resp["prejuizo_novo_saldo"], versao_regras=resp["versao_regras"],
                status=resp["status"],
            )))
        resp["darf"] = darf
        return jsonify(resp)

    @app.route("/api/fiscal/central", methods=["GET"])
    @requer_auth
    def api_fiscal_central():
        ano_mes = request.args.get("ano_mes") or hoje().strftime("%Y-%m")
        return api_fiscal_apuracao(ano_mes)

    @app.route("/api/fiscal/apuracoes", methods=["GET"])
    @requer_auth
    def api_fiscal_apuracoes_historico():
        limite = request.args.get("limite", 24, type=int)
        apuracoes = db.db_listar_apuracoes_fiscais(uid(), limite=limite)
        return jsonify([_serializar_apuracao(a) for a in apuracoes])

    @app.route("/api/fiscal/reprocessar", methods=["POST"])
    @requer_auth
    def api_fiscal_reprocessar():
        """
        Recalcula em cascata a partir de um mês, independente do mês
        inicial estar fechado ou não — útil depois de editar/excluir uma
        venda antiga e querer propagar o efeito pra frente. Meses
        FECHADOS no caminho só são tocados com forcar_reabertura=True.
        """
        d = request.json or {}
        ano_mes_inicial = d.get("ano_mes_inicial")
        forcar_reabertura = bool(d.get("forcar_reabertura"))
        if not ano_mes_inicial:
            return jsonify({"erro": "ano_mes_inicial é obrigatório"}), 400

        recalculados, bloqueados = _reprocessar_cascata(uid(), ano_mes_inicial, forcar_reabertura=forcar_reabertura)
        return jsonify({
            "ok": True,
            "meses_recalculados": recalculados,
            "meses_ainda_bloqueados": bloqueados,
        })

    @app.route("/api/fiscal/mes/<ano_mes>/fechar", methods=["POST"])
    @requer_auth
    def api_fiscal_fechar_mes(ano_mes):
        ok, erro = db.db_fechar_mes_fiscal(uid(), ano_mes)
        if not ok:
            return jsonify({"erro": erro}), 400
        return jsonify({"ok": True})

    @app.route("/api/fiscal/mes/<ano_mes>/reabrir", methods=["POST"])
    @requer_auth
    def api_fiscal_reabrir_mes(ano_mes):
        d = request.json or {}
        motivo = (d.get("motivo") or "").strip()
        cascata = bool(d.get("cascata"))
        if not motivo:
            return jsonify({"erro": "É necessário informar o motivo da reabertura."}), 400

        ok, erro = db.db_reabrir_mes_fiscal(uid(), ano_mes, motivo)
        if not ok:
            return jsonify({"erro": erro}), 400

        recalculados, bloqueados = _reprocessar_cascata(uid(), ano_mes, forcar_reabertura=cascata)
        return jsonify({
            "ok": True,
            "meses_recalculados": recalculados,
            "meses_ainda_bloqueados": bloqueados,
        })

    @app.route("/api/fiscal/reprocessamento/log", methods=["GET"])
    @requer_auth
    def api_fiscal_log_reprocessamento():
        ano_mes = request.args.get("ano_mes")
        return jsonify(db.db_listar_log_reprocessamento(uid(), ano_mes))

    # ── Regras fiscais versionadas (limite, alíquotas, mínimo DARF) ──
    @app.route("/api/fiscal/regras", methods=["GET"])
    @requer_auth
    def api_fiscal_listar_regras():
        tipo = request.args.get("tipo")
        return jsonify(db.db_listar_regras_fiscais(tipo))

    @app.route("/api/fiscal/regras", methods=["POST"])
    @requer_auth
    def api_fiscal_criar_regra():
        d = request.json or {}
        tipo_regra = d.get("tipo_regra")
        vigencia_inicio = d.get("vigencia_inicio")
        if not tipo_regra or tipo_regra not in db.TIPOS_REGRA_VALIDOS:
            return jsonify({"erro": f"tipo_regra inválido. Use um de: {', '.join(db.TIPOS_REGRA_VALIDOS)}"}), 400
        if not vigencia_inicio:
            return jsonify({"erro": "vigencia_inicio é obrigatória"}), 400
        try:
            valor = float(d.get("valor"))
        except (TypeError, ValueError):
            return jsonify({"erro": "valor deve ser numérico"}), 400
        if valor < 0:
            return jsonify({"erro": "valor não pode ser negativo"}), 400
        # Alíquotas são fração (0.15 = 15%) — trava simples contra erro de
        # digitação comum (digitar "15" pensando em 15% em vez de 0.15)
        if 'aliquota' in tipo_regra and valor > 1:
            return jsonify({"erro": "Alíquota deve ser fração decimal (ex: 0.15 para 15%), não percentual inteiro."}), 400

        ok, resultado = db.db_criar_regra_fiscal(
            tipo_regra, valor, vigencia_inicio,
            fonte_normativa=d.get("fonte_normativa"),
            observacao=d.get("observacao"),
            criado_por=uid(),
        )
        if not ok:
            return jsonify({"erro": resultado}), 400
        return jsonify({"ok": True, "id": resultado})

    @app.route("/api/fiscal/vendas", methods=["GET"])
    @requer_auth
    def api_fiscal_vendas():
        ano_mes = request.args.get("ano_mes")
        ticker = request.args.get("ticker")
        vendas = db.db_listar_operacoes_fiscais(uid(), ticker=ticker, ano_mes=ano_mes)
        return jsonify(vendas)

    @app.route("/api/fiscal/export/irpf/<ano>", methods=["GET"])
    @requer_auth
    def api_fiscal_export_irpf(ano):
        apuracoes = [a for a in db.db_listar_apuracoes_fiscais(uid(), limite=12)
                     if a["ano_mes"].startswith(ano)]
        apuracoes.sort(key=lambda a: a["ano_mes"])
        vendas = [v for v in db.db_listar_operacoes_fiscais(uid())
                  if str(v["data_operacao"]).startswith(ano) and v["tipo"] == "VENDA"]

        linhas = [
            f"JANUS FISCAL — RESUMO PARA DECLARAÇÃO DE IMPOSTO DE RENDA {ano}",
            "=" * 70,
            "Este arquivo organiza os números para digitação manual no programa",
            "da Receita Federal (ficha 'Renda Variável - Operações Comuns/Day Trade').",
            "Não é um arquivo de importação automática.",
            "",
            "-- RESULTADO MENSAL (Renda Variável) --",
            "",
        ]
        total_ir_ano = 0.0
        for a in apuracoes:
            linhas.append(f"{a['ano_mes']}:")
            linhas.append(f"  Vendas no mes: R$ {a['total_vendas']:.2f}")
            linhas.append(f"  Resultado liquido: R$ {a['resultado_liquido']:.2f}")
            linhas.append(f"  Situacao: {'Isento' if a['isento'] else 'Tributavel'}")
            if not a["isento"]:
                linhas.append(f"  Base de calculo: R$ {a['base_tributavel']:.2f}")
                linhas.append(f"  IR pago no mes: R$ {a['imposto_a_pagar_agora']:.2f}")
            linhas.append("")
            total_ir_ano += a["imposto_a_pagar_agora"]

        linhas.append(f"TOTAL DE IR PAGO NO ANO: R$ {total_ir_ano:.2f}")
        linhas.append("")
        linhas.append("-- OPERACOES DETALHADAS (auditoria) --")
        linhas.append("")
        for v in vendas:
            linhas.append(
                f"{v['data_operacao']} | VENDA {v['ticker']} | "
                f"{v['quantidade']} un x R$ {v['preco_unitario']:.2f} = R$ {v['valor_bruto']:.2f} | "
                f"Resultado: R$ {(v.get('resultado_liquido') or 0):.2f}"
            )

        conteudo = "\n".join(linhas)
        return Response(
            conteudo, mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=janus_fiscal_irpf_{ano}.txt"},
        )
