import os, json, threading, time, queue, requests, xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, Response, stream_with_context, request
from buscar_cotacoes import buscar_historico, buscar_noticias_rss, OUTPUT_FILE, SETOR_MAP, cor_para_ticker

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LIST_URL = "https://brapi.dev/api/quote/list"

_lock = threading.Lock()
_cache = {"atualizado_em": None, "setores": {}}
_log_entries = []
_sse_clients = []
_sse_lock = threading.Lock()
_atualizando = False

FONTES = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

def log(msg, tipo="info"):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 300: _log_entries.pop(0)
    broadcast_sse(entry)
    print(f"[{entry['ts']}] {msg}")

def broadcast_sse(entry):
    data = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.put_nowait(data)
            except: dead.append(q)
        for q in dead: _sse_clients.remove(q)

def buscar_setores():
    """Busca todos os setores disponíveis na brapi."""
    try:
        resp = requests.get(f"{LIST_URL}?limit=1&token={TOKEN}", timeout=15)
        if resp.status_code == 200:
            return resp.json().get("availableSectors", [])
    except Exception as e:
        log(f"❌ Erro ao buscar setores: {e}", "erro")
    return list(SETOR_MAP.keys())

def buscar_ativos_setor(setor):
    """Busca ativos de um setor com paginação."""
    todos = []
    pagina = 1
    while True:
        try:
            url = f"{LIST_URL}?sector={setor}&type=stock&sortBy=market_cap_basic&sortOrder=desc&limit=50&page={pagina}&token={TOKEN}"
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                stocks = data.get("stocks", [])
                todos.extend(stocks)
                if not data.get("hasNextPage") or pagina >= 3:
                    break
                pagina += 1
                time.sleep(0.5)
            elif resp.status_code == 429:
                log(f"⏳ Rate limit, aguardando 15s...", "aviso")
                time.sleep(15)
            else:
                log(f"⚠️ HTTP {resp.status_code} para setor {setor}", "aviso")
                break
        except Exception as e:
            log(f"❌ Erro setor {setor}: {e}", "erro")
            break
    return todos

def atualizar_cache():
    global _cache, _atualizando
    if _atualizando:
        log("⚠️ Atualização já em andamento, ignorando", "aviso")
        return
    _atualizando = True
    try:
        log("🔄 Iniciando busca de cotações via brapi.dev...", "info")
        setores_api = buscar_setores()
        log(f"📋 {len(setores_api)} setores encontrados", "info")

        novo = {"atualizado_em": datetime.now().isoformat(), "setores": {}}

        for setor_api in setores_api:
            info = SETOR_MAP.get(setor_api, {"nome": setor_api, "icone": "📈", "cor_fundo": "#f5f5f5"})
            log(f"🔍 Buscando: {info['nome']}", "setor")
            ativos = buscar_ativos_setor(setor_api)
            log(f"   📊 {len(ativos)} ativos encontrados", "info")

            empresas = []
            for ativo in ativos:
                ticker = ativo.get("stock", "")
                if not ticker: continue
                preco = ativo.get("close")
                variacao_pct = ativo.get("change") or 0
                if preco:
                    sinal = "▲" if variacao_pct >= 0 else "▼"
                    log(f"   {sinal} {ticker}: R$ {preco} ({variacao_pct:+.2f}%)", "cotacao")
                empresas.append({
                    "ticker": ticker,
                    "nome": ativo.get("name", ticker),
                    "cor": cor_para_ticker(ticker),
                    "preco": preco,
                    "variacao": ativo.get("change_abs"),
                    "variacao_pct": variacao_pct,
                    "maxima_dia": ativo.get("high"),
                    "minima_dia": ativo.get("low"),
                    "volume": ativo.get("volume"),
                    "logo": ativo.get("logourl", ""),
                })
                time.sleep(0.1)

            setor_id = setor_api.lower().replace(" ", "_")
            novo["setores"][setor_id] = {
                "nome": info["nome"],
                "icone": info["icone"],
                "cor_fundo": info["cor_fundo"],
                "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
            }

        with _lock: _cache = novo
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(novo, f, ensure_ascii=False, indent=2)

        total = sum(len(s["empresas"]) for s in novo["setores"].values())
        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ Concluído! {com_preco}/{total} ativos em {len(novo['setores'])} setores", "sucesso")

    except Exception as e:
        log(f"❌ Erro geral: {e}", "erro")
    finally:
        _atualizando = False

