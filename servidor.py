import os, json, threading, time, requests, xml.etree.ElementTree as ET
from janus_routes import registrar_rotas_janus
from janus_cron import iniciar_cron_janus
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request, redirect
from functools import wraps
from buscar_cotacoes import buscar_noticias_rss, SETOR_MAP, cor_para_ticker
import db, auth

VERSION = "3.1.0"
TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora(): return datetime.now(TZ_BRASILIA)

app = Flask(__name__, static_folder="static")
TOKEN_BRAPI = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUOTE_URL = "https://brapi.dev/api/quote"
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY",  "BGj1V_-3OXoV8pKBwAiMYeeB6x9puemJlK3KUT_qlXiBLiUwzJUU3AMx55lxCfn4MhDpmgw3SnOUnREVZLSir_Q")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgQ8Bz9ldEae2wkEujDtHyxmtbBSd4-4fArPDGXRx-nPGhRANCAARo9Vf_tzl6FfKSgcAIjGHngesfabnpiZStylE_6pV4gS4lMMyVFNwDMeeZcQn5-DIQ6ZoMN0pzlJ0RFWS0oq_0")
VAPID_EMAIL = os.getenv("VAPID_EMAIL", "mailto:b3app@investimentos.com")

_log_entries = []
_atualizando = False
_progresso = {"atual": 0, "total": 0, "setor_atual": ""}
_intervalo_segundos = 3600
_proximo_update = None
_cache = {"atualizado_em": None, "setores": {}, "version": VERSION}

# ── Mapeamento dinâmico de setores (Brapi EN → PT-BR) ────────
# Substitui o dicionário SETORES hardcoded anterior
SETOR_META = {
    "Finance":              {"id":"financeiro",       "nome":"Financeiro",                    "icone":"🏦","cor_fundo":"#e3f2fd"},
    "Utilities":            {"id":"utilidade",        "nome":"Utilidade Pública",              "icone":"⚡","cor_fundo":"#fff8e1"},
    "Energy Minerals":      {"id":"petroleo",         "nome":"Petróleo, Gás e Biocombustíveis","icone":"🛢️","cor_fundo":"#e8f5e9"},
    "Non-Energy Minerals":  {"id":"minerais",         "nome":"Minerais e Mineração",           "icone":"🪨","cor_fundo":"#efebe9"},
    "Process Industries":   {"id":"processo",         "nome":"Indústria de Processo",          "icone":"🏭","cor_fundo":"#f3e5f5"},
    "Producer Manufacturing":{"id":"industriais",     "nome":"Bens Industriais",               "icone":"🏗️","cor_fundo":"#fffde7"},
    "Consumer Non-Durables":{"id":"consumo_nciclico", "nome":"Consumo Não Cíclico",            "icone":"🌾","cor_fundo":"#f1f8e9"},
    "Consumer Durables":    {"id":"consumo_duravel",  "nome":"Consumo Durável",                "icone":"🛋️","cor_fundo":"#fce4ec"},
    "Consumer Services":    {"id":"servicos",         "nome":"Serviços ao Consumidor",         "icone":"🍽️","cor_fundo":"#fff3e0"},
    "Retail Trade":         {"id":"varejo",           "nome":"Varejo",                         "icone":"🛍️","cor_fundo":"#f3e5f5"},
    "Distribution Services":{"id":"distribuicao",     "nome":"Distribuição e Logística",       "icone":"📦","cor_fundo":"#e8eaf6"},
    "Transportation":       {"id":"transporte",       "nome":"Transporte",                     "icone":"🚢","cor_fundo":"#e0f7fa"},
    "Health Services":      {"id":"saude",            "nome":"Saúde",                          "icone":"🏥","cor_fundo":"#ffebee"},
    "Health Technology":    {"id":"saude_tec",        "nome":"Tecnologia em Saúde",            "icone":"🔬","cor_fundo":"#fce4ec"},
    "Commercial Services":  {"id":"comercial",        "nome":"Serviços Comerciais",            "icone":"💼","cor_fundo":"#e8f5e9"},
    "Industrial Services":  {"id":"ind_servicos",     "nome":"Serviços Industriais",           "icone":"🔧","cor_fundo":"#fffde7"},
    "Communications":       {"id":"comunicacoes",     "nome":"Comunicações",                   "icone":"📡","cor_fundo":"#e0f2f1"},
    "Technology Services":  {"id":"tec_servicos",     "nome":"Serviços de Tecnologia",         "icone":"💻","cor_fundo":"#ede7f6"},
    "Electronic Technology":{"id":"eletronicos",      "nome":"Tecnologia Eletrônica",          "icone":"📱","cor_fundo":"#e8eaf6"},
    "Miscellaneous":        {"id":"outros",           "nome":"Outros",                         "icone":"📋","cor_fundo":"#f5f5f5"},
}

# Paleta de cores para tickers (ciclica, sem precisar mapear manualmente)
_CORES = [
    "#005a2b","#1a5276","#003399","#7b1fa2","#bf360c","#e65100","#006064",
    "#0d47a1","#37474f","#880e4f","#1b5e20","#f57f17","#4a148c","#263238",
    "#0277bd","#c62828","#33691e","#1565c0","#e53935","#00695c","#ff6600",
    "#cc0000","#003a80","#1a1a2e","#f9a825","#2e7d32","#6200ea","#0000cc",
]
_cor_idx = 0
def proxima_cor():
    global _cor_idx
    cor = _CORES[_cor_idx % len(_CORES)]
    _cor_idx += 1
    return cor

