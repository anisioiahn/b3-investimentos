import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LIST_URL = "https://brapi.dev/api/quote/list"
QUOTE_URL = "https://brapi.dev/api/quote"
OUTPUT_FILE = "cotacoes.json"

_lock = threading.Lock()
_log_entries = []
_atualizando = False
_cache = {"atualizado_em": None, "setores": {}}

FONTES = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

def log(msg, tipo="info"):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 500: _log_entries.pop(0)
    print(f"[{entry['ts']}] {msg}")

def req_get(url, tentativas=3):
    """GET com retry automático em 429."""
    for i in range(tentativas):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200: return r
            if r.status_code == 429:
                wait = 15 * (i+1)
                log(f"⏳ Rate limit, aguardando {wait}s...", "aviso")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            log(f"⚠️ Erro rede: {e}", "aviso")
            time.sleep(3)
    return None

def buscar_setores():
    r = req_get(f"{LIST_URL}?limit=1&token={TOKEN}")
    if r: return r.json().get("availableSectors", [])
    return list(SETOR_MAP.keys())

def buscar_ativos_setor(setor):
    todos = []
    for pg in range(1, 4):
        r = req_get(f"{LIST_URL}?sector={setor}&type=stock&sortBy=market_cap_basic&sortOrder=desc&limit=50&page={pg}&token={TOKEN}")
        if not r: break
        data = r.json()
        todos.extend(data.get("stocks", []))
        if not data.get("hasNextPage"): break
        time.sleep(0.5)
    return todos

def buscar_detalhe_ticker(ticker):
    """Busca detalhes completos de 1 ticker (mín, máx, variação real)."""
    r = req_get(f"{QUOTE_URL}/{ticker}?token={TOKEN}")
    if r:
        results = r.json().get("results", [])
        return results[0] if results else None
    return None

def buscar_historico_ticker(ticker):
    """Busca histórico de 1 ano."""
    r = req_get(f"{QUOTE_URL}/{ticker}?range=1y&interval=1d&token={TOKEN}")
    if r:
        results = r.json().get("results", [])
        if results and results[0].get("historicalDataPrice"):
            hist = results[0]["historicalDataPrice"]
            return [{"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]
    return []

def atualizar_cache():
    global _cache, _atualizando
    if _atualizando: return
    _atualizando = True
    try:
        log("🔄 Iniciando busca de cotações...", "info")
        setores_api = buscar_setores()
        log(f"📋 {len(setores_api)} setores encontrados", "info")
        novo = {"atualizado_em": datetime.now().isoformat(), "setores": {}}

        for setor_api in setores_api:
            info = SETOR_MAP.get(setor_api, {"nome": setor_api, "icone": "📈", "cor_fundo": "#f5f5f5"})
            log(f"🔍 {info['nome']}", "setor")
            ativos = buscar_ativos_setor(setor_api)
            empresas = []
            for ativo in ativos:
                ticker = ativo.get("stock", "")
                if not ticker: continue
                preco = ativo.get("close")
                variacao_pct = ativo.get("change") or 0
                variacao = ativo.get("change_abs") or 0
                # Pega mín/máx da listagem (quando disponível)
                minima = ativo.get("low")
                maxima = ativo.get("high")
                if preco:
                    sinal = "▲" if variacao_pct >= 0 else "▼"
                    log(f"   {sinal} {ticker}: R$ {preco} ({variacao_pct:+.2f}%)", "cotacao")
                empresas.append({
                    "ticker": ticker,
                    "nome": ativo.get("name", ticker),
                    "cor": cor_para_ticker(ticker),
                    "preco": preco,
                    "variacao": variacao,
                    "variacao_pct": variacao_pct,
                    "maxima_dia": maxima,
                    "minima_dia": minima,
                    "volume": ativo.get("volume"),
                    "logo": ativo.get("logourl", ""),
                })
                time.sleep(0.1)

            setor_id = setor_api.lower().replace(" ", "_")
            novo["setores"][setor_id] = {
                "nome": info["nome"], "icone": info["icone"],
                "cor_fundo": info["cor_fundo"],
                "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
            }

        with _lock: _cache = novo
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

@app.route("/api/status")
def api_status():
    with _lock:
        total = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
        return jsonify({
            "pronto": total > 0,
            "atualizando": _atualizando,
            "total_ativos": total,
        })

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    if not _atualizando:
        threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True, "atualizando": _atualizando})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    """Busca histórico individual — usa endpoint /quote com range."""
    dados = buscar_historico_ticker(ticker.upper())
    return jsonify({"ticker": ticker.upper(), "historico": dados})

@app.route("/api/detalhe/<ticker>")
def api_detalhe(ticker):
    """Busca detalhes completos do ticker (mín, máx, variação real)."""
    d = buscar_detalhe_ticker(ticker.upper())
    if d:
        return jsonify({
            "ticker": ticker.upper(),
            "preco": d.get("regularMarketPrice"),
            "variacao": d.get("regularMarketChange"),
            "variacao_pct": d.get("regularMarketChangePercent"),
            "minima_dia": d.get("regularMarketDayLow"),
            "maxima_dia": d.get("regularMarketDayHigh"),
            "volume": d.get("regularMarketVolume"),
            "nome": d.get("shortName") or d.get("longName"),
        })
    return jsonify({"erro": "ticker não encontrado"}), 404

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
    todas = [f"[{f}] {n['titulo']}: {n['resumo']}" for f, items in noticias.items() for n in items]
    if not todas:
        return {"sinal": "NEUTRO", "justificativa": "Sem notícias recentes para análise.", "confianca": "Baixa"}
    if not ANTHROPIC_KEY:
        return {"sinal": "NEUTRO", "justificativa": "Configure ANTHROPIC_API_KEY no Render para habilitar análise de IA.", "confianca": "Baixa"}
    prompt = f"""Analise as notícias sobre {ticker} ({nome}) e responda APENAS com JSON válido:
{chr(10).join(todas[:6])}
Formato exato: {{"sinal":"COMPRAR","justificativa":"2-3 frases.","confianca":"Alta"}}
sinal deve ser: COMPRAR, VENDER ou NEUTRO. confianca: Alta, Média ou Baixa."""
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
def api_logs():
    desde = request.args.get("desde", 0, type=int)
    return jsonify(_log_entries[desde:])

if __name__ == "__main__":
    log("🚀 Servidor iniciado — buscando cotações...", "info")
    threading.Thread(target=atualizar_cache, daemon=True).start()
    threading.Thread(target=loop_auto, daemon=True).start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