def loop_auto():
    time.sleep(INTERVALO)
    while True:
        log(f"⏱️ Atualização automática ({INTERVALO//60} min)", "info")
        atualizar_cache()
        time.sleep(INTERVALO)

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/cotacoes")
def api_cotacoes():
    with _lock: return jsonify(_cache)

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    if not _atualizando:
        threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True, "em_andamento": _atualizando})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    dados = buscar_historico(ticker.upper())
    return jsonify({"ticker": ticker.upper(), "historico": dados})

@app.route("/api/noticias/<ticker>")
def api_noticias(ticker):
    nome = ticker
    with _lock:
        for s in _cache.get("setores", {}).values():
            co = next((e for e in s["empresas"] if e["ticker"] == ticker.upper()), None)
            if co: nome = co.get("nome", ticker); break
    noticias = buscar_noticias_rss(ticker.upper(), nome, FONTES)
    recomendacao = gerar_recomendacao(ticker.upper(), nome, noticias)
    return jsonify({"ticker": ticker.upper(), "nome": nome, "noticias": noticias, "recomendacao": recomendacao})

@app.route("/api/fontes", methods=["GET", "POST"])
def api_fontes():
    global FONTES
    if request.method == "POST":
        FONTES = request.json.get("fontes", FONTES)
        return jsonify({"ok": True})
    return jsonify(FONTES)

def gerar_recomendacao(ticker, nome, noticias):
    todas = []
    for fonte, items in noticias.items():
        for n in items:
            todas.append(f"[{fonte}] {n['titulo']}: {n['resumo']}")
    if not todas:
        return {"sinal": "NEUTRO", "justificativa": "Sem notícias recentes para análise.", "confianca": "Baixa"}
    if not ANTHROPIC_KEY:
        return {"sinal": "NEUTRO", "justificativa": "Configure ANTHROPIC_API_KEY no Render para habilitar análise de IA.", "confianca": "Baixa"}
    prompt = f"""Analise as notícias sobre {ticker} ({nome}) e responda APENAS com JSON:
{chr(10).join(todas[:6])}
Formato: {{"sinal":"COMPRAR","justificativa":"2-3 frases.","confianca":"Alta"}}
sinal: COMPRAR, VENDER ou NEUTRO. confianca: Alta, Média ou Baixa."""
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-6","max_tokens":300,"messages":[{"role":"user","content":prompt}]},
            timeout=30)
        if resp.status_code == 200:
            texto = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
            return json.loads(texto)
    except Exception as e:
        log(f"⚠️ Recomendação {ticker}: {e}", "aviso")
    return {"sinal": "NEUTRO", "justificativa": "Erro ao gerar análise.", "confianca": "Baixa"}

@app.route("/api/logs")
def api_logs(): return jsonify(_log_entries)

@app.route("/api/logs/stream")
def api_logs_stream():
    q = queue.Queue(maxsize=100)
    with _sse_lock: _sse_clients.append(q)
    def generate():
        yield f"data: {json.dumps({'ts':'','msg':'Conectado ao log em tempo real','tipo':'info'})}\n\n"
        while True:
            try: yield q.get(timeout=30)
            except queue.Empty: yield "data: {\"ping\":true}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    log("🚀 Servidor iniciado", "info")
    # Uma única thread de atualização inicial
    threading.Thread(target=atualizar_cache, daemon=True).start()
    # Uma única thread de loop automático
    threading.Thread(target=loop_auto, daemon=True).start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
