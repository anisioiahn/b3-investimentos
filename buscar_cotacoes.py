import json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import os, re, unicodedata

TOKEN = os.environ.get("BRAPI_TOKEN", "iSm92y2Qg4f9iapi1MuHhh")
BASE_URL = "https://brapi.dev/api/quote"
LIST_URL = "https://brapi.dev/api/quote/list"
OUTPUT_FILE = "cotacoes.json"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

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

CORES = [
    "#005a2b","#1565c0","#c62828","#e65100","#6a1b9a","#00695c",
    "#37474f","#f57f17","#283593","#bf360c","#33691e","#1a237e",
]

def cor_para_ticker(ticker):
    idx = sum(ord(c) for c in ticker) % len(CORES)
    return CORES[idx]

def buscar_setores_disponiveis():
    try:
        resp = requests.get(f"{LIST_URL}?limit=1&token={TOKEN}", timeout=15)
        if resp.status_code == 200:
            return resp.json().get("availableSectors", [])
    except Exception as e:
        print(f"Erro ao buscar setores: {e}")
    return list(SETOR_MAP.keys())

def buscar_ativos_por_setor(setor, pagina=1, limite=50):
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
    for tentativa in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/{ticker}", headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                return results[0] if results else None
            elif resp.status_code == 429:
                wait = 15 * (tentativa + 1)
                time.sleep(wait)
                continue
        except Exception as e:
            print(f"  ⚠️ {ticker}: {e}")
            return None
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
        print(f"  ⚠️ Histórico {ticker}: {e}")
    return []

DIAS_MAX_NOTICIA = 30  # notícia mais velha que isso é descartada, mesmo que mencione o ativo

# Palavras do nome da empresa genéricas demais pra servir de filtro sozinhas
# (ex: "Banco do Brasil" -> "banco" apareceria em qualquer notícia de banco)
_STOPWORDS_EMPRESA = {
    "do", "da", "de", "das", "dos", "e", "s.a", "sa", "ltda", "grupo",
    "banco", "companhia", "cia", "brasil", "brasileira", "holding", "participacoes",
}

def _parse_data_noticia(data_str):
    """
    Converte a data de uma notícia (RSS = RFC 822, Atom = ISO 8601) num
    datetime de verdade — antes o código só cortava a string pros
    primeiros 16 caracteres, sem nunca comparar datas de verdade, o que
    permitia notícia de meses atrás aparecer misturada com as recentes.
    Devolve None se não conseguir interpretar (nesse caso a notícia é
    descartada por segurança, não fica um formato de data estranho na tela).
    """
    if not data_str:
        return None
    data_str = data_str.strip()
    try:
        dt = parsedate_to_datetime(data_str)  # RFC 822: "Wed, 04 Mar 2026 10:30:00 +0000"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(data_str.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    return None

def _termos_relevantes_empresa(nome_empresa):
    """Palavras do nome da empresa que servem de filtro sozinhas — exclui
    conectores e termos genéricos demais (seção 'Banco'/'Grupo'/etc)."""
    if not nome_empresa:
        return []
    palavras = nome_empresa.lower().replace(".", "").replace(",", "").split()
    return [p for p in palavras if p not in _STOPWORDS_EMPRESA and len(p) > 2]

def _texto_menciona_ativo(texto, ticker, nome_empresa):
    """
    Confere se um texto realmente é sobre o ativo — antes só checava a
    PRIMEIRA palavra do nome da empresa, o que gerava falso positivo em
    nomes com palavra inicial genérica ("Banco do Brasil" -> "banco" bate
    em qualquer notícia de banco). Agora exige o ticker, o nome completo,
    ou uma palavra realmente específica do nome (não genérica).
    """
    texto = texto.lower()
    ticker_lower = ticker.lower()
    if ticker_lower in texto:
        return True
    nome_completo = (nome_empresa or "").lower().strip()
    if nome_completo and nome_completo in texto:
        return True
    termos = _termos_relevantes_empresa(nome_empresa)
    return any(t in texto for t in termos)

