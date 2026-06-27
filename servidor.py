import os, json, threading, time, queue, requests, xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, Response, stream_with_context, request
from buscar_cotacoes import buscar_todas_cotacoes, buscar_historico, buscar_noticias_rss, OUTPUT_FILE

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_lock = threading.Lock()
_cache = {"atualizado_em": None, "setores": {}}
_log_entries = []
_sse_clients = []
_sse_lock = threading.Lock()

# Fontes de notícias padrão
FONTES_PADRAO = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

def log(msg, tipo="info"):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 200:
        _log_entries.pop(0)
    broadcast_sse(entry)

def broadcast_sse(entry):
    data = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.put_nowait(data)
            except: dead.append(q)
        for q in dead: _sse_clients.remove(q)

def atualizar_cache():
    global _cache
    log("🔄 Iniciando busca de cotações via brapi.dev...", "info")
    try:
        novo = buscar_todas_cotacoes_com_log()
        with _lock: _cache = novo
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(novo, f, ensure_ascii=False, indent=2)
        total = sum(len(s["empresas"]) for s in novo["setores"].values())
        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ Concluído! {com_preco}/{total} ativos com cotação em {len(novo['setores'])} setores", "sucesso")
    except Exception as e:
        log(f"❌ Erro: {e}", "erro")

def buscar_todas_cotacoes_com_log():
    """Versão com logging em tempo real."""
    from buscar_cotacoes import buscar_setores_disponiveis, buscar_ativos_por_setor, cor_para_ticker, SETOR_MAP
    resultado = {"atualizado_em": datetime.now().isoformat(), "setores": {}}
    setores_api = buscar_setores_disponiveis()
    log(f"📋 {len(setores_api)} setores encontrados na B3", "info")

    for setor_api in setores_api:
        info = SETOR_MAP.get(setor_api, {"nome": setor_api, "icone": "📈", "cor_fundo": "#f5f5f5"})
        log(f"🔍 Buscando: {info['nome']}", "setor")
        ativos, _ = buscar_ativos_por_setor(setor_api, limite=30)
        empresas = []
        for ativo in ativos:
            ticker = ativo.get("stock", "")
            if not ticker: continue
            preco = ativo.get("close")
            variacao_pct = ativo.get("change")
            if preco:
                sinal = "▲" if (variacao_pct or 0) >= 0 else "▼"
                log(f"   {sinal} {ticker}: R$ {preco}", "cotacao")
            empresas.append({
                "ticker": ticker, "nome": ativo.get("name", ticker),
                "cor": cor_para_ticker(ticker), "preco": preco,
                "variacao": ativo.get("change_abs"), "variacao_pct": variacao_pct,
                "maxima_dia": ativo.get("high"), "minima_dia": ativo.get("low"),
                "volume": ativo.get("volume"), "logo": ativo.get("logourl", ""),
            })
            time.sleep(0.3)
        setor_id = setor_api.lower().replace(" ", "_")
        resultado["setores"][setor_id] = {
            "nome": info["nome"], "icone": info["icone"], "cor_fundo": info["cor_fundo"],
            "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    return resultado

def loop_auto():
    while True:
        time.sleep(INTERVALO)
        log(f"⏱️ Atualização automática a cada {INTERVALO//60} min", "info")
        atualizar_cache()

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/cotacoes")
def api_cotacoes():
    with _lock: return jsonify(_cache)

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    dados = buscar_historico(ticker.upper())
    return jsonify({"ticker": ticker.upper(), "historico": dados})

@app.route("/api/noticias/<ticker>")
def api_noticias(ticker):
    """Busca notícias das fontes configuradas e gera recomendação via Claude."""
    # Busca nome da empresa
    nome = ticker
    with _lock:
        for s in _cache.get("setores", {}).values():
            co = next((e for e in s["empresas"] if e["ticker"] == ticker.upper()), None)
            if co: nome = co.get("nome", ticker); break

    fontes = FONTES_PADRAO
    noticias = buscar_noticias_rss(ticker.upper(), nome, fontes)
    recomendacao = gerar_recomendacao(ticker.upper(), nome, noticias)
    return jsonify({"ticker": ticker.upper(), "nome": nome, "noticias": noticias, "recomendacao": recomendacao})

@app.route("/api/fontes", methods=["GET", "POST"])
def api_fontes():
    global FONTES_PADRAO
    if request.method == "POST":
        FONTES_PADRAO = request.json.get("fontes", FONTES_PADRAO)
        return jsonify({"ok": True})
    return jsonify(FONTES_PADRAO)

def gerar_recomendacao(ticker, nome, noticias):
    """Usa a API do Claude para analisar notícias e gerar recomendação."""
    todas = []
    for fonte, items in noticias.items():
        for n in items:
            todas.append(f"[{fonte}] {n['titulo']}: {n['resumo']}")
    
    if not todas:
        return {"sinal": "NEUTRO", "justificativa": "Sem notícias recentes para análise.", "confianca": "Baixa"}

    prompt = f"""Analise as seguintes notícias recentes sobre a ação {ticker} ({nome}) da bolsa brasileira B3 e gere uma recomendação de investimento.

Notícias:
{chr(10).join(todas[:9])}

Responda APENAS com um JSON válido neste formato exato:
{{"sinal": "COMPRAR", "justificativa": "Resumo da análise em 2-3 frases.", "confianca": "Alta"}}

O campo "sinal" deve ser exatamente: COMPRAR, VENDER ou NEUTRO.
O campo "confianca" deve ser: Alta, Média ou Baixa.
Não inclua nada além do JSON."""

    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if resp.status_code == 200:
            texto = resp.json()["content"][0]["text"].strip()
            texto = texto.replace("```json", "").replace("```", "").strip()
            return json.loads(texto)
    except Exception as e:
        log(f"⚠️ Recomendação {ticker}: {e}", "aviso")
    
    return {"sinal": "NEUTRO", "justificativa": "Não foi possível gerar análise automática.", "confianca": "Baixa"}

@app.route("/api/logs")
def api_logs(): return jsonify(_log_entries)

@app.route("/api/logs/stream")
def api_logs_stream():
    q = queue.Queue(maxsize=50)
    with _sse_lock: _sse_clients.append(q)
    def generate():
        yield f"data: {json.dumps({'ts':'','msg':'Conectado ao log em tempo real','tipo':'info'}, ensure_ascii=False)}\n\n"
        while True:
            try: yield q.get(timeout=30)
            except queue.Empty: yield "data: {\"ping\":true}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    log("🚀 Servidor iniciado", "info")
    threading.Thread(target=atualizar_cache, daemon=True).start()
    threading.Thread(target=loop_auto, daemon=True).start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
