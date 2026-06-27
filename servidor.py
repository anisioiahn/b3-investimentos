import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker

VERSION = "1.4.1"

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LIST_URL = "https://brapi.dev/api/quote/list"
QUOTE_URL = "https://brapi.dev/api/quote"

_lock = threading.Lock()
_log_entries = []
_atualizando = False
_cache = {"atualizado_em": None, "setores": {}, "version": VERSION}

FONTES = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

def log(msg, tipo="info"):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 500: _log_entries.pop(0)
    print(f"[{entry['ts']}] {msg}", flush=True)

def req_get(url, tentativas=3):
    for i in range(tentativas):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200: return r
            if r.status_code == 429:
                wait = 15*(i+1)
                log(f"⏳ Rate limit, aguardando {wait}s...", "aviso")
                time.sleep(wait)
                continue
            log(f"⚠️ HTTP {r.status_code} para {url[:60]}", "aviso")
            return None
        except requests.Timeout:
            log(f"⚠️ Timeout ({i+1}/{tentativas})", "aviso")
            time.sleep(2)
        except Exception as e:
            log(f"⚠️ Erro ({i+1}/{tentativas}): {e}", "aviso")
            time.sleep(2)
    return None

def buscar_setores():
    r = req_get(f"{LIST_URL}?limit=1&token={TOKEN}")
    if r:
        setores = r.json().get("availableSectors", [])
        if setores: return setores
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
    r = req_get(f"{QUOTE_URL}/{ticker}?token={TOKEN}")
    if r:
        results = r.json().get("results", [])
        return results[0] if results else None
    return None

def buscar_historico_ticker(ticker):
    r = req_get(f"{QUOTE_URL}/{ticker}?range=1y&interval=1d&token={TOKEN}")
    if r:
        results = r.json().get("results", [])
        if results and results[0].get("historicalDataPrice"):
            hist = results[0]["historicalDataPrice"]
            return [{"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]
    return []

def atualizar_cache():
    global _cache, _atualizando
    with _lock:
        if _atualizando:
            log("⚠️ Já atualizando, ignorando chamada duplicada", "aviso")
            return
        _atualizando = True

    try:
        log(f"🔄 Buscando cotações — v{VERSION}", "info")

        # Testa conexão primeiro
        r = req_get(f"{LIST_URL}?limit=1&token={TOKEN}")
        if not r:
            log("❌ Sem conexão com brapi.dev. Tentando em 60s...", "erro")
            time.sleep(60)
            _atualizando = False
            atualizar_cache()
            return

        setores_api = r.json().get("availableSectors", list(SETOR_MAP.keys()))
        log(f"📋 {len(setores_api)} setores encontrados", "info")

        novo = {"atualizado_em": datetime.now().isoformat(), "setores": {}, "version": VERSION}

        for setor_api in setores_api:
            info = SETOR_MAP.get(setor_api, {"nome": setor_api, "icone": "📈", "cor_fundo": "#f5f5f5"})
            log(f"🔍 {info['nome']}", "setor")
            ativos = buscar_ativos_setor(setor_api)
            log(f"   📊 {len(ativos)} ativos", "info")
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
                    "ticker": ticker, "nome": ativo.get("name", ticker),
                    "cor": cor_para_ticker(ticker), "preco": preco,
                    "variacao": ativo.get("change_abs") or 0,
                    "variacao_pct": variacao_pct,
                    "maxima_dia": ativo.get("high"),
                    "minima_dia": ativo.get("low"),
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
        log(f"❌ Erro: {e}", "erro")
    finally:
        _atualizando = False

def loop_auto():
    time.sleep(INTERVALO)
    while True:
        log(f"⏱️ Atualização automática ({INTERVALO//60} min)", "info")
        atualizar_cache()
        time.sleep(INTERVALO)

# Inicia automaticamente — funciona com Gunicorn e direto
log(f"🚀 App B3 v{VERSION} iniciado", "info")

def iniciar_com_delay():
    """Aguarda o servidor subir completamente antes de buscar."""
    log("⏳ Aguardando servidor inicializar (10s)...", "info")
    time.sleep(10)
    atualizar_cache()

_t1 = threading.Thread(target=iniciar_com_delay, daemon=True)
_t2 = threading.Thread(target=loop_auto, daemon=True)
_t1.start()
_t2.start()

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/version")
def api_version(): return jsonify({"version": VERSION})

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
            "total_setores": len(_cache.get("setores",{})),
            "version": VERSION,
        })

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    if not _atualizando:
        threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True, "atualizando": _atualizando})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    dados = buscar_historico_ticker(ticker.upper())
    return jsonify({"ticker": ticker.upper(), "historico": dados})

@app.route("/api/detalhe/<ticker>")
def api_detalhe(ticker):
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
        })
    return jsonify({"erro": "não encontrado"}), 404

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
        return {"sinal": "NEUTRO", "justificativa": "Configure ANTHROPIC_API_KEY no Render para análise de IA.", "confianca": "Baixa"}
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-6","max_tokens":300,"messages":[{"role":"user","content":
                f"Analise notícias sobre {ticker} ({nome}) e responda APENAS com JSON:\n{chr(10).join(todas[:6])}\nFormato: {{\"sinal\":\"COMPRAR\",\"justificativa\":\"2-3 frases.\",\"confianca\":\"Alta\"}}"}]},
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
    app.run(host="0.0.0.0", port=5000, debug=False)
