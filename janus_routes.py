# ============================================================
# JANUS INDEX – ROTAS FLASK v1.1
# Segue o padrão do projeto: psycopg2 + get_conn()
# Chame no servidor.py:
#   from janus_routes import registrar_rotas_janus
#   registrar_rotas_janus(app, requer_auth)
# ============================================================

import psycopg2, psycopg2.extras, os, threading
from flask import jsonify, request
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora():    return datetime.now(TZ_BRASILIA)
def hoje():     return agora().strftime("%Y-%m-%d")

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# Flag global de progresso do collector
_janus_rodando = False
_janus_progresso = {"atual": 0, "total": 0, "ticker_atual": "", "pct": 0}

def run_collector_com_progresso():
    """Wrapper que atualiza o flag global durante a coleta."""
    global _janus_rodando, _janus_progresso
    _janus_rodando = True
    _janus_progresso = {"atual": 0, "total": 0, "ticker_atual": "Iniciando...", "pct": 0}
    try:
        from janus_collector import buscar_lista_ativos, buscar_dados_lote, \
            salvar_lote_banco, salvar_ranking, get_source_id, iniciar_log, finalizar_log, \
            LOTE_BRAPI, DELAY_MS, get_conn as col_get_conn
        import time

        col_conn = col_get_conn()
        try:
            source_id = get_source_id(col_conn)
            col_conn.commit()
            log_id = iniciar_log(col_conn, source_id)
            col_conn.commit()

            lista = buscar_lista_ativos()
            if not lista:
                finalizar_log(col_conn, log_id, "FAILED", 0, "Lista vazia")
                col_conn.commit()
                return

            _janus_progresso["total"] = len(lista)
            _janus_progresso["ticker_atual"] = f"Registrando {len(lista)} ativos no banco..."

            # Upsert batch com progresso
            asset_map = {}
            with col_conn.cursor() as cur:
                for idx, stock in enumerate(lista):
                    ticker = stock.get("stock") or stock.get("symbol")
                    if not ticker: continue
                    nome  = stock.get("name", ticker)
                    setor = stock.get("sector")
                    try:
                        cur.execute("""
                            INSERT INTO companies (corporate_name, trading_name, sector, updated_at)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (trading_name) DO UPDATE SET
                                sector=EXCLUDED.sector, updated_at=EXCLUDED.updated_at
                            RETURNING company_id
                        """, (nome, nome, setor, agora().isoformat()))
                        company_id = cur.fetchone()[0]
                        cur.execute("""
                            INSERT INTO assets (ticker, company_id, asset_type, currency, country, status, updated_at)
                            VALUES (%s, %s, 'ACAO', 'BRL', 'BR', 'ATIVO', %s)
                            ON CONFLICT (ticker) DO UPDATE SET
                                company_id=EXCLUDED.company_id, updated_at=EXCLUDED.updated_at
                            RETURNING asset_id
                        """, (ticker, company_id, agora().isoformat()))
                        asset_map[ticker] = cur.fetchone()[0]
                    except Exception: pass
                    if idx % 10 == 0:
                        _janus_progresso["atual"] = idx
                        _janus_progresso["pct"] = round(idx / len(lista) * 100)
                        _janus_progresso["ticker_atual"] = f"Registrando ativos... ({idx}/{len(lista)})"
            col_conn.commit()

            tickers_lista = list(asset_map.keys())
            total_lotes = (len(tickers_lista) + LOTE_BRAPI - 1) // LOTE_BRAPI
            total_processados = 0
            total_erros = 0
            rankings = []

            for i in range(0, len(tickers_lista), LOTE_BRAPI):
                lote = tickers_lista[i:i+LOTE_BRAPI]
                lote_num = i // LOTE_BRAPI + 1
                _janus_progresso["ticker_atual"] = f"Lote {lote_num}/{total_lotes}: {', '.join(lote)}"
                _janus_progresso["atual"] = i
                _janus_progresso["pct"] = round(i / len(tickers_lista) * 100)

                time.sleep(DELAY_MS)
                dados_lote = buscar_dados_lote(lote)

                lote_para_salvar = []
                for ticker in lote:
                    dados = dados_lote.get(ticker)
                    if dados:
                        lote_para_salvar.append((asset_map[ticker], ticker, dados))
                    else:
                        total_erros += 1

                if lote_para_salvar:
                    try:
                        rankings_lote = salvar_lote_banco(col_conn, lote_para_salvar, source_id)
                        rankings.extend(rankings_lote)
                        total_processados += len(lote_para_salvar)
                    except Exception as e:
                        print(f"[JANUS] Erro lote {lote_num}: {e}", flush=True)
                        total_erros += len(lote_para_salvar)

            if rankings:
                salvar_ranking(col_conn, rankings)

            _janus_progresso["atual"] = len(tickers_lista)
            _janus_progresso["pct"] = 100
            _janus_progresso["ticker_atual"] = f"Concluído! {total_processados} ativos processados"
            finalizar_log(col_conn, log_id, "SUCCESS", total_processados)
            col_conn.commit()

        finally:
            col_conn.close()

    except Exception as e:
        print(f"[JANUS] Erro na coleta: {e}", flush=True)
    finally:
        _janus_rodando = False


