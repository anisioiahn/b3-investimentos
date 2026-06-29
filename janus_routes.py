# ============================================================
# JANUS INDEX – ROTAS FLASK v1.0
# Cole as importações e chame registrar_rotas_janus(app) no servidor.py
# ============================================================

import db
from flask import jsonify, request
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BRASILIA)
def hoje():  return agora().strftime("%Y-%m-%d")


def registrar_rotas_janus(app, requer_auth):
    """
    Registra todas as rotas do Janus Index no app Flask.
    Chame no servidor.py assim:
        from janus_routes import registrar_rotas_janus
        registrar_rotas_janus(app, requer_auth)
    """

    # ── GET /api/janus/status ─────────────────────────────────
    # Saúde do sistema: última coleta, total de ativos e scores
    @app.route("/api/janus/status")
    @requer_auth
    def api_janus_status():
        try:
            total_ativos = db.supabase.table("assets") \
                .select("asset_id", count="exact") \
                .eq("status", "ATIVO").execute().count

            total_scores = db.supabase.table("janus_scores") \
                .select("janus_score_id", count="exact").execute().count

            ultima_coleta = db.supabase.table("data_ingestion_logs") \
                .select("started_at,finished_at,status,records_processed") \
                .eq("job_name", "janus-data-collector") \
                .order("started_at", desc=True) \
                .limit(1).execute().data

            return jsonify({
                "status":        "online",
                "total_ativos":  total_ativos,
                "total_scores":  total_scores,
                "ultima_coleta": ultima_coleta[0] if ultima_coleta else None
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/ranking ────────────────────────────────
    # Ranking geral com Janus Score — usado na tela principal do Janus
    @app.route("/api/janus/ranking")
    @requer_auth
    def api_janus_ranking():
        try:
            limite = int(request.args.get("limit", 50))
            tipo   = request.args.get("tipo", "GERAL")

            # Data mais recente disponível
            ultima = db.supabase.table("ranking_snapshots") \
                .select("reference_date") \
                .eq("ranking_type", tipo) \
                .order("reference_date", desc=True) \
                .limit(1).execute().data

            if not ultima:
                return jsonify({"ranking": [], "mensagem": "Nenhum dado disponível ainda"})

            ref_date = ultima[0]["reference_date"]

            ranking = db.supabase.table("ranking_snapshots") \
                .select("general_position,sector_position,janus_score,quality_score,reference_date,assets(ticker,asset_type,companies(trading_name,sector))") \
                .eq("reference_date", ref_date) \
                .eq("ranking_type", tipo) \
                .order("general_position") \
                .limit(limite).execute().data

            return jsonify({
                "reference_date": ref_date,
                "ranking_type":   tipo,
                "total":          len(ranking),
                "ranking":        ranking
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/score/<ticker> ─────────────────────────
    # Score completo de um ativo com indicadores e engine scores
    @app.route("/api/janus/score/<ticker>")
    @requer_auth
    def api_janus_score(ticker):
        try:
            ticker = ticker.upper()

            asset = db.supabase.table("assets") \
                .select("asset_id,ticker,companies(trading_name,sector)") \
                .eq("ticker", ticker).execute().data

            if not asset:
                return jsonify({"erro": f"Ativo {ticker} não encontrado"}), 404

            asset_id = asset[0]["asset_id"]

            score = db.supabase.table("janus_scores") \
                .select("*") \
                .eq("asset_id", asset_id) \
                .order("reference_date", desc=True) \
                .limit(1).execute().data

            engine_scores = db.supabase.table("engine_scores") \
                .select("engine_name,score,confidence,trend,reference_date") \
                .eq("asset_id", asset_id) \
                .order("reference_date", desc=True) \
                .limit(10).execute().data

            indicadores = db.supabase.table("indicator_values") \
                .select("indicator_code,raw_value,unit,reference_date") \
                .eq("asset_id", asset_id) \
                .order("reference_date", desc=True) \
                .limit(20).execute().data

            empresa = asset[0].get("companies") or {}

            return jsonify({
                "ticker":        ticker,
                "empresa":       empresa.get("trading_name"),
                "setor":         empresa.get("sector"),
                "janus_score":   score[0] if score else None,
                "engine_scores": engine_scores,
                "indicadores":   indicadores
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/evidence/<ticker> ──────────────────────
    # Evidências que explicam o score — transparência do Janus
    @app.route("/api/janus/evidence/<ticker>")
    @requer_auth
    def api_janus_evidence(ticker):
        try:
            ticker = ticker.upper()

            asset = db.supabase.table("assets") \
                .select("asset_id") \
                .eq("ticker", ticker).execute().data

            if not asset:
                return jsonify({"erro": f"Ativo {ticker} não encontrado"}), 404

            evidencias = db.supabase.table("evidences") \
                .select("evidence_code,engine_name,score,confidence,trend,weight,explanation,reference_date") \
                .eq("asset_id", asset[0]["asset_id"]) \
                .order("reference_date", desc=True) \
                .limit(20).execute().data

            return jsonify({
                "ticker":    ticker,
                "total":     len(evidencias),
                "evidencias": evidencias
            })
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── GET /api/janus/assets ─────────────────────────────────
    # Lista todos os ativos cobertos pelo Janus
    @app.route("/api/janus/assets")
    @requer_auth
    def api_janus_assets():
        try:
            assets = db.supabase.table("assets") \
                .select("ticker,asset_type,status,companies(trading_name,sector)") \
                .eq("status", "ATIVO") \
                .order("ticker").execute().data

            return jsonify({"total": len(assets), "assets": assets})
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # ── POST /api/admin/janus/coletar ─────────────────────────
    # Dispara coleta manual via painel admin
    @app.route("/api/admin/janus/coletar", methods=["POST"])
    def api_janus_coletar_manual():
        # Verifica token admin
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.cookies.get("janus_admin_token", "")
        import auth
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
