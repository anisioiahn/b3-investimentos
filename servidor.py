import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker

VERSION = "1.9.0"

# Fuso horário de Brasília (UTC-3)
TZ_BRASILIA = timezone(timedelta(hours=-3))

def agora():
    return datetime.now(TZ_BRASILIA)

app = Flask(__name__, static_folder="static")
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUOTE_URL = "https://brapi.dev/api/quote"
OUTPUT_FILE = "cotacoes.json"

_log_entries = []
_atualizando = False
_intervalo_segundos = 3600  # padrão: 1 hora
_proximo_update = None

# ── Carrega cache do disco ao iniciar ────────────────────────
def _carregar_cache():
    try:
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            total = sum(len(s.get("empresas",[])) for s in dados.get("setores",{}).values())
            if total > 0:
                print(f"[INIT] Cache carregado do disco: {total} ativos", flush=True)
                return dados
    except Exception as e:
        print(f"[INIT] Erro ao carregar cache: {e}", flush=True)
    return {"atualizado_em": None, "setores": {}, "version": VERSION}

_cache = _carregar_cache()

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

        # Salva no disco para persistir entre restarts
        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(novo, f, ensure_ascii=False)
            log(f"💾 Cache salvo em disco", "info")
        except Exception as e:
            log(f"⚠️ Erro ao salvar cache: {e}", "aviso")

        # Verifica alertas com os novos preços
        verificar_alertas(novo)

        # Agenda próxima atualização
        _proximo_update = agora().timestamp() + _intervalo_segundos

    except Exception as e:
        log(f"❌ Erro: {e}", "erro")
    finally:
        _atualizando = False

def loop_auto():
    global _proximo_update
    while True:
        time.sleep(10)  # verifica a cada 10s se é hora de atualizar
        if _proximo_update and agora().timestamp() >= _proximo_update and not _atualizando:
            log(f"⏱️ Atualização automática programada", "info")
            atualizar_cache()

log(f"🚀 App B3 v{VERSION} iniciado", "info")
# Cache do disco já carregado — agenda próxima atualização para daqui 1 hora
_proximo_update = agora().timestamp() + _intervalo_segundos
threading.Thread(target=loop_auto, daemon=True).start()

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/version")
def api_version(): return jsonify({"version": VERSION})

@app.route("/api/cotacoes")
def api_cotacoes(): return jsonify(_cache)

@app.route("/api/status")
def api_status():
    total = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
    restante = max(0, int((_proximo_update or 0) - agora().timestamp())) if _proximo_update else None
    return jsonify({
        "pronto": total > 0,
        "atualizando": _atualizando,
        "total_ativos": total,
        "version": VERSION,
        "intervalo_segundos": _intervalo_segundos,
        "segundos_para_proxima": restante,
    })

@app.route("/api/intervalo", methods=["GET", "POST"])
def api_intervalo():
    global _intervalo_segundos, _proximo_update
    if request.method == "POST":
        novo_intervalo = request.json.get("segundos", 3600)
        _intervalo_segundos = int(novo_intervalo)
        _proximo_update = agora().timestamp() + _intervalo_segundos
        log(f"⏱️ Intervalo de atualização configurado para {_intervalo_segundos//60} minutos", "info")
        return jsonify({"ok": True, "intervalo_segundos": _intervalo_segundos})
    return jsonify({"intervalo_segundos": _intervalo_segundos})

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

# ── SISTEMA DE ALERTAS ────────────────────────────────────────
ALERTAS_FILE = "alertas.json"
_alertas = []          # lista de alertas ativos
_disparados = []       # histórico de alertas disparados

def _carregar_alertas():
    global _alertas, _disparados
    try:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            _alertas = dados.get("alertas", [])
            _disparados = dados.get("disparados", [])
            log(f"🔔 {len(_alertas)} alerta(s) carregado(s) do disco", "info")
    except Exception as e:
        log(f"⚠️ Erro ao carregar alertas: {e}", "aviso")