def log(msg, tipo="info"):
    entry = {"ts": agora().strftime("%H:%M:%S"), "msg": msg, "tipo": tipo}
    _log_entries.append(entry)
    if len(_log_entries) > 500: _log_entries.pop(0)
    print(f"[{entry['ts']}] {msg}", flush=True)

# ── DECORATORS AUTH ───────────────────────────────────────────
def requer_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization','').replace('Bearer ','')
        if not token:
            token = request.cookies.get('janus_token','')
        payload = auth.verificar_jwt(token)
        if not payload:
            return jsonify({"erro": "Não autenticado", "redirect": "/login"}), 401
        request.usuario = payload
        return f(*args, **kwargs)
    return decorated

def requer_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization','').replace('Bearer ','')
        if not token:
            token = request.cookies.get('janus_admin_token','')
        payload = auth.verificar_jwt_admin(token)
        if not payload:
            return jsonify({"erro": "Acesso negado"}), 403
        request.admin = payload
        return f(*args, **kwargs)
    return decorated

def uid(): return request.usuario['uid']

# ── FONTES (do banco) ──────────────────────────────────────────
def get_fontes():
    cfg = db.db_get_all_config()
    return [
        {"nome": cfg.get('fonte_1_nome','Infomoney'), "url": cfg.get('fonte_1_url',''), "cor":"#e53935"},
        {"nome": cfg.get('fonte_2_nome','Valor Econômico'), "url": cfg.get('fonte_2_url',''), "cor":"#1565c0"},
        {"nome": cfg.get('fonte_3_nome','MoneyTimes'), "url": cfg.get('fonte_3_url',''), "cor":"#2e7d32"},
    ]

# ── COTAÇÕES ──────────────────────────────────────────────────
def buscar_lote(tickers):
    try:
        r = requests.get(f"{QUOTE_URL}/{','.join(tickers)}",
                         headers={"Authorization": f"Bearer {TOKEN_BRAPI}"}, timeout=20)
        if r.status_code == 200:
            return {d["symbol"]: d for d in r.json().get("results", [])}
        elif r.status_code == 429:
            log("⏳ Rate limit, aguardando 30s...", "aviso"); time.sleep(30)
    except Exception as e:
        log(f"⚠️ Erro lote: {e}", "aviso")
    return {}

def buscar_lista_completa_b3():
    """Busca todos os ativos da B3 via Brapi (até 500), retorna lista de stocks."""
    try:
        r = requests.get(
            f"https://brapi.dev/api/quote/list?token={TOKEN_BRAPI}&type=stock&limit=500&sortBy=volume&sortOrder=desc",
            timeout=30
        )
        if r.status_code == 200:
            stocks = r.json().get("stocks", [])
            log(f"📋 {len(stocks)} ativos listados na B3", "info")
            return stocks
        log(f"⚠️ Erro ao listar ativos: {r.status_code}", "aviso")
    except Exception as e:
        log(f"⚠️ Erro ao buscar lista B3: {e}", "aviso")
    return []

def atualizar_cache():
    global _cache, _atualizando, _proximo_update, _cor_idx, _progresso
    _atualizando = True
    _progresso = {"atual": 0, "total": 0, "setor_atual": "Iniciando..."}
    try:
        log(f"🔄 Buscando cotações v{VERSION}...", "info")
        novo = {"atualizado_em": agora().isoformat(), "setores": {}, "version": VERSION}

        # STEP 1: Busca lista completa da B3
        _progresso["setor_atual"] = "Buscando lista de ativos..."
        stocks = buscar_lista_completa_b3()
        if not stocks:
            log("❌ Lista de ativos vazia, abortando", "erro")
            return

        # STEP 2: Organiza tickers por setor
        tickers_por_setor = {}
        ticker_meta = {}  # ticker → {nome, setor_en, cor}
        _cor_idx = 0
        for s in stocks:
            ticker  = s.get("stock") or s.get("symbol")
            nome    = s.get("name") or ticker
            setor_en = s.get("sector") or "Miscellaneous"
            if not ticker: continue
            if setor_en not in tickers_por_setor:
                tickers_por_setor[setor_en] = []
            tickers_por_setor[setor_en].append(ticker)
            ticker_meta[ticker] = {"nome": nome, "setor_en": setor_en, "cor": proxima_cor()}

        total_tickers = len(ticker_meta)
        _progresso["total"] = total_tickers
        log(f"📊 {total_tickers} ativos em {len(tickers_por_setor)} setores", "info")

        # STEP 3: Busca cotações em lotes de 10
        cotacoes = {}
        todos_tickers = list(ticker_meta.keys())
        for i in range(0, len(todos_tickers), 10):
            lote = todos_tickers[i:i+10]
            cotacoes.update(buscar_lote(lote))
            _progresso["atual"] = min(i + 10, total_tickers)
            _progresso["setor_atual"] = f"Buscando cotações... ({_progresso['atual']}/{total_tickers})"
            time.sleep(0.3)

        # STEP 4: Monta cache por setor em português
        ativos_processados = 0
        for setor_en, tickers in tickers_por_setor.items():
            meta = SETOR_META.get(setor_en, SETOR_META["Miscellaneous"])
            sid  = meta["id"]
            _progresso["setor_atual"] = meta["nome"]
            empresas = []

            for ticker in tickers:
                tm = ticker_meta[ticker]
                d  = cotacoes.get(ticker)
                if d:
                    preco = d.get("regularMarketPrice")
                    pct   = d.get("regularMarketChangePercent") or 0
                    if preco:
                        log(f"   {'▲' if pct>=0 else '▼'} {ticker}: R$ {preco} ({pct:+.2f}%)", "cotacao")
                    empresas.append({
                        "ticker": ticker,
                        "nome": tm["nome"],
                        "cor": tm["cor"],
                        "preco": preco,
                        "variacao": d.get("regularMarketChange") or 0,
                        "variacao_pct": pct,
                        "maxima_dia": d.get("regularMarketDayHigh"),
                        "minima_dia": d.get("regularMarketDayLow"),
                        "volume": d.get("regularMarketVolume"),
                        "logo": d.get("logourl", f"https://icons.brapi.dev/icons/{ticker}.svg"),
                    })
                else:
                    empresas.append({
                        "ticker": ticker,
                        "nome": tm["nome"],
                        "cor": tm["cor"],
                        "preco": None,
                        "logo": f"https://icons.brapi.dev/icons/{ticker}.svg",
                    })
                ativos_processados += 1
                _progresso["atual"] = ativos_processados

            novo["setores"][sid] = {
                "nome":      meta["nome"],
                "icone":     meta["icone"],
                "cor_fundo": meta["cor_fundo"],
                "empresas":  sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
            }
            com_preco = sum(1 for e in empresas if e.get("preco"))
            log(f"🔍 {meta['nome']}: {com_preco}/{len(empresas)} com cotação", "setor")

        _cache = novo
        db.db_salvar_cache(novo)
        verificar_alertas_todos(novo)

        total_com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ {total_com_preco}/{total_tickers} ativos atualizados em {len(novo['setores'])} setores", "sucesso")
        _proximo_update = agora().timestamp() + _intervalo_segundos

    except Exception as e:
        log(f"❌ Erro: {e}", "erro")
    finally:
        _atualizando = False

