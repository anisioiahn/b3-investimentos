import os, json, threading, time, queue
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, Response, stream_with_context
from buscar_cotacoes import buscar_todas_cotacoes, buscar_historico, OUTPUT_FILE, SETORES

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))

_lock = threading.Lock()
_cache = {"atualizado_em": None, "setores": {}}
_log_entries = []
_sse_clients = []
_sse_lock = threading.Lock()

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
            try:
                q.put_nowait(data)
            except:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

def atualizar_cache():
    global _cache
    log("🔄 Iniciando busca de cotações...", "info")
    try:
        novo = buscar_todas_cotacoes_com_log()
        with _lock:
            _cache = novo
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(novo, f, ensure_ascii=False, indent=2)
        total = sum(len(s["empresas"]) for s in novo["setores"].values())
        com_preco = sum(1 for s in novo["setores"].values() for e in s["empresas"] if e.get("preco"))
        log(f"✅ Concluído! {com_preco}/{total} empresas atualizadas", "sucesso")
    except Exception as e:
        log(f"❌ Erro na atualização: {e}", "erro")

def buscar_todas_cotacoes_com_log():
    import requests
    TOKEN = os.getenv("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
    BASE_URL = "https://brapi.dev/api/quote"
    HEADERS = {"Authorization": f"Bearer {TOKEN}"}

    resultado = {"atualizado_em": datetime.now().isoformat(), "setores": {}}

    for sid, s in SETORES.items():
        log(f"🔍 Buscando: {s['nome']}", "setor")
        empresas = []
        for ticker, meta in s["tickers"].items():
            try:
                resp = requests.get(f"{BASE_URL}/{ticker}", headers=HEADERS, timeout=15)
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    d = results[0] if results else None
                    if d:
                        preco = d.get("regularMarketPrice")
                        pct = d.get("regularMarketChangePercent", 0)
                        sinal = "▲" if pct >= 0 else "▼"
                        log(f"   {sinal} {ticker}: R$ {preco} ({pct:+.2f}%)", "cotacao")
                        empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": preco, "variacao": d.get("regularMarketChange"), "variacao_pct": pct, "maxima_dia": d.get("regularMarketDayHigh"), "minima_dia": d.get("regularMarketDayLow"), "volume": d.get("regularMarketVolume"), "logo": d.get("logourl")})
                    else:
                        log(f"   ⚠️  {ticker}: sem dados", "aviso")
                        empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})
                else:
                    log(f"   ❌ {ticker}: HTTP {resp.status_code}", "erro")
                    empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})
            except Exception as e:
                log(f"   ❌ {ticker}: {str(e)[:60]}", "erro")
                empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})
            time.sleep(0.4)
        resultado["setores"][sid] = {"nome": s["nome"], "icone": s["icone"], "cor_fundo": s["cor_fundo"], "empresas": empresas}

    return resultado

def loop_auto():
    while True:
        time.sleep(INTERVALO)
        log(f"⏱️  Atualização automática ({INTERVALO//60} min)", "info")
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

@app.route("/api/logs")
def api_logs():
    return jsonify(_log_entries)

@app.route("/api/logs/stream")
def api_logs_stream():
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)
    def generate():
        yield f"data: {json.dumps({'ts':'','msg':'Conectado ao log em tempo real','tipo':'info'}, ensure_ascii=False)}\n\n"
        while True:
            try:
                data = q.get(timeout=30)
                yield data
            except queue.Empty:
                yield "data: {\"ping\":true}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    log("🚀 Servidor iniciado", "info")
    threading.Thread(target=atualizar_cache, daemon=True).start()
    threading.Thread(target=loop_auto, daemon=True).start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
