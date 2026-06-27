import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker

VERSION = "1.7.2"

# Fuso horário de Brasília (UTC-3)
TZ_BRASILIA = timezone(timedelta(hours=-3))

def agora():
    """Retorna datetime atual no horário de Brasília."""
    return datetime.now(TZ_BRASILIA)

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUOTE_URL = "https://brapi.dev/api/quote"

_cache = {"atualizado_em": None, "setores": {}, "version": VERSION}
_log_entries = []
_atualizando = False

# Setores e tickers fixos — igual à versão que funcionava
SETORES = {
    "petroleo":         {"nome":"Petróleo, Gás e Biocombustíveis","icone":"🛢️","cor_fundo":"#e8f5e9","tickers":{"PETR4":{"nome":"Petrobras PN","cor":"#005a2b"},"PETR3":{"nome":"Petrobras ON","cor":"#007a3d"},"PRIO3":{"nome":"PetroRio","cor":"#1b5e20"},"RECV3":{"nome":"Petrorecôncavo","cor":"#2e7d32"},"VBBR3":{"nome":"Vibra Energia","cor":"#388e3c"}}},
    "utilidade":        {"nome":"Utilidade Pública","icone":"⚡","cor_fundo":"#fff8e1","tickers":{"ENGI11":{"nome":"Energisa","cor":"#f9a825"},"CPFE3":{"nome":"CPFL Energia","cor":"#b71c1c"},"TAEE11":{"nome":"Taesa","cor":"#00695c"},"EQTL3":{"nome":"Equatorial","cor":"#1565c0"},"CMIG4":{"nome":"Cemig","cor":"#7b1fa2"},"EGIE3":{"nome":"Engie Brasil","cor":"#0d47a1"},"CPLE3":{"nome":"Copel","cor":"#283593"}}},
    "materiais":        {"nome":"Materiais Básicos","icone":"🪨","cor_fundo":"#efebe9","tickers":{"VALE3":{"nome":"Vale","cor":"#1a5276"},"CSAN3":{"nome":"Cosan","cor":"#1a237e"},"SUZB3":{"nome":"Suzano","cor":"#1b5e20"},"KLBN11":{"nome":"Klabin","cor":"#33691e"},"DXCO3":{"nome":"Dexco","cor":"#5d4037"},"GGBR4":{"nome":"Gerdau","cor":"#37474f"},"CSNA3":{"nome":"CSN","cor":"#263238"},"GOAU4":{"nome":"Metalúrgica Gerdau","cor":"#455a64"}}},
    "industriais":      {"nome":"Bens Industriais","icone":"🏗️","cor_fundo":"#fffde7","tickers":{"WEGE3":{"nome":"WEG","cor":"#003366"},"EMBJ3":{"nome":"Embraer","cor":"#003a80"},"RAIL3":{"nome":"Rumo","cor":"#bf360c"},"UGPA3":{"nome":"Ultrapar","cor":"#e65100"},"CYRE3":{"nome":"Cyrela","cor":"#1565c0"},"MRVE3":{"nome":"MRV","cor":"#f57f17"},"EZTC3":{"nome":"EZTEC","cor":"#004d40"},"DIRR3":{"nome":"Direcional","cor":"#c62828"},"TEND3":{"nome":"Tenda","cor":"#1a237e"},"CCRO3":{"nome":"CCR","cor":"#0277bd"}}},
    "financeiro":       {"nome":"Financeiro","icone":"🏦","cor_fundo":"#e3f2fd","tickers":{"ITUB4":{"nome":"Itaú Unibanco","cor":"#ff6600"},"BBDC4":{"nome":"Bradesco","cor":"#cc0000"},"BBAS3":{"nome":"Banco do Brasil","cor":"#003399"},"SANB11":{"nome":"Santander BR","cor":"#cc0000"},"B3SA3":{"nome":"B3 S.A.","cor":"#003a80"},"BPAC11":{"nome":"BTG Pactual","cor":"#1a1a2e"},"ITSA4":{"nome":"Itaúsa","cor":"#e65100"},"CIEL3":{"nome":"Cielo","cor":"#ffaa00"}}},
    "consumo_nciclico": {"nome":"Consumo Não Cíclico","icone":"🌾","cor_fundo":"#f1f8e9","tickers":{"ABEV3":{"nome":"Ambev","cor":"#f9a825"},"JBSS3":{"nome":"JBS","cor":"#c62828"},"BEEF3":{"nome":"Minerva","cor":"#bf360c"},"SLCE3":{"nome":"SLC Agrícola","cor":"#33691e"},"SMTO3":{"nome":"São Martinho","cor":"#2e7d32"},"AGRO3":{"nome":"BrasilAgro","cor":"#1b5e20"},"MRFG3":{"nome":"Marfrig","cor":"#e53935"}}},
    "consumo_ciclico":  {"nome":"Consumo Cíclico","icone":"🛍️","cor_fundo":"#f3e5f5","tickers":{"LREN3":{"nome":"Lojas Renner","cor":"#c62828"},"ASAI3":{"nome":"Assaí","cor":"#e53935"},"MGLU3":{"nome":"Magazine Luiza","cor":"#0000cc"},"SOMA3":{"nome":"Grupo Soma","cor":"#6a1b9a"},"ARZZ3":{"nome":"Arezzo","cor":"#880e4f"},"SBFG3":{"nome":"SBF Group","cor":"#e65100"},"ALPA4":{"nome":"Alpargatas","cor":"#0288d1"}}},
    "saude":            {"nome":"Saúde","icone":"🏥","cor_fundo":"#ffebee","tickers":{"RDOR3":{"nome":"Rede D'Or","cor":"#c62828"},"HAPV3":{"nome":"Hapvida","cor":"#0277bd"},"FLRY3":{"nome":"Fleury","cor":"#1565c0"},"HYPE3":{"nome":"Hypera","cor":"#006064"},"DASA3":{"nome":"Dasa","cor":"#0288d1"},"QUAL3":{"nome":"Qualicorp","cor":"#00838f"}}},
    "comunicacoes":     {"nome":"Comunicações","icone":"📡","cor_fundo":"#e0f2f1","tickers":{"VIVT3":{"nome":"Telefônica Vivo","cor":"#6200ea"},"TIMS3":{"nome":"TIM","cor":"#0000cc"},"OIBR3":{"nome":"Oi","cor":"#f57f17"}}},
    "tecnologia":       {"nome":"Tecnologia da Informação","icone":"💻","cor_fundo":"#ede7f6","tickers":{"TOTS3":{"nome":"TOTVS","cor":"#e53935"},"LWSA3":{"nome":"Locaweb","cor":"#0033cc"},"INTB3":{"nome":"Intelbras","cor":"#1a237e"},"POSI3":{"nome":"Positivo","cor":"#1565c0"}}},
    "imobiliario":      {"nome":"Imobiliário","icone":"🏢","cor_fundo":"#fce4ec","tickers":{"CYRE3":{"nome":"Cyrela","cor":"#1565c0"},"MULT3":{"nome":"Multiplan","cor":"#880e4f"}," IGTI11":{"nome":"Iguatemi","cor":"#4a148c"},"BRPR3":{"nome":"BR Properties","cor":"#37474f"}}},
    "papel_celulose":   {"nome":"Papel e Celulose","icone":"🌲","cor_fundo":"#e8f5e9","tickers":{"SUZB3":{"nome":"Suzano","cor":"#1b5e20"},"KLBN11":{"nome":"Klabin","cor":"#33691e"},"DXCO3":{"nome":"Dexco","cor":"#5d4037"},"RANI3":{"nome":"Irani","cor":"#388e3c"}}},
}

