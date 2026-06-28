import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker

VERSION = "2.2.0"

# Fuso horário de Brasília (UTC-3)
TZ_BRASILIA = timezone(timedelta(hours=-3))

def agora():
    return datetime.now(TZ_BRASILIA)

app = Flask(__name__, static_folder="static")
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUOTE_URL = "https://brapi.dev/api/quote"
OUTPUT_FILE = "cotacoes.json"

# Chaves VAPID para Web Push
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY",  "BGj1V_-3OXoV8pKBwAiMYeeB6x9puemJlK3KUT_qlXiBLiUwzJUU3AMx55lxCfn4MhDpmgw3SnOUnREVZLSir_Q")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgQ8Bz9ldEae2wkEujDtHyxmtbBSd4-4fArPDGXRx-nPGhRANCAARo9Vf_tzl6FfKSgcAIjGHngesfabnpiZStylE_6pV4gS4lMMyVFNwDMeeZcQn5-DIQ6ZoMN0pzlJ0RFWS0oq_0")
VAPID_EMAIL = os.getenv("VAPID_EMAIL", "mailto:b3app@investimentos.com")

_push_subscriptions = []   # lista de subscriptions do browser
PUSH_FILE = "push_subscriptions.json"

def _carregar_subscriptions():
    global _push_subscriptions
    try:
        if os.path.exists(PUSH_FILE):
            with open(PUSH_FILE, "r", encoding="utf-8") as f:
                _push_subscriptions = json.load(f)
            print(f"[PUSH] {len(_push_subscriptions)} subscription(s) carregada(s)", flush=True)
    except Exception as e:
        print(f"[PUSH] Erro ao carregar subscriptions: {e}", flush=True)

def _salvar_subscriptions():
    try:
        with open(PUSH_FILE, "w", encoding="utf-8") as f:
            json.dump(_push_subscriptions, f)
    except: pass

def enviar_push(titulo, corpo, url="/"):
    """Envia push para todos os dispositivos registrados."""
    if not _push_subscriptions:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log("⚠️ pywebpush não instalado", "aviso")
        return
    payload = json.dumps({"title": titulo, "body": corpo, "url": url, "tag": "b3-alerta"})
    mortos = []
    for sub in _push_subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_EMAIL}
            )
        except Exception as e:
            err = str(e)
            if "410" in err or "404" in err:
                mortos.append(sub)  # subscription expirada
            else:
                log(f"⚠️ Push falhou: {err[:80]}", "aviso")
    # Remove subscriptions expiradas
    if mortos:
        for m in mortos:
            if m in _push_subscriptions:
                _push_subscriptions.remove(m)
        _salvar_subscriptions()

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

