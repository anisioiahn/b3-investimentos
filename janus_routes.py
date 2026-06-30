# ============================================================
# JANUS INDEX – ROTAS FLASK v1.1
# Segue o padrão do projeto: psycopg2 + get_conn()
# Chame no servidor.py:
#   from janus_routes import registrar_rotas_janus
#   registrar_rotas_janus(app, requer_auth)
# ============================================================

import psycopg2, psycopg2.extras, os
from flask import jsonify, request
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora():    return datetime.now(TZ_BRASILIA)
def hoje():     return agora().strftime("%Y-%m-%d")

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")


def registrar_rotas_janus(app, requer_auth):

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

    # ── POST /api/admin/janus/coletar ─────────────────────────
    @app.route("/api/admin/janus/coletar", methods=["POST"])
    def api_janus_coletar_manual():
        import auth
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.cookies.get("janus_admin_token", "")
        if not auth.verificar_jwt_admin(token):
            return jsonify({"erro": "Acesso negado"}), 403

        import threading
        from janus_collector import run_collector
        threading.Thread(target=run_collector, daemon=True).start()
        return jsonify({"ok": True, "mensagem": "Coleta iniciada"})

    print("[JANUS] Rotas registradas:")
    print("  GET  /api/janus/status")
    print("  GET  /api/janus/ranking")
    print("  GET  /api/janus/score/<ticker>")
    print("  GET  /api/janus/evidence/<ticker>")
    print("  GET  /api/janus/comentario/<ticker>")
    print("  GET  /api/janus/assets")
    print("  POST /api/admin/janus/coletar")