def registrar_rotas_janus(app, requer_auth):

    # Limpeza de estado: marca coletas RUNNING como FAILED ao iniciar
    # (evita mostrar "Atualizando..." após reinício do servidor)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE data_ingestion_logs
                SET status='FAILED', finished_at=%s,
                    error_message='Servidor reiniciado durante a coleta'
                WHERE job_name='janus-data-collector' AND status='RUNNING'
            """, (agora().isoformat(),))
            rows = cur.rowcount
        conn.commit(); conn.close()
        if rows > 0:
            print(f"[JANUS] ⚠️ {rows} coleta(s) RUNNING marcada(s) como FAILED (servidor reiniciou)", flush=True)
    except Exception as e:
        print(f"[JANUS] Aviso ao limpar coletas travadas: {e}", flush=True)

    # ── GET /api/janus/status ─────────────────────────────────
    @app.route("/api/janus/status")
    @requer_auth
    def api_janus_status():
        try:
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) as total FROM assets WHERE status='ATIVO'")
                total_ativos = cur.fetchone()["total"]

                cur.execute("SELECT COUNT(*) as total FROM janus_scores")
                total_scores = cur.fetchone()["total"]

                cur.execute("""
                    SELECT started_at, finished_at, status, records_processed
                    FROM data_ingestion_logs
                    WHERE job_name='janus-data-collector'
                    ORDER BY started_at DESC LIMIT 1
                """)
                ultima = cur.fetchone()
            conn.close()

            return jsonify({
                "status":        "online",
                "total_ativos":  total_ativos,
                "total_scores":  total_scores,
                "rodando":       _janus_rodando,  # flag real do processo em memória
                "ultima_coleta": dict(ultima) if ultima else None
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/debug-token (temporário) ───────────────
    @app.route("/api/janus/debug-token")
    def api_janus_debug_token():
        token = os.getenv("BRAPI_TOKEN", "")
        return jsonify({
            "token_length": len(token),
            "token_preview": (token[:6] + "..." + token[-4:]) if len(token) > 10 else token
        })

    # ── GET /api/janus/comentario/<ticker> ────────────────────
    # Gera comentário de agente especialista via IA, sob demanda
    @app.route("/api/janus/comentario/<ticker>")
    @requer_auth
    def api_janus_comentario(ticker):
        try:
            import requests as req
            ticker = ticker.upper()
            ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.asset_id, a.ticker, c.trading_name, c.sector
                    FROM assets a LEFT JOIN companies c ON c.company_id = a.company_id
                    WHERE a.ticker=%s
                """, (ticker,))
                asset = cur.fetchone()
                if not asset:
                    conn.close()
                    return jsonify({"erro": f"Ativo {ticker} não encontrado"}), 404

                cur.execute("""
                    SELECT overall_score, classification, confidence
                    FROM janus_scores WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 1
                """, (asset["asset_id"],))
                score_row = cur.fetchone()

                cur.execute("""
                    SELECT evidence_code, score, trend, explanation
                    FROM evidences WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 10
                """, (asset["asset_id"],))
                evidencias = [dict(r) for r in cur.fetchall()]
            conn.close()

            if not score_row:
                return jsonify({"comentario": "Ainda não há dados suficientes para análise deste ativo."})

            if not ANTHROPIC_KEY:
                return jsonify({"comentario": "Configure ANTHROPIC_API_KEY para habilitar comentários do agente IA."})

            evidencias_txt = "\n".join([
                f"- {e['evidence_code']}: score {e['score']:.1f}, tendência {e['trend']}"
                for e in evidencias
            ])

            prompt = f"""Você é um analista de investimentos experiente e didático. Analise os dados fundamentalistas abaixo e escreva um comentário curto (3-4 frases) sobre {asset['trading_name']} ({ticker}), setor {asset['sector']}.

Janus Score: {float(score_row['overall_score']):.1f}/100 ({score_row['classification']})
Confiança da análise: {float(score_row['confidence']):.0f}%

Evidências que compõem o score:
{evidencias_txt}

Escreva em português, tom profissional mas acessível, destacando os pontos fortes e fracos. NÃO dê recomendação de compra/venda — apenas interprete os fundamentos. Não use formatação markdown, apenas texto corrido."""

            resp = req.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
                json={"model":"claude-sonnet-4-6","max_tokens":300,
                      "messages":[{"role":"user","content":prompt}]},
                timeout=30)

            if resp.status_code == 200:
                comentario = resp.json()["content"][0]["text"].strip()
                return jsonify({"comentario": comentario})
            else:
                return jsonify({"comentario": "Não foi possível gerar o comentário no momento."})

        except Exception as e:
            return jsonify({"comentario": f"Erro ao gerar análise: {str(e)}"})

    # ── GET /api/janus/ranking ────────────────────────────────
    @app.route("/api/janus/ranking")
    @requer_auth
    def api_janus_ranking():
        try:
            limite = int(request.args.get("limit", 50))
            tipo   = request.args.get("tipo", "GERAL")

            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT MAX(reference_date) as ultima
                    FROM ranking_snapshots WHERE ranking_type=%s
                """, (tipo,))
                row = cur.fetchone()
                if not row or not row["ultima"]:
                    conn.close()
                    return jsonify({"ranking": [], "mensagem": "Nenhum dado disponível ainda"})

                ref_date = row["ultima"]

                cur.execute("""
                    SELECT r.general_position, r.sector_position, r.janus_score,
                           r.quality_score, r.reference_date,
                           a.ticker, a.asset_type,
                           c.trading_name, c.sector,
                           js.confidence
                    FROM ranking_snapshots r
                    JOIN assets a ON a.asset_id = r.asset_id
                    LEFT JOIN companies c ON c.company_id = a.company_id
                    LEFT JOIN janus_scores js ON js.asset_id = r.asset_id AND js.reference_date = r.reference_date
                    WHERE r.reference_date=%s AND r.ranking_type=%s
                    ORDER BY r.general_position
                    LIMIT %s
                """, (ref_date, tipo, limite))
                rows = [dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({
                "reference_date": str(ref_date),
                "ranking_type":   tipo,
                "total":          len(rows),
                "ranking":        rows
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/score/<ticker> ─────────────────────────
    @app.route("/api/janus/score/<ticker>")
    @requer_auth
    def api_janus_score(ticker):
        try:
            ticker = ticker.upper()
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.asset_id, a.ticker, c.trading_name, c.sector
                    FROM assets a
                    LEFT JOIN companies c ON c.company_id = a.company_id
                    WHERE a.ticker=%s
                """, (ticker,))
                asset = cur.fetchone()
                if not asset:
                    conn.close()
                    return jsonify({"erro": f"Ativo {ticker} não encontrado"}), 404

                asset_id = asset["asset_id"]

                cur.execute("""
                    SELECT * FROM janus_scores
                    WHERE asset_id=%s ORDER BY reference_date DESC LIMIT 1
                """, (asset_id,))
                score = cur.fetchone()

                cur.execute("""
                    SELECT engine_name, score, confidence, trend, reference_date
                    FROM engine_scores WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 10
                """, (asset_id,))
                engine_scores = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT indicator_code, raw_value, unit, reference_date
                    FROM indicator_values WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 20
                """, (asset_id,))
                indicadores = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT overall_score, reference_date
                    FROM janus_scores WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 30
                """, (asset_id,))
                historico = [dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({
                "ticker":        ticker,
                "empresa":       asset["trading_name"],
                "setor":         asset["sector"],
                "janus_score":   dict(score) if score else None,
                "engine_scores": engine_scores,
                "indicadores":   indicadores,
                "historico":     historico
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/evidence/<ticker> ──────────────────────
    @app.route("/api/janus/evidence/<ticker>")
    @requer_auth
    def api_janus_evidence(ticker):
        try:
            ticker = ticker.upper()
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT asset_id FROM assets WHERE ticker=%s", (ticker,))
                asset = cur.fetchone()
                if not asset:
                    conn.close()
                    return jsonify({"erro": f"Ativo {ticker} não encontrado"}), 404

                cur.execute("""
                    SELECT evidence_code, engine_name, score, confidence,
                           trend, weight, explanation, reference_date
                    FROM evidences WHERE asset_id=%s
                    ORDER BY reference_date DESC LIMIT 20
                """, (asset["asset_id"],))
                evidencias = [dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({
                "ticker":     ticker,
                "total":      len(evidencias),
                "evidencias": evidencias
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/assets ─────────────────────────────────
    @app.route("/api/janus/assets")
    @requer_auth
    def api_janus_assets():
        try:
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.ticker, a.asset_type, a.status,
                           c.trading_name, c.sector
                    FROM assets a
                    LEFT JOIN companies c ON c.company_id = a.company_id
                    WHERE a.status='ATIVO'
                    ORDER BY a.ticker
                """)
                assets = [dict(r) for r in cur.fetchall()]
            conn.close()
            return jsonify({"total": len(assets), "assets": assets})
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/progresso ───────────────────────────────
    @app.route("/api/janus/progresso")
    @requer_auth
    def api_janus_progresso():
        return jsonify({
            "rodando": _janus_rodando,
            "atual": _janus_progresso["atual"],
            "total": _janus_progresso["total"],
            "pct": _janus_progresso["pct"],
            "ticker_atual": _janus_progresso["ticker_atual"]
        })

    # ── POST /api/admin/janus/coletar ─────────────────────────
    @app.route("/api/admin/janus/coletar", methods=["POST"])
    def api_janus_coletar_manual():
        import auth
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.cookies.get("janus_token", "")
        if not auth.verificar_jwt(token):
            return jsonify({"erro": "Acesso negado"}), 403
        if _janus_rodando:
            return jsonify({"ok": False, "mensagem": "Coleta já em andamento"}), 409

        threading.Thread(target=run_collector_com_progresso, daemon=True).start()
        return jsonify({"ok": True, "mensagem": "Coleta iniciada"})

    print("[JANUS] Rotas registradas:")
    print("  GET  /api/janus/status")
    print("  GET  /api/janus/progresso")
    print("  GET  /api/janus/ranking")
    print("  GET  /api/janus/score/<ticker>")
    print("  GET  /api/janus/evidence/<ticker>")
    print("  GET  /api/janus/comentario/<ticker>")
    print("  GET  /api/janus/assets")
    print("  POST /api/admin/janus/coletar")