@app.route("/sw.js")
def service_worker():
    """Service Worker precisa estar na raiz para ter escopo total."""
    response = send_from_directory("static", "sw.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response

@app.route("/manifest.json")
def manifest(): return send_from_directory("static", "manifest.json")

@app.route("/apple-touch-icon.png")
def apple_icon(): return send_from_directory("static", "apple-touch-icon.png")

@app.route("/icon-192.png")
def icon192(): return send_from_directory("static", "icon-192.png")

@app.route("/icon-72.png")
def icon72(): return send_from_directory("static", "icon-72.png")

@app.route("/icon-512.png")
def icon512(): return send_from_directory("static", "icon-512.png")

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
    for alerta in _alertas:
        ticker = alerta["ticker"]
        valor_alvo = alerta["valor"]
        direcao = alerta["direcao"]
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
            msg_log = f"🚨 ALERTA: {ticker} {seta} R$ {preco_atual:.2f} ({'≥' if direcao=='acima' else '≤'} alvo R$ {valor_alvo:.2f})"
            log(msg_log, "alerta")
            entrada = {**alerta, "preco_no_disparo": preco_atual, "disparado_em": agora().isoformat()}
            _disparados.insert(0, entrada)
            # 🔔 Envia push notification
            titulo = f"🚨 Alerta B3: {ticker}"
            corpo = f"{alerta['nome']}\n{seta} Preço: R$ {preco_atual:.2f} ({'≥' if direcao=='acima' else '≤'} alvo R$ {valor_alvo:.2f})"
            enviar_push(titulo, corpo)
    _salvar_alertas()

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

# ── CARTEIRA DO INVESTIDOR ────────────────────────────────────
CARTEIRA_FILE = "carteira.json"
_carteira = []  # lista de posições

def _carregar_carteira():
    global _carteira
    try:
        if os.path.exists(CARTEIRA_FILE):
            with open(CARTEIRA_FILE, "r", encoding="utf-8") as f:
                _carteira = json.load(f)
            log(f"💼 Carteira carregada: {len(_carteira)} posição(ões)", "info")
    except Exception as e:
        log(f"⚠️ Erro ao carregar carteira: {e}", "aviso")

def _salvar_carteira():
    try:
        with open(CARTEIRA_FILE, "w", encoding="utf-8") as f:
            json.dump(_carteira, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ Erro ao salvar carteira: {e}", "aviso")

def _enriquecer_carteira():
    """Adiciona cotação atual a cada posição da carteira."""
    resultado = []
    for pos in _carteira:
        ticker = pos["ticker"]
        preco_atual = None
        nome = pos.get("nome", ticker)
        cor = pos.get("cor", "#1a5f3f")
        setor_nome = pos.get("setor_nome", "")
        # Busca no cache
        for s in _cache.get("setores", {}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker:
                    preco_atual = e.get("preco")
                    nome = e.get("nome", nome)
                    cor = e.get("cor", cor)
                    setor_nome = s.get("nome", setor_nome)
                    break
            if preco_atual: break
        qtd = pos.get("quantidade", 0)
        preco_medio = pos.get("preco_medio", 0)
        valor_investido = qtd * preco_medio
        valor_atual = qtd * preco_atual if preco_atual else None
        lucro = (valor_atual - valor_investido) if valor_atual else None
        lucro_pct = ((preco_atual - preco_medio) / preco_medio * 100) if preco_atual and preco_medio else None
        resultado.append({
            **pos,
            "nome": nome,
            "cor": cor,
            "setor_nome": setor_nome,
            "preco_atual": preco_atual,
            "valor_investido": round(valor_investido, 2),
            "valor_atual": round(valor_atual, 2) if valor_atual else None,
            "lucro": round(lucro, 2) if lucro is not None else None,
            "lucro_pct": round(lucro_pct, 2) if lucro_pct is not None else None,
        })
    return resultado

@app.route("/api/carteira", methods=["GET"])
def api_carteira_get():
    return jsonify(_enriquecer_carteira())

@app.route("/api/carteira", methods=["POST"])
def api_carteira_post():
    """Adiciona ou atualiza uma posição na carteira."""
    dados = request.json
    ticker = dados.get("ticker", "").upper().strip()
    if not ticker:
        return jsonify({"erro": "ticker obrigatório"}), 400
    # Busca nome e cor no cache
    nome = ticker
    cor = "#1a5f3f"
    setor_id = ""
    setor_nome = ""
    for sid, s in _cache.get("setores", {}).items():
        for e in s["empresas"]:
            if e["ticker"] == ticker:
                nome = e.get("nome", ticker)
                cor = e.get("cor", cor)
                setor_id = sid
                setor_nome = s.get("nome", "")
                break
        if nome != ticker: break
    nova_pos = {
        "ticker": ticker,
        "nome": nome,
        "cor": cor,
        "setor_id": setor_id,
        "setor_nome": setor_nome,
        "preco_medio": float(dados.get("preco_medio", 0)),
        "quantidade": float(dados.get("quantidade", 0)),
        "data_compra": dados.get("data_compra", ""),
        "corretora": dados.get("corretora", ""),
        "adicionado_em": agora().isoformat(),
    }
    # Atualiza se já existe, senão adiciona
    global _carteira
    idx = next((i for i, p in enumerate(_carteira) if p["ticker"] == ticker), None)
    if idx is not None:
        _carteira[idx] = nova_pos
        log(f"💼 Posição atualizada: {ticker}", "info")
    else:
        _carteira.append(nova_pos)
        log(f"💼 Posição adicionada: {ticker} ({nova_pos['quantidade']}x R$ {nova_pos['preco_medio']})", "info")
    _salvar_carteira()
    return jsonify({"ok": True, "posicao": nova_pos})

@app.route("/api/carteira/<ticker>", methods=["DELETE"])
def api_carteira_delete(ticker):
    global _carteira
    antes = len(_carteira)
    _carteira = [p for p in _carteira if p["ticker"] != ticker.upper()]
    _salvar_carteira()
    log(f"🗑️ Posição removida: {ticker.upper()}", "info")
    return jsonify({"ok": True, "removidos": antes - len(_carteira)})

@app.route("/api/carteira/resumo", methods=["GET"])
def api_carteira_resumo():
    """Resumo consolidado da carteira."""
    posicoes = _enriquecer_carteira()
    total_investido = sum(p["valor_investido"] for p in posicoes)
    total_atual = sum(p["valor_atual"] for p in posicoes if p["valor_atual"])
    lucro_total = total_atual - total_investido if total_atual else None
    lucro_pct = (lucro_total / total_investido * 100) if lucro_total and total_investido else None
    # Agrupa por setor para o gráfico de pizza
    por_setor = {}
    for p in posicoes:
        s = p.get("setor_nome") or "Outros"
        if s not in por_setor:
            por_setor[s] = {"nome": s, "valor_atual": 0, "valor_investido": 0}
        por_setor[s]["valor_atual"] += p["valor_atual"] or p["valor_investido"]
        por_setor[s]["valor_investido"] += p["valor_investido"]
    return jsonify({
        "total_posicoes": len(posicoes),
        "total_investido": round(total_investido, 2),
        "total_atual": round(total_atual, 2) if total_atual else None,
        "lucro_total": round(lucro_total, 2) if lucro_total is not None else None,
        "lucro_pct": round(lucro_pct, 2) if lucro_pct is not None else None,
        "por_setor": list(por_setor.values()),
    })

@app.route("/api/push/vapid-public-key")
def api_vapid_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})

@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    sub = request.json
    if sub and sub not in _push_subscriptions:
        _push_subscriptions.append(sub)
        _salvar_subscriptions()
        log(f"📱 Novo dispositivo registrado para push ({len(_push_subscriptions)} total)", "info")
    return jsonify({"ok": True, "total": len(_push_subscriptions)})

@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    sub = request.json
    if sub in _push_subscriptions:
        _push_subscriptions.remove(sub)
        _salvar_subscriptions()
    return jsonify({"ok": True})

@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    enviar_push("🧪 Teste B3", "Notificação push funcionando! Você receberá alertas assim quando uma ação atingir seu valor alvo.")
    return jsonify({"ok": True, "dispositivos": len(_push_subscriptions)})

@app.route("/api/push/status")
def api_push_status():
    return jsonify({"dispositivos": len(_push_subscriptions), "ativo": len(_push_subscriptions) > 0})

@app.route("/api/logs")
def api_logs():
    return jsonify(_log_entries[request.args.get("desde",0,type=int):])

_carregar_alertas()
_carregar_subscriptions()
_carregar_carteira()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