FONTES = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

def log(msg, tipo="info"):
    entry = {"ts": agora().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 500: _log_entries.pop(0)
    print(f"[{entry['ts']}] {msg}", flush=True)

def buscar_lote(tickers):
    """Busca até 10 tickers de uma vez — plano Startup brapi.dev."""
    try:
        symbols = ",".join(tickers)
        r = requests.get(
            f"{QUOTE_URL}/{symbols}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=20
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            return {d["symbol"]: d for d in results}
        elif r.status_code == 429:
            log(f"⏳ Rate limit, aguardando 30s...", "aviso")
            time.sleep(30)
            return {}
        else:
            log(f"⚠️ HTTP {r.status_code}", "aviso")
    except Exception as e:
        log(f"⚠️ Erro lote: {e}", "aviso")
    return {}

def atualizar_cache():
    global _cache, _atualizando
    _atualizando = True
    try:
        log(f"🔄 Buscando cotações v{VERSION}...", "info")
        novo = {"atualizado_em": agora().isoformat(), "setores": {}, "version": VERSION}

        for sid, s in SETORES.items():
            log(f"🔍 {s['nome']}", "setor")
            tickers = list(s["tickers"].keys())
            dados = {}

            # Busca em lotes de 10
            for i in range(0, len(tickers), 10):
                lote = tickers[i:i+10]
                resultado = buscar_lote(lote)
                dados.update(resultado)
                if i + 10 < len(tickers):
                    time.sleep(1)  # 1s entre lotes — suficiente no plano Startup

            empresas = []
            for ticker, meta in s["tickers"].items():
                d = dados.get(ticker)
                if d:
                    preco = d.get("regularMarketPrice")
                    pct = d.get("regularMarketChangePercent") or 0
                    var = d.get("regularMarketChange") or 0
                    sinal = "▲" if pct >= 0 else "▼"
                    log(f"   {sinal} {ticker}: R$ {preco} ({pct:+.2f}%)", "cotacao")
                    empresas.append({
                        "ticker": ticker, "nome": meta["nome"], "cor": meta["cor"],
                        "preco": preco, "variacao": var, "variacao_pct": pct,
                        "maxima_dia": d.get("regularMarketDayHigh"),
                        "minima_dia": d.get("regularMarketDayLow"),
                        "volume": d.get("regularMarketVolume"),
                        "logo": d.get("logourl", ""),
                    })
                else:
                    log(f"   ❌ {ticker}: sem dados", "aviso")
                    empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})

            novo["setores"][sid] = {
                "nome": s["nome"], "icone": s["icone"], "cor_fundo": s["cor_fundo"],
                "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
            }

        _cache = novo
        total = sum(len(s["empresas"]) for s in novo["setores"].values())
        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        sem_preco = [(sid, e["ticker"]) for sid, s in novo["setores"].items() for e in s["empresas"] if not e.get("preco")]

        # Retry automático dos que falharam
        if sem_preco:
            log(f"🔁 Retentando {len(sem_preco)} ticker(s) sem dados...", "info")
            time.sleep(3)
            retry_tickers = [t for _, t in sem_preco]
            retry_dados = {}
            for i in range(0, len(retry_tickers), 10):
                lote = retry_tickers[i:i+10]
                resultado = buscar_lote(lote)
                retry_dados.update(resultado)
                time.sleep(1)
            # Atualiza os que vieram no retry
            for sid, ticker in sem_preco:
                if ticker in retry_dados:
                    d = retry_dados[ticker]
                    for e in novo["setores"][sid]["empresas"]:
                        if e["ticker"] == ticker:
                            preco = d.get("regularMarketPrice")
                            pct = d.get("regularMarketChangePercent") or 0
                            e.update({"preco": preco, "variacao": d.get("regularMarketChange") or 0,
                                "variacao_pct": pct, "maxima_dia": d.get("regularMarketDayHigh"),
                                "minima_dia": d.get("regularMarketDayLow"), "logo": d.get("logourl","")})
                            log(f"   ✅ {ticker}: R$ {preco} (retry OK)", "cotacao")
            _cache = novo

        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ {com_preco}/{total} ativos em {len(novo['setores'])} setores", "sucesso")
    except Exception as e:
        log(f"❌ Erro: {e}", "erro")
    finally:
        _atualizando = False

