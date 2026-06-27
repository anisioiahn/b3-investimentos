import json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime
import os

TOKEN = os.environ.get("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
BASE_URL = "https://brapi.dev/api/quote"
LIST_URL = "https://brapi.dev/api/quote/list"
OUTPUT_FILE = "cotacoes.json"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Mapeamento dos nomes de setores da brapi para português
SETOR_MAP = {
    "Finance": {"nome": "Financeiro", "icone": "🏦", "cor_fundo": "#e3f2fd"},
    "Energy": {"nome": "Petróleo, Gás e Biocombustíveis", "icone": "🛢️", "cor_fundo": "#e8f5e9"},
    "Basic Materials": {"nome": "Materiais Básicos", "icone": "🪨", "cor_fundo": "#efebe9"},
    "Industrials": {"nome": "Bens Industriais", "icone": "🏗️", "cor_fundo": "#fffde7"},
    "Consumer Cyclical": {"nome": "Consumo Cíclico", "icone": "🛍️", "cor_fundo": "#f3e5f5"},
    "Consumer Defensive": {"nome": "Consumo Não Cíclico", "icone": "🌾", "cor_fundo": "#f1f8e9"},
    "Healthcare": {"nome": "Saúde", "icone": "🏥", "cor_fundo": "#ffebee"},
    "Communication Services": {"nome": "Comunicações", "icone": "📡", "cor_fundo": "#e0f2f1"},
    "Technology": {"nome": "Tecnologia da Informação", "icone": "💻", "cor_fundo": "#ede7f6"},
    "Utilities": {"nome": "Utilidade Pública", "icone": "⚡", "cor_fundo": "#fff8e1"},
    "Real Estate": {"nome": "Imobiliário", "icone": "🏢", "cor_fundo": "#fce4ec"},
}

# Cores por ticker para logos
CORES = [
    "#005a2b","#1565c0","#c62828","#e65100","#6a1b9a","#00695c",
    "#37474f","#f57f17","#283593","#bf360c","#33691e","#1a237e",
]

def cor_para_ticker(ticker):
    idx = sum(ord(c) for c in ticker) % len(CORES)
    return CORES[idx]

def buscar_setores_disponiveis():
    """Busca os setores disponíveis na brapi."""
    try:
        resp = requests.get(f"{LIST_URL}?limit=1&token={TOKEN}", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("availableSectors", [])
    except Exception as e:
        print(f"Erro ao buscar setores: {e}")
    return list(SETOR_MAP.keys())

def buscar_ativos_por_setor(setor, pagina=1, limite=50):
    """Busca ativos de um setor específico com cotação."""
    try:
        url = f"{LIST_URL}?sector={setor}&type=stock&sortBy=market_cap_basic&sortOrder=desc&limit={limite}&page={pagina}&token={TOKEN}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("stocks", []), data.get("hasNextPage", False)
        elif resp.status_code == 429:
            print(f"  ⏳ Rate limit, aguardando 15s...")
            time.sleep(15)
            return buscar_ativos_por_setor(setor, pagina, limite)
    except Exception as e:
        print(f"  Erro ao buscar {setor}: {e}")
    return [], False

def buscar_ticker(ticker):
    """Busca cotação detalhada de 1 ticker."""
    for tentativa in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/{ticker}", headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                return results[0] if results else None
            elif resp.status_code == 429:
                wait = 15 * (tentativa + 1)
                print(f"  ⏳ {ticker}: rate limit, aguardando {wait}s...")
                time.sleep(wait)
                continue
            else:
                return None
        except Exception as e:
            print(f"  ⚠️ {ticker}: {e}")
            return None
    return None

def buscar_historico(ticker):
    """Busca histórico de 1 ano."""
    try:
        url = f"{BASE_URL}/{ticker}?range=1y&interval=1d"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results and results[0].get("historicalDataPrice"):
                hist = results[0]["historicalDataPrice"]
                return [{"date": h.get("date"), "close": h.get("close")} for h in hist if h.get("close")]
    except Exception as e:
        print(f"  ⚠️ Histórico {ticker}: {e}")
    return []

def buscar_noticias_rss(ticker, nome_empresa, fontes):
    """Busca notícias de cada fonte RSS configurada."""
    noticias_por_fonte = {}
    
    for fonte in fontes:
        noticias = []
        try:
            url_rss = montar_url_rss(fonte, ticker)
            resp = requests.get(url_rss, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                items = root.findall(".//item") or root.findall(".//atom:entry", ns)
                ticker_lower = ticker.lower()
                nome_lower = nome_empresa.lower().split()[0] if nome_empresa else ticker_lower
                for item in items[:20]:
                    titulo = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
                    link = (item.findtext("link") or item.findtext("atom:link", namespaces=ns) or "").strip()
                    desc = (item.findtext("description") or item.findtext("atom:summary", namespaces=ns) or "").strip()
                    data = (item.findtext("pubDate") or item.findtext("atom:published", namespaces=ns) or "").strip()
                    texto = (titulo + " " + desc).lower()
                    if ticker_lower in texto or nome_lower in texto:
                        noticias.append({"titulo": titulo[:120], "link": link, "data": data[:16], "resumo": desc[:200]})
                    if len(noticias) >= 3:
                        break
        except Exception as e:
            print(f"  ⚠️ RSS {fonte['nome']}: {e}")
        noticias_por_fonte[fonte["nome"]] = noticias[:3]
    
    return noticias_por_fonte

def montar_url_rss(fonte, ticker):
    """Monta a URL do RSS de acordo com a fonte configurada."""
    base = fonte.get("url", "")
    if "{ticker}" in base:
        return base.replace("{ticker}", ticker.lower())
    return base

def buscar_todas_cotacoes():
    """Busca todos os setores e ativos dinamicamente da brapi."""
    resultado = {"atualizado_em": datetime.now().isoformat(), "setores": {}}
    
    setores_api = buscar_setores_disponiveis()
    print(f"Setores encontrados na brapi: {setores_api}")

    for setor_api in setores_api:
        info = SETOR_MAP.get(setor_api, {
            "nome": setor_api,
            "icone": "📈",
            "cor_fundo": "#f5f5f5",
        })
        print(f"\n🔍 {info['nome']}")
        
        ativos, _ = buscar_ativos_por_setor(setor_api, limite=30)
        empresas = []
        
        for ativo in ativos:
            ticker = ativo.get("stock", "")
            if not ticker:
                continue
            preco = ativo.get("close")
            variacao = ativo.get("change_abs")
            variacao_pct = ativo.get("change")
            nome = ativo.get("name", ticker)
            logo = ativo.get("logourl", "")
            
            if preco:
                print(f"   ✅ {ticker}: R$ {preco} ({variacao_pct:+.2f}%)" if variacao_pct else f"   ✅ {ticker}: R$ {preco}")
            else:
                print(f"   ⚠️  {ticker}: sem preço")
            
            empresas.append({
                "ticker": ticker,
                "nome": nome,
                "cor": cor_para_ticker(ticker),
                "preco": preco,
                "variacao": variacao,
                "variacao_pct": variacao_pct,
                "maxima_dia": ativo.get("high"),
                "minima_dia": ativo.get("low"),
                "volume": ativo.get("volume"),
                "logo": logo,
            })
            time.sleep(0.3)

        setor_id = setor_api.lower().replace(" ", "_")
        resultado["setores"][setor_id] = {
            "nome": info["nome"],
            "icone": info["icone"],
            "cor_fundo": info["cor_fundo"],
            "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    return resultado

if __name__ == "__main__":
    buscar_todas_cotacoes()