def _salvar_alertas():
    try:
        with open(ALERTAS_FILE, "w", encoding="utf-8") as f:
            json.dump({"alertas": _alertas, "disparados": _disparados[-50:]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ Erro ao salvar alertas: {e}", "aviso")

def verificar_alertas(novo_cache):
    """Verifica alertas após cada atualização de cotações."""
    global _alertas, _disparados
    if not _alertas: return
    disparados_agora = []
    for alerta in _alertas:
        ticker = alerta["ticker"]
        valor_alvo = alerta["valor"]
        direcao = alerta["direcao"]  # "acima" ou "abaixo"
        # Busca preço atual no cache
        preco_atual = None
        for s in novo_cache.get("setores", {}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker:
                    preco_atual = e.get("preco")
                    break
            if preco_atual: break
        if preco_atual is None: continue
        disparou = (direcao == "acima" and preco_atual >= valor_alvo) or \
                   (direcao == "abaixo" and preco_atual <= valor_alvo)
        if disparou:
            seta = "▲" if direcao == "acima" else "▼"
            msg = f"🚨 ALERTA: {ticker} {seta} R$ {preco_atual:.2f} {'≥' if direcao=='acima' else '≤'} alvo R$ {valor_alvo:.2f}"
            log(msg, "alerta")
            entrada = {**alerta, "preco_no_disparo": preco_atual, "disparado_em": agora().isoformat()}
            _disparados.insert(0, entrada)
            disparados_agora.append(entrada)
    if disparados_agora:
        _salvar_alertas()
    return disparados_agora

@app.route("/api/alertas", methods=["GET"])
def api_alertas_get():
    return jsonify({"alertas": _alertas, "disparados": _disparados[:20]})

@app.route("/api/alertas", methods=["POST"])
def api_alertas_post():
    """Cria ou atualiza um alerta."""
    dados = request.json
    ticker = dados.get("ticker","").upper()
    valor = float(dados.get("valor", 0))
    direcao = dados.get("direcao", "acima")  # "acima" ou "abaixo"
    nome = next((e["nome"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker), ticker)
    cor = next((e["cor"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker), "#1a5f3f")
    if not ticker or valor <= 0:
        return jsonify({"erro": "ticker e valor são obrigatórios"}), 400
    # Remove alerta anterior do mesmo ticker+direção se existir
    global _alertas
    _alertas = [a for a in _alertas if not (a["ticker"]==ticker and a["direcao"]==direcao)]
    novo = {"ticker": ticker, "nome": nome, "cor": cor, "valor": valor, "direcao": direcao, "criado_em": agora().isoformat()}
    _alertas.append(novo)
    _salvar_alertas()
    log(f"🔔 Alerta criado: {ticker} {'≥' if direcao=='acima' else '≤'} R$ {valor:.2f}", "info")
    return jsonify({"ok": True, "alerta": novo})

@app.route("/api/alertas/<ticker>", methods=["DELETE"])
def api_alertas_delete(ticker):
    global _alertas
    direcao = request.args.get("direcao")
    antes = len(_alertas)
    if direcao:
        _alertas = [a for a in _alertas if not (a["ticker"]==ticker.upper() and a["direcao"]==direcao)]
    else:
        _alertas = [a for a in _alertas if a["ticker"] != ticker.upper()]
    _salvar_alertas()
    removidos = antes - len(_alertas)
    log(f"🗑️ {removidos} alerta(s) removido(s) para {ticker.upper()}", "info")
    return jsonify({"ok": True, "removidos": removidos})

@app.route("/api/alertas/disparados/limpar", methods=["POST"])
def api_alertas_limpar_disparados():
    global _disparados
    _disparados = []
    _salvar_alertas()
    return jsonify({"ok": True})

@app.route("/api/logs")
def api_logs():
    return jsonify(_log_entries[request.args.get("desde",0,type=int):])

_carregar_alertas()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