def loop_auto():
    time.sleep(INTERVALO)
    while True:
        atualizar_cache()
        time.sleep(INTERVALO)

log(f"🚀 App B3 v{VERSION} iniciado", "info")
threading.Thread(target=loop_auto, daemon=True).start()
# Não inicia busca automática — usuário clica em Atualizar

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/version")
def api_version(): return jsonify({"version": VERSION})

@app.route("/api/cotacoes")
def api_cotacoes(): return jsonify(_cache)

@app.route("/api/status")
def api_status():
    total = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
    return jsonify({"pronto":total>0,"atualizando":_atualizando,"total_ativos":total,"version":VERSION})

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    try:
        r = requests.get(
            f"{QUOTE_URL}/{ticker}?range=1y&interval=1d",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=20
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results and results[0].get("historicalDataPrice"):
                hist = results[0]["historicalDataPrice"]
                return jsonify({"ticker": ticker, "historico": [
                    {"date": h.get("date"), "close": h.get("close")}
                    for h in hist if h.get("close")
                ]})
    except Exception as e:
        log(f"⚠️ Histórico {ticker}: {e}", "aviso")
    return jsonify({"ticker": ticker, "historico": []})

@app.route("/api/detalhe/<ticker>")
def api_detalhe(ticker):
    try:
        r = requests.get(
            f"{QUOTE_URL}/{ticker}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=15
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                d = results[0]
                return jsonify({
                    "ticker": ticker,
                    "preco": d.get("regularMarketPrice"),
                    "variacao": d.get("regularMarketChange"),
                    "variacao_pct": d.get("regularMarketChangePercent"),
                    "minima_dia": d.get("regularMarketDayLow"),
                    "maxima_dia": d.get("regularMarketDayHigh"),
                })
    except Exception as e:
        log(f"⚠️ Detalhe {ticker}: {e}", "aviso")
    return jsonify({"erro": "não encontrado"}), 404

@app.route("/api/noticias/<ticker>")
def api_noticias(ticker):
    nome = next((e["nome"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker.upper()), ticker)
    noticias = buscar_noticias_rss(ticker.upper(), nome, FONTES)
    rec = gerar_recomendacao(ticker.upper(), nome, noticias)
    return jsonify({"ticker":ticker.upper(),"nome":nome,"noticias":noticias,"recomendacao":rec})

@app.route("/api/fontes", methods=["GET","POST"])
def api_fontes():
    global FONTES
    if request.method == "POST":
        FONTES = request.json.get("fontes", FONTES)
        return jsonify({"ok": True})
    return jsonify(FONTES)

def gerar_recomendacao(ticker, nome, noticias):
    todas = [f"[{f}] {n['titulo']}" for f,items in noticias.items() for n in items]
    if not todas: return {"sinal":"NEUTRO","justificativa":"Sem notícias recentes.","confianca":"Baixa"}
    if not ANTHROPIC_KEY: return {"sinal":"NEUTRO","justificativa":"Configure ANTHROPIC_API_KEY no Render.","confianca":"Baixa"}
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-6","max_tokens":300,"messages":[{"role":"user","content":
                f"Analise notícias sobre {ticker} ({nome}) e responda APENAS com JSON:\n{chr(10).join(todas[:6])}\nFormato: {{\"sinal\":\"COMPRAR\",\"justificativa\":\"2-3 frases.\",\"confianca\":\"Alta\"}}"}]},
            timeout=30)
        if resp.status_code == 200:
            return json.loads(resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip())
    except: pass
    return {"sinal":"NEUTRO","justificativa":"Erro ao gerar análise.","confianca":"Baixa"}

@app.route("/api/logs")
def api_logs():
    return jsonify(_log_entries[request.args.get("desde",0,type=int):])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
