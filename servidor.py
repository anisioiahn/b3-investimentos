import os, json, threading, time, queue, requests, xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, Response, request
from buscar_cotacoes import buscar_historico, buscar_noticias_rss, OUTPUT_FILE, SETOR_MAP, cor_para_ticker

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LIST_URL = "https://brapi.dev/api/quote/list"

_lock = threading.Lock()
_log_entries = []
_atualizando = False

# ── Carrega cache do disco ao iniciar ────────────────────────
def carregar_cache_disco():
    try:
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            total = sum(len(s.get("empresas",[])) for s in dados.get("setores",{}).values())
            if total > 0:
                log(f"📂 Cache carregado do disco: {total} ativos em {len(dados['setores'])} setores", "sucesso")
                return dados
    except Exception as e:
        log(f"⚠️ Erro ao carregar cache: {e}", "aviso")
    return {"atualizado_em": None, "setores": {}}

_cache = carregar_cache_disco()

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

def buscar_setores():
    try:
        resp = requests.get(f"{LIST_URL}?limit=1&token={TOKEN}", timeout=15)
        if resp.status_code == 200:
            return resp.json().get("availableSectors", [])
    except Exception as e:
        log(f"❌ Erro ao buscar setores: {e}", "erro")
    return list(SETOR_MAP.keys())

def buscar_ativos_setor(setor):
    todos = []
    for pagina in range(1, 4):
        try:
            url = f"{LIST_URL}?sector={setor}&type=stock&sortBy=market_cap_basic&sortOrder=desc&limit=50&page={pagina}&token={TOKEN}"
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                stocks = data.get("stocks", [])
                todos.extend(stocks)
                if not data.get("hasNextPage"): break
                time.sleep(0.5)
            elif resp.status_code == 429:
                log(f"⏳ Rate limit, aguardando 15s...", "aviso")
                time.sleep(15)
            else:
                break
        except Exception as e:
            log(f"❌ Erro setor {setor}: {e}", "erro")
            break
    return todos

def atualizar_cache():
    global _cache, _atualizando
    if _atualizando:
        return
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
                if preco:
                    sinal = "▲" if variacao_pct >= 0 else "▼"
                    log(f"   {sinal} {ticker}: R$ {preco} ({variacao_pct:+.2f}%)", "cotacao")
                empresas.append({
                    "ticker": ticker, "nome": ativo.get("name", ticker),
                    "cor": cor_para_ticker(ticker), "preco": preco,
                    "variacao": ativo.get("change_abs"), "variacao_pct": variacao_pct,
                    "maxima_dia": ativo.get("high"), "minima_dia": ativo.get("low"),
                    "volume": ativo.get("volume"), "logo": ativo.get("logourl", ""),
                })
                time.sleep(0.1)

            setor_id = setor_api.lower().replace(" ", "_")
            novo["setores"][setor_id] = {
                "nome": info["nome"], "icone": info["icone"],
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

@app.route("/api/status")
def api_status():
    with _lock:
        total = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
        return jsonify({
            "pronto": total > 0,
            "atualizando": _atualizando,
            "total_ativos": total,
            "total_setores": len(_cache.get("setores",{})),
            "atualizado_em": _cache.get("atualizado_em"),
        })

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
    todas = [f"[{f}] {n['titulo']}: {n['resumo']}" for f, items in noticias.items() for n in items]
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
def api_logs():
    """Retorna todos os logs — front-end faz polling a cada 3s."""
    desde = request.args.get("desde", 0, type=int)
    return jsonify(_log_entries[desde:])

if __name__ == "__main__":
    log("🚀 Servidor iniciado", "info")
    threading.Thread(target=atualizar_cache, daemon=True).start()
    threading.Thread(target=loop_auto, daemon=True).start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
