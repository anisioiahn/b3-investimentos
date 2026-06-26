import json, time, requests
from datetime import datetime

TOKEN = "iSm92y2Qg4f9iapi1MuHhh"
BASE_URL = "https://brapi.dev/api/quote"
OUTPUT_FILE = "cotacoes.json"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

SETORES = {
    "petroleo":         {"nome":"Petróleo, Gás e Biocombustíveis","icone":"🛢️","cor_fundo":"#e8f5e9","tickers":{"PETR4":{"nome":"Petrobras PN","cor":"#005a2b"},"PETR3":{"nome":"Petrobras ON","cor":"#007a3d"}}},
    "utilidade":        {"nome":"Utilidade Pública","icone":"⚡","cor_fundo":"#fff8e1","tickers":{"ENGI11":{"nome":"Energisa","cor":"#f9a825"},"CPFE3":{"nome":"CPFL Energia","cor":"#b71c1c"},"TAEE11":{"nome":"Taesa","cor":"#00695c"},"EQTL3":{"nome":"Equatorial","cor":"#1565c0"},"CMIG4":{"nome":"Cemig","cor":"#7b1fa2"}}},
    "materiais":        {"nome":"Materiais Básicos","icone":"🪨","cor_fundo":"#efebe9","tickers":{"VALE3":{"nome":"Vale","cor":"#1a5276"},"CSAN3":{"nome":"Cosan","cor":"#1a237e"},"SUZB3":{"nome":"Suzano","cor":"#1b5e20"},"KLBN11":{"nome":"Klabin","cor":"#33691e"},"DXCO3":{"nome":"Dexco","cor":"#5d4037"},"GGBR4":{"nome":"Gerdau","cor":"#37474f"},"CSNA3":{"nome":"CSN","cor":"#263238"}}},
    "industriais":      {"nome":"Bens Industriais","icone":"🏗️","cor_fundo":"#fffde7","tickers":{"WEGE3":{"nome":"WEG","cor":"#003366"},"EMBR3":{"nome":"Embraer","cor":"#003a80"},"RAIL3":{"nome":"Rumo","cor":"#bf360c"},"UGPA3":{"nome":"Ultrapar","cor":"#e65100"},"CYRE3":{"nome":"Cyrela","cor":"#1565c0"},"MRVE3":{"nome":"MRV","cor":"#f57f17"},"EZTC3":{"nome":"EZTEC","cor":"#004d40"},"DIRR3":{"nome":"Direcional","cor":"#c62828"},"TEND3":{"nome":"Tenda","cor":"#1a237e"}}},
    "financeiro":       {"nome":"Financeiro","icone":"🏦","cor_fundo":"#e3f2fd","tickers":{"ITUB4":{"nome":"Itaú Unibanco","cor":"#ff6600"},"BBDC4":{"nome":"Bradesco","cor":"#cc0000"},"BBAS3":{"nome":"Banco do Brasil","cor":"#003399"},"SANB11":{"nome":"Santander BR","cor":"#cc0000"},"B3SA3":{"nome":"B3 S.A.","cor":"#003a80"},"BPAC11":{"nome":"BTG Pactual","cor":"#1a1a2e"}}},
    "consumo_nciclico": {"nome":"Consumo Não Cíclico","icone":"🌾","cor_fundo":"#f1f8e9","tickers":{"ABEV3":{"nome":"Ambev","cor":"#f9a825"},"JBSS3":{"nome":"JBS","cor":"#c62828"},"BEEF3":{"nome":"Minerva","cor":"#bf360c"},"SLCE3":{"nome":"SLC Agrícola","cor":"#33691e"},"SMTO3":{"nome":"São Martinho","cor":"#2e7d32"},"AGRO3":{"nome":"BrasilAgro","cor":"#1b5e20"}}},
    "consumo_ciclico":  {"nome":"Consumo Cíclico","icone":"🛍️","cor_fundo":"#f3e5f5","tickers":{"LREN3":{"nome":"Lojas Renner","cor":"#c62828"},"ASAI3":{"nome":"Assaí","cor":"#e53935"},"MGLU3":{"nome":"Magazine Luiza","cor":"#0000cc"},"PCAR3":{"nome":"Grupo GPA","cor":"#f57c00"},"SOMA3":{"nome":"Grupo Soma","cor":"#6a1b9a"},"CVCB3":{"nome":"CVC Corp","cor":"#ff5722"}}},
    "saude":            {"nome":"Saúde","icone":"🏥","cor_fundo":"#ffebee","tickers":{"RDOR3":{"nome":"Rede D'Or","cor":"#c62828"},"HAPV3":{"nome":"Hapvida","cor":"#0277bd"},"FLRY3":{"nome":"Fleury","cor":"#1565c0"},"HYPE3":{"nome":"Hypera","cor":"#006064"},"DASA3":{"nome":"Dasa","cor":"#0288d1"}}},
    "comunicacoes":     {"nome":"Comunicações","icone":"📡","cor_fundo":"#e0f2f1","tickers":{"VIVT3":{"nome":"Telefônica Vivo","cor":"#6200ea"},"TIMS3":{"nome":"TIM","cor":"#0000cc"}}},
    "tecnologia":       {"nome":"Tecnologia da Informação","icone":"💻","cor_fundo":"#ede7f6","tickers":{"TOTS3":{"nome":"TOTVS","cor":"#e53935"},"LWSA3":{"nome":"Locaweb","cor":"#0033cc"},"INTB3":{"nome":"Intelbras","cor":"#1a237e"}}},
}

def buscar_ticker(ticker):
    try:
        resp = requests.get(f"{BASE_URL}/{ticker}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return results[0] if results else None
        print(f"  ⚠️  {ticker}: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️  {ticker}: {e}")
    return None

def buscar_historico(ticker):
    try:
        url = f"{BASE_URL}/{ticker}?range=1y&interval=1d"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results and results[0].get("historicalDataPrice"):
                hist = results[0]["historicalDataPrice"]
                return [{"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]
    except Exception as e:
        print(f"  ⚠️  Histórico {ticker}: {e}")
    return []

def buscar_todas_cotacoes():
    resultado = {"atualizado_em": datetime.now().isoformat(), "setores": {}}
    for sid, s in SETORES.items():
        print(f"\n🔍 {s['nome']}")
        empresas = []
        for ticker, meta in s["tickers"].items():
            d = buscar_ticker(ticker)
            if d:
                print(f"   ✅ {ticker}: R$ {d.get('regularMarketPrice')}")
                empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": d.get("regularMarketPrice"), "variacao": d.get("regularMarketChange"), "variacao_pct": d.get("regularMarketChangePercent"), "maxima_dia": d.get("regularMarketDayHigh"), "minima_dia": d.get("regularMarketDayLow"), "volume": d.get("regularMarketVolume"), "logo": d.get("logourl")})
            else:
                print(f"   ❌ {ticker}: não encontrado")
                empresas.append({"ticker": ticker, "nome": meta["nome"], "cor": meta["cor"], "preco": None})
            time.sleep(0.4)
        resultado["setores"][sid] = {"nome": s["nome"], "icone": s["icone"], "cor_fundo": s["cor_fundo"], "empresas": empresas}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    return resultado

if __name__ == "__main__":
    buscar_todas_cotacoes()