def verificar_alertas_todos(cache):
    """Verifica alertas de TODOS os usuários."""
    alertas = db.db_listar_todos_alertas()
    disparados_por_usuario = {}
    for alerta in alertas:
        uid_u = alerta['uid']
        ticker = alerta['ticker']
        valor_alvo = float(alerta['valor'])
        direcao = alerta['direcao']
        preco_atual = None
        for s in cache.get("setores",{}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker: preco_atual = e.get("preco"); break
            if preco_atual: break
        if preco_atual is None: continue
        disparou = (direcao=="acima" and preco_atual>=valor_alvo) or \
                   (direcao=="abaixo" and preco_atual<=valor_alvo)
        if disparou:
            seta = "▲" if direcao=="acima" else "▼"
            log(f"🚨 {ticker} {seta} R${preco_atual:.2f} (user {uid_u})", "alerta")
            db.db_registrar_disparado(uid_u, alerta, preco_atual)
            if uid_u not in disparados_por_usuario:
                disparados_por_usuario[uid_u] = []
            disparados_por_usuario[uid_u].append((alerta, preco_atual))
    # Envia push por usuário
    for uid_u, itens in disparados_por_usuario.items():
        subs = db.db_listar_push(uid_u)
        for alerta, preco in itens:
            seta = "▲" if alerta['direcao']=="acima" else "▼"
            enviar_push_para(subs, f"🚨 Janus: {alerta['ticker']}",
                f"{alerta.get('nome',alerta['ticker'])}\n{seta} R$ {preco:.2f}")

def enviar_push_para(subs, titulo, corpo):
    if not subs: return
    try:
        from pywebpush import webpush
        payload = json.dumps({"title":titulo,"body":corpo,"tag":"janus-alerta"})
        for sub in subs:
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub":VAPID_EMAIL})
            except: pass
    except: pass

def loop_auto():
    time.sleep(10)
    while True:
        if _proximo_update and agora().timestamp() >= _proximo_update and not _atualizando:
            atualizar_cache()
        time.sleep(10)

def enriquecer_carteira(posicoes):
    resultado = []
    for pos in posicoes:
        ticker = pos["ticker"]
        preco_atual = None
        for s in _cache.get("setores",{}).values():
            for e in s["empresas"]:
                if e["ticker"] == ticker:
                    preco_atual = e.get("preco")
                    if not pos.get("nome") or pos["nome"]==ticker: pos["nome"]=e.get("nome",ticker)
                    if not pos.get("cor"): pos["cor"]=e.get("cor","#0066cc")
                    if not pos.get("setor_nome"): pos["setor_nome"]=s.get("nome","")
                    break
            if preco_atual: break
        qtd = float(pos.get("quantidade",0))
        pm = float(pos.get("preco_medio",0))
        vi = round(qtd*pm,2)
        va = round(qtd*preco_atual,2) if preco_atual else None
        lucro = round(va-vi,2) if va else None
        lucro_pct = round((preco_atual-pm)/pm*100,2) if preco_atual and pm else None
        resultado.append({**pos,"preco_atual":preco_atual,"valor_investido":vi,
                          "valor_atual":va,"lucro":lucro,"lucro_pct":lucro_pct})
    return resultado

# ── ROTAS ESTÁTICAS ──────────────────────────────────────────
@app.route("/")
def index():
    token = request.cookies.get('janus_token','')
    if not token or not auth.verificar_jwt(token):
        return redirect('/login')
    return send_from_directory("static", "index.html")

@app.route("/login")
def login_page(): return send_from_directory("static", "login.html")

@app.route("/admin")
def admin_page():
    token = request.cookies.get('janus_admin_token','')
    if not token or not auth.verificar_jwt_admin(token):
        return redirect('/admin/login')
    return send_from_directory("static", "admin.html")

@app.route("/admin/login")
def admin_login_page(): return send_from_directory("static", "admin-login.html")

@app.route("/reset-senha")
def reset_senha_page(): return send_from_directory("static", "reset-senha.html")

@app.route("/sw.js")
def sw():
    r = send_from_directory("static","sw.js")
    r.headers["Cache-Control"]="no-cache"; r.headers["Service-Worker-Allowed"]="/"
    return r

@app.route("/manifest.json")
def manifest(): return send_from_directory("static","manifest.json")
@app.route("/apple-touch-icon.png")
def apple_icon(): return send_from_directory("static","apple-touch-icon.png")
@app.route("/icon-192.png")
def icon192(): return send_from_directory("static","icon-192.png")
@app.route("/icon-72.png")
def icon72(): return send_from_directory("static","icon-72.png")
@app.route("/icon-512.png")
def icon512(): return send_from_directory("static","icon-512.png")

# ── AUTH ROUTES ───────────────────────────────────────────────
@app.route("/api/auth/cadastro", methods=["POST"])
def api_cadastro():
    d = request.json or {}
    email = d.get("email","").strip().lower()
    nome = d.get("nome","").strip()
    senha = d.get("senha","")
    lang = d.get("lang","pt")
    if not email or "@" not in email: return jsonify({"erro":"E-mail inválido"}),400
    if len(senha) < 8: return jsonify({"erro":"Senha deve ter pelo menos 8 caracteres"}),400
    if not nome: return jsonify({"erro":"Nome obrigatório"}),400
    if db.db_buscar_usuario_email(email): return jsonify({"erro":"E-mail já cadastrado"}),409
    codigo = auth.gerar_codigo()
    expira = auth.expira_em(30)
    uid_novo = db.db_criar_usuario(email, nome, auth.hash_senha(senha), codigo, expira)
    if not uid_novo: return jsonify({"erro":"Erro ao criar conta"}),500
    auth.enviar_verificacao(email, nome, codigo, lang)
    return jsonify({"ok":True,"mensagem":"Conta criada! Verifique seu e-mail.","uid":uid_novo})

@app.route("/api/auth/verificar-email", methods=["POST"])
def api_verificar_email():
    d = request.json or {}
    email = d.get("email","").lower()
    codigo = d.get("codigo","").strip()
    if db.db_verificar_email(email, codigo):
        usuario = db.db_buscar_usuario_email(email)
        token = auth.gerar_jwt(usuario['id'], email, usuario.get('plano','free'))
        resp = jsonify({"ok":True,"token":token,"nome":usuario['nome'],"plano":usuario.get('plano','free')})
        resp.set_cookie('janus_token', token, max_age=86400*3, httponly=True, samesite='Lax')
        return resp
    return jsonify({"erro":"Código inválido ou expirado"}),400

@app.route("/api/auth/reenviar-codigo", methods=["POST"])
def api_reenviar_codigo():
    d = request.json or {}
    email = d.get("email","").lower()
    lang = d.get("lang","pt")
    usuario = db.db_buscar_usuario_email(email)
    if not usuario: return jsonify({"erro":"E-mail não encontrado"}),404
    if usuario.get('email_verificado'): return jsonify({"erro":"E-mail já verificado"}),400
    codigo = auth.gerar_codigo()
    expira = auth.expira_em(30)
    db.db_reenviar_codigo(email, codigo, expira)
    auth.enviar_verificacao(email, usuario['nome'], codigo, lang)
    return jsonify({"ok":True})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.json or {}
    email = d.get("email","").lower()
    senha = d.get("senha","")
    usuario = db.db_buscar_usuario_email(email)
    if not usuario or not auth.verificar_senha(senha, usuario['senha_hash']):
        return jsonify({"erro":"E-mail ou senha incorretos"}),401
    if not usuario.get('email_verificado'):
        return jsonify({"erro":"E-mail não verificado","nao_verificado":True}),403
    if not usuario.get('ativo'):
        return jsonify({"erro":"Conta desativada"}),403
    db.db_atualizar_ultimo_acesso(usuario['id'])
    token = auth.gerar_jwt(usuario['id'], email, usuario.get('plano','free'))
    resp = jsonify({"ok":True,"token":token,"nome":usuario['nome'],"plano":usuario.get('plano','free')})
    resp.set_cookie('janus_token', token, max_age=86400*3, httponly=True, samesite='Lax')
    return resp

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    resp = jsonify({"ok":True})
    resp.delete_cookie('janus_token')
    return resp

@app.route("/api/auth/esqueci-senha", methods=["POST"])
def api_esqueci_senha():
    d = request.json or {}
    email = d.get("email","").lower()
    lang = d.get("lang","pt")
    usuario = db.db_buscar_usuario_email(email)
    if not usuario: return jsonify({"ok":True})  # Não revela se existe
    token = auth.gerar_token()
    expira = auth.expira_em(60)
    db.db_salvar_reset_token(email, token, expira)
    auth.enviar_reset(email, usuario['nome'], token, lang)
    return jsonify({"ok":True})

@app.route("/api/auth/reset-senha", methods=["POST"])
def api_reset_senha():
    d = request.json or {}
    token = d.get("token","")
    nova_senha = d.get("senha","")
    if len(nova_senha) < 8: return jsonify({"erro":"Senha deve ter pelo menos 8 caracteres"}),400
    if db.db_reset_senha(token, auth.hash_senha(nova_senha)):
        return jsonify({"ok":True})
    return jsonify({"erro":"Link inválido ou expirado"}),400

@app.route("/api/auth/me")
@requer_auth
def api_me():
    usuario = db.db_buscar_usuario_id(uid())
    if not usuario: return jsonify({"erro":"Usuário não encontrado"}),404
    return jsonify({"id":usuario['id'],"email":usuario['email'],"nome":usuario['nome'],
                    "plano":usuario.get('plano','free'),"criado_em":usuario.get('criado_em')})

# ── API PROTEGIDA ─────────────────────────────────────────────
@app.route("/api/version")
def api_version(): return jsonify({"version":VERSION})

@app.route("/api/cotacoes")
@requer_auth
def api_cotacoes(): return jsonify(_cache)

@app.route("/api/status")
@requer_auth
def api_status():
    total = sum(len(s.get("empresas",[])) for s in _cache.get("setores",{}).values())
    restante = max(0,int((_proximo_update or 0)-agora().timestamp())) if _proximo_update else None
    return jsonify({"pronto":total>0,"atualizando":_atualizando,"total_ativos":total,
                    "version":VERSION,"intervalo_segundos":_intervalo_segundos,"segundos_para_proxima":restante,
                    "progresso": _progresso if _atualizando else None})

@app.route("/api/progresso")
@requer_auth
def api_progresso():
    """Retorna o progresso atual da atualização de cotações."""
    pct = 0
    if _progresso["total"] > 0:
        pct = round(_progresso["atual"] / _progresso["total"] * 100)
    return jsonify({
        "atualizando": _atualizando,
        "atual": _progresso["atual"],
        "total": _progresso["total"],
        "pct": pct,
        "setor_atual": _progresso["setor_atual"]
    })

@app.route("/api/atualizar", methods=["POST"])
@requer_auth
def api_atualizar():
    threading.Thread(target=atualizar_cache,daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/intervalo", methods=["GET","POST"])
@requer_auth
def api_intervalo():
    global _intervalo_segundos, _proximo_update
    if request.method=="POST":
        _intervalo_segundos = int(request.json.get("segundos",3600))
        _proximo_update = agora().timestamp() + _intervalo_segundos
        return jsonify({"ok":True,"intervalo_segundos":_intervalo_segundos})
    return jsonify({"intervalo_segundos":_intervalo_segundos})

_cache_historico = {}  # ticker → {data, ts}
CACHE_HISTORICO_TTL = 300  # 5 minutos

@app.route("/api/historico/<ticker>")
@requer_auth
def api_historico(ticker):
    ticker = ticker.upper()
    agora_ts = time.time()
    # Retorna do cache se ainda válido
    if ticker in _cache_historico:
        cached = _cache_historico[ticker]
        if agora_ts - cached["ts"] < CACHE_HISTORICO_TTL:
            return jsonify(cached["data"])
    try:
        # Uma única chamada retorna preço atual + histórico
        r = requests.get(
            f"{QUOTE_URL}/{ticker}?range=1y&interval=1d&token={TOKEN_BRAPI}",
            timeout=15)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                d = results[0]
                hist = d.get("historicalDataPrice", [])
                resp = {
                    "ticker": ticker,
                    "preco": d.get("regularMarketPrice"),
                    "variacao": d.get("regularMarketChange"),
                    "variacao_pct": d.get("regularMarketChangePercent"),
                    "minima_dia": d.get("regularMarketDayLow"),
                    "maxima_dia": d.get("regularMarketDayHigh"),
                    "historico": [{"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]
                }
                _cache_historico[ticker] = {"data": resp, "ts": agora_ts}
                return jsonify(resp)
    except: pass
    return jsonify({"ticker": ticker, "historico": []})

@app.route("/api/detalhe/<ticker>")
@requer_auth
def api_detalhe(ticker):
    ticker = ticker.upper()
    # Aproveita cache do histórico se disponível e recente
    if ticker in _cache_historico:
        cached = _cache_historico[ticker]
        if time.time() - cached["ts"] < CACHE_HISTORICO_TTL:
            d = cached["data"]
            return jsonify({"ticker": ticker, "preco": d.get("preco"),
                "variacao": d.get("variacao"), "variacao_pct": d.get("variacao_pct"),
                "minima_dia": d.get("minima_dia"), "maxima_dia": d.get("maxima_dia")})
    try:
        r = requests.get(f"{QUOTE_URL}/{ticker}?token={TOKEN_BRAPI}", timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                d = results[0]
                return jsonify({"ticker": ticker,
                    "preco": d.get("regularMarketPrice"),
                    "variacao": d.get("regularMarketChange"),
                    "variacao_pct": d.get("regularMarketChangePercent"),
                    "minima_dia": d.get("regularMarketDayLow"),
                    "maxima_dia": d.get("regularMarketDayHigh")})
    except: pass
    return jsonify({"erro": "não encontrado"}), 404

@app.route("/api/noticias/<ticker>")
@requer_auth
def api_noticias(ticker):
    nome = next((e["nome"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker.upper()),ticker)
    noticias = buscar_noticias_rss(ticker.upper(), nome, get_fontes())
    rec = gerar_recomendacao(ticker.upper(), nome, noticias)
    return jsonify({"ticker":ticker.upper(),"nome":nome,"noticias":noticias,"recomendacao":rec})

# ── ALERTAS por usuário ───────────────────────────────────────
@app.route("/api/alertas", methods=["GET"])
@requer_auth
def api_alertas_get():
    return jsonify({"alertas":db.db_listar_alertas(uid()),"disparados":db.db_listar_disparados(uid())})

@app.route("/api/alertas", methods=["POST"])
@requer_auth
def api_alertas_post():
    d = request.json
    ticker = d.get("ticker","").upper().strip()
    valor = float(d.get("valor",0))
    direcao = d.get("direcao","acima")
    if not ticker or valor<=0: return jsonify({"erro":"dados inválidos"}),400
    # Verifica limite do plano
    plano_nome = request.usuario.get('plano','free')
    planos = {p['nome']:p for p in db.db_listar_planos()}
    plano = planos.get(plano_nome,{})
    max_alt = plano.get('max_alertas',-1)
    if max_alt > 0 and len(db.db_listar_alertas(uid())) >= max_alt:
        return jsonify({"erro":f"Limite de {max_alt} alertas atingido. Faça upgrade para o plano Pró.","limite":True}),403
    nome = next((e["nome"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker),ticker)
    cor = next((e["cor"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker),"#0066cc")
    db.db_salvar_alerta(uid(), ticker, nome, cor, valor, direcao)
    return jsonify({"ok":True})

@app.route("/api/alertas/<ticker>", methods=["DELETE"])
@requer_auth
def api_alertas_delete(ticker):
    db.db_remover_alerta(uid(), ticker.upper(), request.args.get("direcao"))
    return jsonify({"ok":True})

@app.route("/api/alertas/disparados/limpar", methods=["POST"])
@requer_auth
def api_alertas_limpar():
    db.db_limpar_disparados(uid())
    return jsonify({"ok":True})

# ── CARTEIRA por usuário ──────────────────────────────────────
@app.route("/api/carteira", methods=["GET"])
@requer_auth
def api_carteira_get():
    return jsonify(enriquecer_carteira(db.db_listar_carteira(uid())))

@app.route("/api/carteira", methods=["POST"])
@requer_auth
def api_carteira_post():
    d = request.json
    ticker = d.get("ticker","").upper().strip()
    if not ticker: return jsonify({"erro":"ticker obrigatório"}),400
    # Verifica limite do plano
    plano_nome = request.usuario.get('plano','free')
    planos = {p['nome']:p for p in db.db_listar_planos()}
    plano = planos.get(plano_nome,{})
    max_cart = plano.get('max_carteira',-1)
    carteira_atual = db.db_listar_carteira(uid())
    tickers_atuais = [p['ticker'] for p in carteira_atual]
    if max_cart > 0 and ticker not in tickers_atuais and len(carteira_atual) >= max_cart:
        return jsonify({"erro":f"Limite de {max_cart} ativos na carteira. Faça upgrade para o plano Pró.","limite":True}),403
    nome = next((e["nome"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker),ticker)
    cor = next((e["cor"] for s in _cache.get("setores",{}).values() for e in s["empresas"] if e["ticker"]==ticker),"#0066cc")
    setor_id,setor_nome = "",""
    for sid,s in _cache.get("setores",{}).items():
        if any(e["ticker"]==ticker for e in s["empresas"]):
            setor_id,setor_nome = sid,s["nome"]; break
    db.db_salvar_posicao(uid(), ticker, nome, cor, setor_id, setor_nome,
        float(d.get("preco_medio",0)), float(d.get("quantidade",0)),
        d.get("data_compra",""), d.get("corretora",""))
    return jsonify({"ok":True})

@app.route("/api/carteira/<ticker>", methods=["DELETE"])
@requer_auth
def api_carteira_delete(ticker):
    db.db_remover_posicao(uid(), ticker.upper())
    return jsonify({"ok":True})

@app.route("/api/carteira/resumo")
@requer_auth
def api_carteira_resumo():
    posicoes = enriquecer_carteira(db.db_listar_carteira(uid()))
    ti = sum(p["valor_investido"] for p in posicoes)
    ta = sum(p["valor_atual"] for p in posicoes if p["valor_atual"])
    lucro = round(ta-ti,2) if ta else None
    pct = round(lucro/ti*100,2) if lucro and ti else None
    return jsonify({"total_posicoes":len(posicoes),"total_investido":round(ti,2),
                    "total_atual":round(ta,2) if ta else None,"lucro_total":lucro,"lucro_pct":pct})

# ── CARTEIRA PENDENTE (sugestões do Janus Index) ───────────────
@app.route("/api/carteira/sugestao", methods=["POST"])
@requer_auth
def api_carteira_sugestao():
    """Recebe lista de tickers sugeridos pelo Janus Index e cria como pendentes."""
    d = request.json or {}
    itens = d.get("itens", [])  # [{ticker, quantidade}]
    if not itens:
        return jsonify({"erro": "Nenhum item informado"}), 400

    # Verifica limite do plano (mesma regra da carteira normal)
    plano_nome = request.usuario.get('plano', 'free')
    planos = {p['nome']: p for p in db.db_listar_planos()}
    plano = planos.get(plano_nome, {})
    max_cart = plano.get('max_carteira', -1)
    carteira_atual = db.db_listar_carteira(uid())
    tickers_atuais = [p['ticker'] for p in carteira_atual]

    adicionados, ja_existentes, erros = [], [], []

    for item in itens:
        ticker = item.get("ticker", "").upper().strip()
        quantidade = float(item.get("quantidade", 100))
        if not ticker:
            continue
        if ticker in tickers_atuais:
            ja_existentes.append(ticker)
            continue
        if max_cart > 0 and (len(tickers_atuais) + len(adicionados)) >= max_cart:
            erros.append(ticker)
            continue

        nome = next((e["nome"] for s in _cache.get("setores", {}).values() for e in s["empresas"] if e["ticker"] == ticker), None)
        cor = next((e["cor"] for s in _cache.get("setores", {}).values() for e in s["empresas"] if e["ticker"] == ticker), "#0066cc")
        preco_atual = next((e["preco"] for s in _cache.get("setores", {}).values() for e in s["empresas"] if e["ticker"] == ticker), None)
        setor_id, setor_nome = "", ""
        for sid, s in _cache.get("setores", {}).items():
            if any(e["ticker"] == ticker for e in s["empresas"]):
                setor_id, setor_nome = sid, s["nome"]
                break

        # Fallback: ticker não está no cache interno (não faz parte dos setores monitorados)
        # Busca direto na Brapi para garantir preço e nome corretos
        if not nome or not preco_atual:
            try:
                rb = requests.get(f"{QUOTE_URL}/{ticker}?token={TOKEN_BRAPI}", timeout=15)
                if rb.status_code == 200:
                    res = rb.json().get("results", [])
                    if res:
                        d_brapi = res[0]
                        if not nome:
                            nome = d_brapi.get("longName") or d_brapi.get("shortName") or ticker
                        if not preco_atual:
                            preco_atual = d_brapi.get("regularMarketPrice") or 0
            except Exception:
                pass

        if not nome:
            nome = ticker
        if not preco_atual:
            preco_atual = 0

        ok = db.db_salvar_posicao_pendente(
            uid(), ticker, nome, cor, setor_id, setor_nome,
            preco_atual or 0, quantidade, agora().strftime("%Y-%m-%d"), "", origem="janus_sugestao"
        )
        if ok:
            adicionados.append(ticker)

    return jsonify({
        "ok": True,
        "adicionados": adicionados,
        "ja_existentes": ja_existentes,
        "erros": erros
    })

@app.route("/api/carteira/pendente/<ticker>/confirmar", methods=["POST"])
@requer_auth
def api_carteira_pendente_confirmar(ticker):
    d = request.json or {}
    preco = float(d.get("preco_medio", 0))
    qtd = float(d.get("quantidade", 0))
    if preco <= 0 or qtd <= 0:
        return jsonify({"erro": "Preço e quantidade devem ser maiores que zero"}), 400

    ok = db.db_confirmar_posicao_pendente(
        uid(), ticker.upper(), preco, qtd,
        d.get("data_compra", ""), d.get("corretora", "")
    )
    if not ok:
        return jsonify({"erro": "Posição pendente não encontrada"}), 404
    return jsonify({"ok": True})

@app.route("/api/carteira/pendente/<ticker>", methods=["DELETE"])
@requer_auth
def api_carteira_pendente_descartar(ticker):
    db.db_descartar_posicao_pendente(uid(), ticker.upper())
    return jsonify({"ok": True})

# ── PUSH por usuário ──────────────────────────────────────────
@app.route("/api/push/vapid-public-key")
def api_vapid(): return jsonify({"publicKey":VAPID_PUBLIC_KEY})

@app.route("/api/push/subscribe", methods=["POST"])
@requer_auth
def api_push_sub():
    sub = request.json
    db.db_salvar_push(uid(), json.dumps(sub))
    return jsonify({"ok":True})

@app.route("/api/push/unsubscribe", methods=["POST"])
@requer_auth
def api_push_unsub():
    sub = request.json
    db.db_remover_push(uid(), json.dumps(sub))
    return jsonify({"ok":True})

@app.route("/api/push/test", methods=["POST"])
@requer_auth
def api_push_test():
    subs = db.db_listar_push(uid())
    enviar_push_para(subs,"🧪 Janus","Notificações funcionando!")
    return jsonify({"ok":True,"dispositivos":len(subs)})

@app.route("/api/push/status")
@requer_auth
def api_push_status():
    return jsonify({"dispositivos":len(db.db_listar_push(uid()))})

# ── LOGS ──────────────────────────────────────────────────────
@app.route("/api/fontes", methods=["GET"])
@requer_auth
def api_fontes_get():
    return jsonify(get_fontes())

@app.route("/api/fontes", methods=["POST"])
@requer_admin
def api_fontes_post():
    fontes = request.json.get("fontes", [])
    for i, f in enumerate(fontes[:3], 1):
        db.db_set_config(f"fonte_{i}_nome", f.get("nome",""))
        db.db_set_config(f"fonte_{i}_url", f.get("url",""))
    return jsonify({"ok": True})

@app.route("/api/logs")
@requer_auth
def api_logs():
    desde = request.args.get("desde",0,type=int)
    return jsonify(_log_entries[desde:])

# ── AJIA — Analista Janus com Inteligência Artificial ─────────
@app.route("/api/ajia/chat", methods=["POST"])
@requer_auth
def api_ajia_chat():
    """Proxy seguro para a API da Anthropic — mantém a chave no servidor."""
    if not ANTHROPIC_KEY:
        return jsonify({"erro": "ANTHROPIC_API_KEY não configurada"}), 503
    d = request.json or {}
    system_prompt = d.get("system", "")
    messages = d.get("messages", [])
    max_tokens = d.get("max_tokens", 600)
    if not messages:
        return jsonify({"erro": "messages obrigatório"}), 400
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": messages
            },
            timeout=45
        )
        if resp.status_code == 200:
            data = resp.json()
            texto = data.get("content", [{}])[0].get("text", "")
            return jsonify({"texto": texto})
        return jsonify({"erro": f"Erro na API: {resp.status_code}"}), resp.status_code
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# ── ADMIN ROUTES ──────────────────────────────────────────────
@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    d = request.json or {}
    email = d.get("email","").lower()
    senha = d.get("senha","")
    admin = db.db_buscar_admin(email)
    if not admin or not auth.verificar_senha(senha, admin['senha_hash']):
        return jsonify({"erro":"Credenciais inválidas"}),401
    token = auth.gerar_jwt_admin(email)
    resp = jsonify({"ok":True,"token":token})
    resp.set_cookie('janus_admin_token', token, max_age=28800, httponly=True, samesite='Lax')
    return resp

@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    resp = jsonify({"ok":True})
    resp.delete_cookie('janus_admin_token')
    return resp

@app.route("/api/admin/usuarios")
@requer_admin
def api_admin_usuarios():
    return jsonify(db.db_listar_usuarios())

@app.route("/api/admin/usuarios/<int:user_id>/plano", methods=["PUT"])
@requer_admin
def api_admin_atualizar_plano(user_id):
    plano = request.json.get("plano","free")
    db.db_atualizar_plano_usuario(user_id, plano)
    return jsonify({"ok":True})

@app.route("/api/admin/planos", methods=["GET"])
@requer_admin
def api_admin_planos_get():
    return jsonify(db.db_listar_planos())

@app.route("/api/admin/planos", methods=["POST"])
@requer_admin
def api_admin_planos_post():
    d = request.json
    db.db_salvar_plano(d['nome'],d.get('preco_mensal',0),d.get('preco_anual',0),
        d.get('desconto_anual_pct',0),d.get('max_alertas',-1),d.get('max_carteira',-1),d.get('descricao',''))
    return jsonify({"ok":True})

@app.route("/api/admin/config", methods=["GET"])
@requer_admin
def api_admin_config_get():
    return jsonify(db.db_get_all_config())

@app.route("/api/admin/config", methods=["POST"])
@requer_admin
def api_admin_config_post():
    for chave, valor in (request.json or {}).items():
        db.db_set_config(chave, valor)
    return jsonify({"ok":True})

@app.route("/api/admin/stats")
@requer_admin
def api_admin_stats():
    usuarios = db.db_listar_usuarios()
    return jsonify({
        "total_usuarios": len(usuarios),
        "verificados": sum(1 for u in usuarios if u.get('email_verificado')),
        "plano_free": sum(1 for u in usuarios if u.get('plano')=='free'),
        "plano_pro": sum(1 for u in usuarios if u.get('plano')=='pro'),
        "ativos": sum(1 for u in usuarios if u.get('ativo')),
    })

def gerar_recomendacao(ticker, nome, noticias):
    todas = [f"[{f}] {n['titulo']}" for f,items in noticias.items() for n in items]
    if not todas: return {"sinal":"NEUTRO","justificativa":"Sem notícias recentes.","confianca":"Baixa"}
    if not ANTHROPIC_KEY: return {"sinal":"NEUTRO","justificativa":"Configure ANTHROPIC_API_KEY.","confianca":"Baixa"}
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-6","max_tokens":300,"messages":[{"role":"user","content":
                f"Analise o sentimento das notícias sobre {ticker} ({nome}) e responda APENAS com JSON.\n{chr(10).join(todas[:6])}\nFormato: {{\"sinal\":\"POSITIVO\",\"justificativa\":\"2-3 frases.\",\"confianca\":\"Alta\"}}\nO campo sinal deve ser exatamente POSITIVO, NEGATIVO ou NEUTRO — representa o sentimento das notícias, não uma recomendação de investimento."}]},
            timeout=30)
        if resp.status_code==200:
            return json.loads(resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip())
    except: pass
    return {"sinal":"NEUTRO","justificativa":"Erro ao gerar análise.","confianca":"Baixa"}

# ── INIT ─────────────────────────────────────────────────────
INTERVALO_INICIAL = 5
log(f"🚀 Janus v{VERSION} iniciado", "info")
_db_ok = db.init_db()
registrar_rotas_janus(app, requer_auth)
iniciar_cron_janus()
if _db_ok:
    cache_db = db.db_carregar_cache()
    if cache_db:
        _cache = cache_db
        log(f"📂 Cache restaurado do banco", "sucesso")
    auth.init_admin_padrao()
_proximo_update = agora().timestamp() + INTERVALO_INICIAL
threading.Thread(target=loop_auto, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
