import os, json, threading, time
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request
from buscar_cotacoes import buscar_todas_cotacoes, buscar_historico, OUTPUT_FILE

app = Flask(__name__, static_folder="static")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
_lock = threading.Lock()
_cache = {"atualizado_em": None, "setores": {}}

def atualizar_cache():
    global _cache
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Atualizando cotações...")
    novo = buscar_todas_cotacoes()
    with _lock:
        _cache = novo
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Cotações atualizadas.")

def loop_auto():
    while True:
        time.sleep(INTERVALO)
        try: atualizar_cache()
        except Exception as e: print(f"Erro auto: {e}")

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/api/cotacoes")
def api_cotacoes():
    with _lock: return jsonify(_cache)

@app.route("/api/atualizar", methods=["POST"])
def api_atualizar():
    atualizar_cache()
    with _lock: return jsonify(_cache)

@app.route("/api/historico/<ticker>")
def api_historico(ticker):
    """Busca histórico de 1 ano para o gráfico — chamado sob demanda pelo front."""
    dados = buscar_historico(ticker.upper())
    return jsonify({"ticker": ticker.upper(), "historico": dados})

if __name__ == "__main__":
    atualizar_cache()
    t = threading.Thread(target=loop_auto, daemon=True)
    t.start()
    print(f"\n🚀 Servidor rodando em http://localhost:5000")
    print(f"⏱️  Atualização automática a cada {INTERVALO // 60} minutos\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
