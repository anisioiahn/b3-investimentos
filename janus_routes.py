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
                           c.trading_name, c.sector
                    FROM ranking_snapshots r
                    JOIN assets a ON a.asset_id = r.asset_id
                    LEFT JOIN companies c ON c.company_id = a.company_id
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
            conn.close()

            return jsonify({
                "ticker":        ticker,
                "empresa":       asset["trading_name"],
                "setor":         asset["sector"],
                "janus_score":   dict(score) if score else None,
                "engine_scores": engine_scores,
                "indicadores":   indicadores
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
    print("  GET  /api/janus/assets")
    print("  POST /api/admin/janus/coletar")