def _parse_rss(content, ticker, nome_empresa, max_items=3):
    """
    Parseia XML RSS/Atom, filtra por ticker/nome, descarta notícia sem
    data interpretável ou mais velha que DIAS_MAX_NOTICIA, e ordena da
    mais recente pra mais antiga antes de cortar pros max_items — antes
    pegava os primeiros itens que batiam no filtro, na ordem crua do
    feed, sem checar se eram realmente recentes.
    """
    candidatas = []
    try:
        root = ET.fromstring(content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        agora = datetime.now(timezone.utc)
        limite_antiguidade = agora - timedelta(days=DIAS_MAX_NOTICIA)

        for item in items[:30]:
            titulo = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
            link   = (item.findtext("link")  or item.findtext("atom:link",  namespaces=ns) or "").strip()
            desc   = (item.findtext("description") or item.findtext("atom:summary", namespaces=ns) or "").strip()
            data_raw = (item.findtext("pubDate") or item.findtext("atom:published", namespaces=ns) or "").strip()

            texto = titulo + " " + desc
            if not _texto_menciona_ativo(texto, ticker, nome_empresa):
                continue

            data_dt = _parse_data_noticia(data_raw)
            if data_dt is None or data_dt < limite_antiguidade:
                continue  # sem data confiável, ou velha demais — descarta

            candidatas.append({
                "titulo": titulo[:120],
                "link": link,
                "data": data_dt.strftime("%d/%m/%Y %H:%M"),
                "resumo": desc[:200],
                "_data_dt": data_dt,  # só pra ordenar, removido antes de devolver
            })
    except Exception:
        pass

    candidatas.sort(key=lambda n: n["_data_dt"], reverse=True)
    for n in candidatas:
        del n["_data_dt"]
    return candidatas[:max_items]

def _slugificar_nome(nome_empresa):
    """
    Gera o slug que sites de notícias costumam usar em tags/categorias
    baseadas no NOME da empresa (ex: InfoMoney usa /tudo-sobre/axia/,
    não /tudo-sobre/axia3/ — o ticker não é a mesma coisa que o slug
    de conteúdo do site). 'Axia Energia' -> 'axia-energia'.

    ATENÇÃO: isso é uma aproximação (nome completo, minúsculo, hífens).
    Alguns sites usam só a primeira palavra do nome como tag (ex:
    InfoMoney tem tanto /tudo-sobre/axia/ quanto /tudo-sobre/axia-energia/
    como páginas válidas; já o Money Times só tem /tag/axia/, não
    /tag/axia-energia/) — pra esse segundo caso use {slug_curto} em vez
    de {slug} na URL da fonte. Vale conferir manualmente qual delas tem
    mais conteúdo antes de configurar a URL de uma fonte nova.
    """
    if not nome_empresa:
        return ""
    texto = unicodedata.normalize("NFKD", nome_empresa).encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    texto = re.sub(r"[^a-z0-9\s-]", "", texto)
    texto = re.sub(r"\s+", "-", texto)
    return texto

def _slugificar_primeira_palavra(nome_empresa):
    """
    Slug de só a primeira palavra do nome — alguns sites (ex: Money
    Times) taggeiam empresas só pelo nome curto/comercial, não pelo
    nome completo. 'Axia Energia' -> 'axia', 'Petróleo Brasileiro' -> 'petroleo'.
    """
    if not nome_empresa:
        return ""
    primeira = nome_empresa.strip().split()[0] if nome_empresa.strip() else ""
    return _slugificar_nome(primeira)

def buscar_noticias_rss(ticker, nome_empresa, fontes):
    """
    Busca notícias de cada fonte RSS configurada.
    - {ticker} na URL vira o ticker em minúsculo (ex: petr4).
    - {slug} na URL vira o nome da empresa "slugificado" (ex: axia-energia)
      — use isso pra fontes que organizam conteúdo por nome da empresa,
      não por ticker (é o caso do InfoMoney, por exemplo).
    - Se a URL é genérica (sem {ticker}/{slug}), filtra os itens pelo
      ticker/nome depois de buscar.
    - Se a fonte configurada não trouxer NADA, o frame correspondente
      fica vazio de propósito ("sem notícias recentes") — NUNCA
      substitui silenciosamente por outra fonte disfarçada do nome
      configurado (isso já causou notícia errada aparecendo como se
      fosse do site configurado, e a MESMA notícia repetida em vários
      frames quando várias fontes falhavam ao mesmo tempo).
    """
    noticias_por_fonte = {}
    hdrs = {"User-Agent": "Mozilla/5.0"}

    for fonte in fontes:
        noticias = []
        try:
            url_rss = fonte.get("url", "")
            url_rss = url_rss.replace("{ticker}", ticker.lower())
            url_rss = url_rss.replace("{slug}", _slugificar_nome(nome_empresa))
            url_rss = url_rss.replace("{slug_curto}", _slugificar_primeira_palavra(nome_empresa))
            if url_rss:
                resp = requests.get(url_rss, timeout=10, headers=hdrs)
                if resp.status_code == 200:
                    noticias = _parse_rss(resp.content, ticker, nome_empresa)
                else:
                    # Log específico pra status != 200 — ajuda a detectar
                    # URL de fonte mal configurada (como o caso do InfoMoney
                    # usando {ticker} onde precisava de {slug}) em vez de só
                    # "sem notícia" silencioso indistinguível de "nada novo hoje"
                    print(f"  ⚠️ RSS {fonte['nome']} devolveu status {resp.status_code} pra URL {url_rss} — confira se a URL configurada está certa pra este site")
        except Exception as e:
            print(f"  ⚠️ RSS {fonte['nome']}: {e}")

        noticias_por_fonte[fonte["nome"]] = noticias

    return noticias_por_fonte

def montar_url_rss(fonte, ticker, nome_empresa=""):
    base = fonte.get("url", "")
    base = base.replace("{ticker}", ticker.lower())
    base = base.replace("{slug}", _slugificar_nome(nome_empresa))
    base = base.replace("{slug_curto}", _slugificar_primeira_palavra(nome_empresa))
    return base

def buscar_todas_cotacoes():
    resultado = {"atualizado_em": datetime.now().isoformat(), "setores": {}}
    setores_api = buscar_setores_disponiveis()
    print(f"Setores encontrados na brapi: {setores_api}")

    for setor_api in setores_api:
        info = SETOR_MAP.get(setor_api, {"nome": setor_api, "icone": "📈", "cor_fundo": "#f5f5f5"})
        print(f"\n🔍 {info['nome']}")
        ativos, _ = buscar_ativos_por_setor(setor_api, limite=30)
        empresas = []
        for ativo in ativos:
            ticker = ativo.get("stock", "")
            if not ticker: continue
            preco = ativo.get("close")
            variacao_pct = ativo.get("change")
            nome = ativo.get("name", ticker)
            if preco:
                print(f"   ✅ {ticker}: R$ {preco} ({variacao_pct:+.2f}%)" if variacao_pct else f"   ✅ {ticker}: R$ {preco}")
            empresas.append({
                "ticker": ticker, "nome": nome, "cor": cor_para_ticker(ticker),
                "preco": preco, "variacao": ativo.get("change_abs"),
                "variacao_pct": variacao_pct, "maxima_dia": ativo.get("high"),
                "minima_dia": ativo.get("low"), "volume": ativo.get("volume"),
                "logo": ativo.get("logourl", ""),
            })
            time.sleep(0.3)

        setor_id = setor_api.lower().replace(" ", "_")
        resultado["setores"][setor_id] = {
            "nome": info["nome"], "icone": info["icone"], "cor_fundo": info["cor_fundo"],
            "empresas": sorted(empresas, key=lambda x: x.get("preco") or 0, reverse=True),
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    return resultado

if __name__ == "__main__":
    buscar_todas_cotacoes()
