import os, json, threading, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker
import db

VERSION = "2.3.5"
TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BRASILIA)

app = Flask(__name__, static_folder="static")
TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUOTE_URL = "https://brapi.dev/api/quote"

VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY",  "BGj1V_-3OXoV8pKBwAiMYeeB6x9puemJlK3KUT_qlXiBLiUwzJUU3AMx55lxCfn4MhDpmgw3SnOUnREVZLSir_Q")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgQ8Bz9ldEae2wkEujDtHyxmtbBSd4-4fArPDGXRx-nPGhRANCAARo9Vf_tzl6FfKSgcAIjGHngesfabnpiZStylE_6pV4gS4lMMyVFNwDMeeZcQn5-DIQ6ZoMN0pzlJ0RFWS0oq_0")
VAPID_EMAIL = os.getenv("VAPID_EMAIL", "mailto:b3app@investimentos.com")

_push_subscriptions = []
PUSH_FILE = "push_subscriptions.json"

_log_entries = []
_atualizando = False
_intervalo_segundos = 3600
_proximo_update = None

# Cache em memória — carregado do banco ao iniciar
_cache = {"atualizado_em": None, "setores": {}, "version": VERSION}

FONTES = [
    {"nome": "Infomoney", "url": "https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/", "cor": "#e53935"},
    {"nome": "Valor Econômico", "url": "https://valor.globo.com/financas/rss20.xml", "cor": "#1565c0"},
    {"nome": "MoneyTimes", "url": "https://www.moneytimes.com.br/mercados/feed/", "cor": "#2e7d32"},
]

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
    "imobiliario":      {"nome":"Imobiliário","icone":"🏢","cor_fundo":"#fce4ec","tickers":{"MULT3":{"nome":"Multiplan","cor":"#880e4f"},"IGTI11":{"nome":"Iguatemi","cor":"#4a148c"},"BRPR3":{"nome":"BR Properties","cor":"#37474f"}}},
    "papel_celulose":   {"nome":"Papel e Celulose","icone":"🌲","cor_fundo":"#e8f5e9","tickers":{"SUZB3":{"nome":"Suzano","cor":"#1b5e20"},"KLBN11":{"nome":"Klabin","cor":"#33691e"},"DXCO3":{"nome":"Dexco","cor":"#5d4037"},"RANI3":{"nome":"Irani","cor":"#388e3c"}}},
}

def log(msg, tipo="info"):
    entry = {"ts": agora().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 500: _log_entries.pop(0)
    print(f"[{entry['ts']}] {msg}", flush=True)

def _carregar_subscriptions():
    global _push_subscriptions
    try:
        if os.path.exists(PUSH_FILE):
            with open(PUSH_FILE, "r") as f:
                _push_subscriptions = json.load(f)
    except: pass

def _salvar_subscriptions():
    try:
        with open(PUSH_FILE, "w") as f:
            json.dump(_push_subscriptions, f)
    except: pass

def enviar_push(titulo, corpo, url="/"):
    if not _push_subscriptions: return
    try:
        from pywebpush import webpush
        payload = json.dumps({"title": titulo, "body": corpo, "url": url, "tag": "janus-alerta"})
        mortos = []
        for sub in _push_subscriptions:
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": VAPID_EMAIL})
            except Exception as e:
                if "410" in str(e) or "404" in str(e): mortos.append(sub)
        for m in mortos:
            if m in _push_subscriptions: _push_subscriptions.remove(m)
        if mortos: _salvar_subscriptions()
    except Exception as e:
        log(f"⚠️ Push erro: {e}", "aviso")

def buscar_lote(tickers):
    try:
        symbols = ",".join(tickers)
        r = requests.get(f"{QUOTE_URL}/{symbols}",
                         headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
        if r.status_code == 200:
            return {d["symbol"]: d for d in r.json().get("results", [])}
        elif r.status_code == 429:
            log("⏳ Rate limit, aguardando 30s...", "aviso")
            time.sleep(30)
    except Exception as e:
        log(f"⚠️ Erro lote: {e}", "aviso")
    return {}

def atualizar_cache():
    global _cache, _atualizando, _proximo_update
    _atualizando = True
    try:
        log(f"🔄 Buscando cotações v{VERSION}...", "info")
        novo = {"atualizado_em": agora().isoformat(), "setores": {}, "version": VERSION}
        for sid, s in SETORES.items():
            log(f"🔍 {s['nome']}", "setor")
            tickers = list(s["tickers"].keys())
            dados = {}
            for i in range(0, len(tickers), 10):
                resultado = buscar_lote(tickers[i:i+10])
                dados.update(resultado)
                if i + 10 < len(tickers): time.sleep(1)
            empresas = []
            for ticker, meta in s["tickers"].items():
                d = dados.get(ticker)
                if d:
                    preco = d.get("regularMarketPrice")
                    pct = d.get("regularMarketChangePercent") or 0
                    log(f"   {'▲' if pct>=0 else '▼'} {ticker}: R$ {preco} ({pct:+.2f}%)", "cotacao")
                    empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"],
                        "preco": preco, "variacao": d.get("regularMarketChange") or 0,
                        "variacao_pct": pct, "maxima_dia": d.get("regularMarketDayHigh"),
                        "minima_dia": d.get("regularMarketDayLow"),
                        "volume": d.get("regularMarketVolume"), "logo": d.get("logourl","")})
                else:
                    log(f"   ❌ {ticker}: sem dados", "aviso")
                    empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})
                time.sleep(0.1)
            # Retry dos que falharam
            sem_dados = [e["ticker"] for e in empresas if not e.get("preco")]
            if sem_dados:
                log(f"🔁 Retentando {len(sem_dados)} ticker(s)...", "info")
                time.sleep(3)
                retry = {}
                for i in range(0, len(sem_dados), 10):
                    retry.update(buscar_lote(sem_dados[i:i+10]))
                    time.sleep(1)
                for e in empresas:
                    if not e.get("preco") and e["ticker"] in retry:
                        d = retry[e["ticker"]]
                        preco = d.get("regularMarketPrice")
                        pct = d.get("regularMarketChangePercent") or 0
                        e.update({"preco": preco, "variacao": d.get("regularMarketChange") or 0,
                            "variacao_pct": pct, "maxima_dia": d.get("regularMarketDayHigh"),
                            "minima_dia": d.get("regularMarketDayLow"),
                            "logo": d.get("logourl","")})
                        log(f"   ✅ {e['ticker']}: R$ {preco} (retry)", "cotacao")
            novo["setores"][sid] = {"nome": s["nome"], "icone": s["icone"],
                "cor_fundo": s["cor_fundo"],
                "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True)}
        _cache = novo
        # Salva no banco de dados
        if db.db_salvar_cache(novo):
            log("💾 Cache salvo no banco de dados", "info")
        # Verifica alertas
        verificar_alertas(novo)
        total = sum(len(s["empresas"]) for s in novo["setores"].values())
        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ {com_preco}/{total} ativos em {len(novo['setores'])} setores", "sucesso")
        _proximo_update = agora().timestamp() + _intervalo_segundos
    except Exception as e:
        log(f"❌ Erro: {e}", "erro")
    finally:
        _atualizando = False

def verificar_alertas(novo_cache):
    alertas = db.db_listar_alertas()
    if not alertas: return
    for alerta in alertas:
        ticker = alerta["ticker"]
        valor_alvo = float(alerta["valor"])
        direcao = alerta["direcao"]
        preco_atual = None
        for s in novo_cache.get("setores", {}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker:
                    preco_atual = e.get("preco"); break
            if preco_atual: break
        if preco_atual is None: continue
        disparou = (direcao == "acima" and preco_atual >= valor_alvo) or \
                   (direcao == "abaixo" and preco_atual <= valor_alvo)
        if disparou:
            seta = "▲" if direcao == "acima" else "▼"
            log(f"🚨 ALERTA: {ticker} {seta} R$ {preco_atual:.2f} ({'≥' if direcao=='acima' else '≤'} R$ {valor_alvo:.2f})", "alerta")
            db.db_registrar_disparado(alerta, preco_atual)
            enviar_push(f"🚨 Janus: {ticker}",
                f"{alerta.get('nome',ticker)}\n{seta} R$ {preco_atual:.2f} ({'≥' if direcao=='acima' else '≤'} alvo R$ {valor_alvo:.2f})")

def loop_auto():
    time.sleep(INTERVALO_INICIAL)
    while True:
        if _proximo_update and agora().timestamp() >= _proximo_update and not _atualizando:
            log(f"⏱️ Atualização automática programada", "info")
            atualizar_cache()
        time.sleep(10)

# ── ROTAS ESTÁTICAS ──────────────────────────────────────────
@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/sw.js")
def service_worker():
    r = send_from_directory("static", "sw.js")
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Service-Worker-Allowed"] = "/"
    return r

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
    return jsonify({"pronto": total>0, "atualizando": _atualizando, "total_ativos": total,
                    "version": VERSION, "intervalo_segundos": _intervalo_segundos,
                    "segundos_para_proxima": restante})

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    threading.Thread(target=atualizar_cache, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/intervalo", methods=["GET","POST"])
def api_intervalo():
    global _intervalo_segundos, _proximo_update
    if request.method == "POST":
        _intervalo_segundos = int(request.json.get("segundos", 3600))
        _proximo_update = agora().timestamp() + _intervalo_segundos
        log(f"⏱️ Intervalo: {_intervalo_segundos//60} min", "info")
        return jsonify({"ok": True, "intervalo_segundos": _intervalo_segundos})
    return jsonify({"intervalo_segundos": _intervalo_segundos})

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    try:
        r = requests.get(f"{QUOTE_URL}/{ticker}?range=1y&interval=1d",
                         headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results and results[0].get("historicalDataPrice"):
                hist = results[0]["historicalDataPrice"]
                return jsonify({"ticker": ticker, "historico": [
                    {"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]})
    except: pass
    return jsonify({"ticker": ticker, "historico": []})

@app.route("/api/detalhe/<ticker>")
def api_detalhe(ticker):
    try:
        r = requests.get(f"{QUOTE_URL}/{ticker}",
                         headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                d = results[0]
                return jsonify({"ticker": ticker, "preco": d.get("regularMarketPrice"),
                    "variacao": d.get("regularMarketChange"),
                    "variacao_pct": d.get("regularMarketChangePercent"),
                    "minima_dia": d.get("regularMarketDayLow"),
                    "maxima_dia": d.get("regularMarketDayHigh")})
    except: pass
    return jsonify({"erro": "não encontrado"}), 404

@app.route("/api/noticias/<ticker>")
def api_noticias(ticker):
    nome = next((e["nome"] for s in _cache.get("setores",{}).values()
                 for e in s["empresas"] if e["ticker"]==ticker.upper()), ticker)
    noticias = buscar_noticias_rss(ticker.upper(), nome, FONTES)
    rec = gerar_recomendacao(ticker.upper(), nome, noticias)
    return jsonify({"ticker": ticker.upper(), "nome": nome, "noticias": noticias, "recomendacao": rec})

@app.route("/api/fontes", methods=["GET","POST"])
def api_fontes():
    global FONTES
    if request.method == "POST":
        FONTES = request.json.get("fontes", FONTES)
        return jsonify({"ok": True})
    return jsonify(FONTES)

# ── ALERTAS (banco de dados) ──────────────────────────────────
@app.route("/api/alertas", methods=["GET"])
def api_alertas_get():
    return jsonify({"alertas": db.db_listar_alertas(), "disparados": db.db_listar_disparados()})

@app.route("/api/alertas", methods=["POST"])
def api_alertas_post():
    dados = request.json
    ticker = dados.get("ticker","").upper().strip()
    valor = float(dados.get("valor", 0))
    direcao = dados.get("direcao", "acima")
    if not ticker or valor <= 0: return jsonify({"erro": "dados inválidos"}), 400
    nome = next((e["nome"] for s in _cache.get("setores",{}).values()
                 for e in s["empresas"] if e["ticker"]==ticker), ticker)
    cor = next((e["cor"] for s in _cache.get("setores",{}).values()
                for e in s["empresas"] if e["ticker"]==ticker), "#0066cc")
    db.db_salvar_alerta(ticker, nome, cor, valor, direcao)
    log(f"🔔 Alerta: {ticker} {'≥' if direcao=='acima' else '≤'} R$ {valor:.2f}", "info")
    return jsonify({"ok": True})

@app.route("/api/alertas/<ticker>", methods=["DELETE"])
def api_alertas_delete(ticker):
    direcao = request.args.get("direcao")
    db.db_remover_alerta(ticker.upper(), direcao)
    return jsonify({"ok": True})

@app.route("/api/alertas/disparados/limpar", methods=["POST"])
def api_alertas_limpar():
    db.db_limpar_disparados()
    return jsonify({"ok": True})

# ── CARTEIRA (banco de dados) ─────────────────────────────────
def _enriquecer(posicoes):
    resultado = []
    for pos in posicoes:
        ticker = pos["ticker"]
        preco_atual = None
        for s in _cache.get("setores",{}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker:
                    preco_atual = e.get("preco")
                    if not pos.get("nome") or pos["nome"] == ticker:
                        pos["nome"] = e.get("nome", ticker)
                    if not pos.get("cor"):
                        pos["cor"] = e.get("cor","#0066cc")
                    if not pos.get("setor_nome"):
                        pos["setor_nome"] = s.get("nome","")
                    break
            if preco_atual: break
        qtd = float(pos.get("quantidade",0))
        pm = float(pos.get("preco_medio",0))
        vi = round(qtd * pm, 2)
        va = round(qtd * preco_atual, 2) if preco_atual else None
        lucro = round(va - vi, 2) if va else None
        lucro_pct = round((preco_atual - pm)/pm*100, 2) if preco_atual and pm else None
        resultado.append({**pos, "preco_atual": preco_atual, "valor_investido": vi,
                          "valor_atual": va, "lucro": lucro, "lucro_pct": lucro_pct})
    return resultado

@app.route("/api/carteira", methods=["GET"])
def api_carteira_get():
    return jsonify(_enriquecer(db.db_listar_carteira()))

@app.route("/api/carteira", methods=["POST"])
def api_carteira_post():
    d = request.json
    ticker = d.get("ticker","").upper().strip()
    if not ticker: return jsonify({"erro": "ticker obrigatório"}), 400
    nome = next((e["nome"] for s in _cache.get("setores",{}).values()
                 for e in s["empresas"] if e["ticker"]==ticker), ticker)
    cor = next((e["cor"] for s in _cache.get("setores",{}).values()
                for e in s["empresas"] if e["ticker"]==ticker), "#0066cc")
    setor_id, setor_nome = "", ""
    for sid, s in _cache.get("setores",{}).items():
        if any(e["ticker"]==ticker for e in s["empresas"]):
            setor_id, setor_nome = sid, s["nome"]; break
    db.db_salvar_posicao(ticker, nome, cor, setor_id, setor_nome,
        float(d.get("preco_medio",0)), float(d.get("quantidade",0)),
        d.get("data_compra",""), d.get("corretora",""))
    log(f"💼 Posição salva: {ticker}", "info")
    return jsonify({"ok": True})

@app.route("/api/carteira/<ticker>", methods=["DELETE"])
def api_carteira_delete(ticker):
    db.db_remover_posicao(ticker.upper())
    return jsonify({"ok": True})

@app.route("/api/carteira/resumo")
def api_carteira_resumo():
    posicoes = _enriquecer(db.db_listar_carteira())
    ti = sum(p["valor_investido"] for p in posicoes)
    ta = sum(p["valor_atual"] for p in posicoes if p["valor_atual"])
    lucro = round(ta - ti, 2) if ta else None
    pct = round(lucro/ti*100, 2) if lucro and ti else None
    por_setor = {}
    for p in posicoes:
        s = p.get("setor_nome") or "Outros"
        if s not in por_setor: por_setor[s] = {"nome":s,"valor_atual":0,"valor_investido":0}
        por_setor[s]["valor_atual"] += p["valor_atual"] or p["valor_investido"]
        por_setor[s]["valor_investido"] += p["valor_investido"]
    return jsonify({"total_posicoes": len(posicoes), "total_investido": round(ti,2),
                    "total_atual": round(ta,2) if ta else None,
                    "lucro_total": lucro, "lucro_pct": pct,
                    "por_setor": list(por_setor.values())})

# ── PUSH ──────────────────────────────────────────────────────
@app.route("/api/push/vapid-public-key")
def api_vapid(): return jsonify({"publicKey": VAPID_PUBLIC_KEY})

@app.route("/api/push/subscribe", methods=["POST"])
def api_push_sub():
    sub = request.json
    if sub and sub not in _push_subscriptions:
        _push_subscriptions.append(sub); _salvar_subscriptions()
        log(f"📱 Push registrado ({len(_push_subscriptions)} dispositivos)", "info")
    return jsonify({"ok": True})

@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsub():
    sub = request.json
    if sub in _push_subscriptions:
        _push_subscriptions.remove(sub); _salvar_subscriptions()
    return jsonify({"ok": True})

@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    enviar_push("🧪 Janus", "Notificações funcionando! Você receberá alertas quando suas ações atingirem os valores configurados.")
    return jsonify({"ok": True, "dispositivos": len(_push_subscriptions)})

@app.route("/api/push/status")
def api_push_status(): return jsonify({"dispositivos": len(_push_subscriptions)})

# ── LOG (fix para desktop) ───────────────────────────────────
@app.route("/api/logs")
def api_logs():
    desde = request.args.get("desde", 0, type=int)
    entries = _log_entries[desde:]
    return jsonify(entries)

def gerar_recomendacao(ticker, nome, noticias):
    todas = [f"[{f}] {n['titulo']}" for f,items in noticias.items() for n in items]
    if not todas: return {"sinal":"NEUTRO","justificativa":"Sem notícias recentes.","confianca":"Baixa"}
    if not ANTHROPIC_KEY: return {"sinal":"NEUTRO","justificativa":"Configure ANTHROPIC_API_KEY.","confianca":"Baixa"}
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

# ── INIT ─────────────────────────────────────────────────────
INTERVALO_INICIAL = 5  # segundos para iniciar primeira busca após subir

log(f"🚀 Janus v{VERSION} iniciado", "info")

# Inicializa banco de dados
_db_ok = db.init_db()

# Carrega cache do banco (dados da última atualização)
if _db_ok:
    cache_db = db.db_carregar_cache()
    if cache_db:
        _cache = cache_db
        total_cache = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
        log(f"📂 Cache restaurado do banco: {total_cache} ativos", "sucesso")
    else:
        log("ℹ️ Sem cache no banco — aguardando primeira atualização", "info")
else:
    log("⚠️ Banco de dados não disponível — usando apenas memória", "aviso")

_carregar_subscriptions()
_proximo_update = agora().timestamp() + INTERVALO_INICIAL

threading.Thread(target=loop_auto, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
