# ============================================================
# JANUS BACKTESTING ENGINE v1.0
# Motor de simulação de estratégias de investimento
# Estratégias: Buy & Hold, Médias Móveis, Janus Score
# ============================================================

import os
import numpy as np
import pandas as pd
from datetime import date, datetime, timezone, timedelta
import psycopg2
import psycopg2.extras

TZ_BR = timezone(timedelta(hours=-3))

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

# ── BUSCA DE DADOS ────────────────────────────────────────────

def carregar_historico(ticker, data_inicio, data_fim):
    """Carrega histórico do banco e retorna DataFrame."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT data, open, high, low, close, volume
                FROM historico_precos
                WHERE ticker=%s AND intervalo='1d'
                  AND data BETWEEN %s AND %s
                ORDER BY data ASC
            """, (ticker, data_inicio, data_fim))
            rows = cur.fetchall()
        conn.close()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        df['data'] = pd.to_datetime(df['data'])
        df = df.set_index('data')
        for col in ['open','high','low','close','volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['close'])
        return df
    except Exception as e:
        print(f"[BT] Erro carregar histórico {ticker}: {e}", flush=True)
        return None

def carregar_ibovespa(data_inicio, data_fim):
    """Carrega histórico do IBOVESPA para benchmark."""
    return carregar_historico('^BVSP', data_inicio, data_fim)

def calcular_cdi_periodo(data_inicio, data_fim):
    """Estima retorno CDI no período (taxa aproximada)."""
    # CDI médio histórico aproximado: 11% ao ano
    # Ideal: buscar da API do Banco Central no futuro
    dias = (data_fim - data_inicio).days
    taxa_anual = 0.115  # 11.5% a.a. aproximado
    retorno = (1 + taxa_anual) ** (dias / 365) - 1
    return retorno * 100

# ── INDICADORES TÉCNICOS ──────────────────────────────────────

def calcular_indicadores(df):
    """Calcula todos os indicadores técnicos no DataFrame."""
    df = df.copy()

    # Médias Móveis
    df['mm9']  = df['close'].rolling(9).mean()
    df['mm21'] = df['close'].rolling(21).mean()
    df['mm50'] = df['close'].rolling(50).mean()
    df['mm200']= df['close'].rolling(200).mean()

    # RSI (14 períodos)
    delta  = df['close'].diff()
    ganhos = delta.clip(lower=0)
    perdas = (-delta).clip(lower=0)
    media_ganhos = ganhos.rolling(14).mean()
    media_perdas = perdas.rolling(14).mean()
    rs = media_ganhos / media_perdas.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd']        = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist']   = df['macd'] - df['macd_signal']

    # Bollinger Bands (20 períodos, 2 desvios)
    df['bb_media'] = df['close'].rolling(20).mean()
    bb_std         = df['close'].rolling(20).std()
    df['bb_sup']   = df['bb_media'] + 2 * bb_std
    df['bb_inf']   = df['bb_media'] - 2 * bb_std

    return df

# ── MÉTRICAS DE RESULTADO ─────────────────────────────────────

def calcular_metricas(patrimonio_series, operacoes, capital_inicial):
    """Calcula todas as métricas de performance."""
    if len(patrimonio_series) < 2:
        return {}

    patrimonio = np.array(patrimonio_series)
    retornos   = np.diff(patrimonio) / patrimonio[:-1]

    # Retorno total
    retorno_total = (patrimonio[-1] - patrimonio[0]) / patrimonio[0] * 100

    # Retorno anualizado
    n_dias = len(patrimonio)
    retorno_anualizado = ((patrimonio[-1] / patrimonio[0]) ** (252 / n_dias) - 1) * 100

    # Drawdown máximo
    pico = np.maximum.accumulate(patrimonio)
    drawdown_serie = (patrimonio - pico) / pico * 100
    drawdown_max   = float(drawdown_serie.min())

    # Volatilidade anualizada
    volatilidade = float(np.std(retornos) * np.sqrt(252) * 100)

    # Sharpe Ratio (taxa livre de risco ~11.5% ao ano = ~0.044% ao dia)
    taxa_livre = 0.115 / 252
    sharpe = 0.0
    if np.std(retornos) > 0:
        sharpe = float((np.mean(retornos) - taxa_livre) / np.std(retornos) * np.sqrt(252))

    # Sortino Ratio (só retornos negativos)
    retornos_neg = retornos[retornos < taxa_livre]
    sortino = 0.0
    if len(retornos_neg) > 0 and np.std(retornos_neg) > 0:
        sortino = float((np.mean(retornos) - taxa_livre) / np.std(retornos_neg) * np.sqrt(252))

    # Operações
    ops_vencedoras  = [o for o in operacoes if o.get('resultado_pct', 0) > 0]
    ops_perdedoras  = [o for o in operacoes if o.get('resultado_pct', 0) <= 0]
    taxa_acerto     = len(ops_vencedoras) / len(operacoes) * 100 if operacoes else 0

    # Profit Factor
    ganhos_total  = sum(o.get('resultado_pct', 0) for o in ops_vencedoras)
    perdas_total  = abs(sum(o.get('resultado_pct', 0) for o in ops_perdedoras))
    profit_factor = ganhos_total / perdas_total if perdas_total > 0 else 0

    # Maior ganho e maior perda
    maior_ganho = max((o.get('resultado_pct', 0) for o in operacoes), default=0)
    maior_perda = min((o.get('resultado_pct', 0) for o in operacoes), default=0)

    return {
        'capital_inicial':      round(capital_inicial, 2),
        'capital_final':        round(float(patrimonio[-1]), 2),
        'lucro':                round(float(patrimonio[-1]) - capital_inicial, 2),
        'retorno_pct':          round(retorno_total, 2),
        'retorno_anualizado':   round(retorno_anualizado, 2),
        'drawdown_max':         round(drawdown_max, 2),
        'volatilidade':         round(volatilidade, 2),
        'sharpe':               round(sharpe, 2),
        'sortino':              round(sortino, 2),
        'n_operacoes':          len(operacoes),
        'ops_vencedoras':       len(ops_vencedoras),
        'ops_perdedoras':       len(ops_perdedoras),
        'taxa_acerto':          round(taxa_acerto, 1),
        'profit_factor':        round(profit_factor, 2),
        'maior_ganho':          round(maior_ganho, 2),
        'maior_perda':          round(maior_perda, 2),
    }

# ── ESTRATÉGIAS ───────────────────────────────────────────────

def simular_buy_and_hold(df, capital_inicial):
    """Compra no início, vende no final."""
    preco_compra = float(df['close'].iloc[0])
    preco_venda  = float(df['close'].iloc[-1])
    qtd = capital_inicial / preco_compra

    operacoes = [{
        'tipo':          'COMPRA',
        'data':          str(df.index[0].date()),
        'preco':         round(preco_compra, 2),
        'quantidade':    round(qtd, 4),
        'valor':         round(capital_inicial, 2),
    }, {
        'tipo':           'VENDA',
        'data':           str(df.index[-1].date()),
        'preco':          round(preco_venda, 2),
        'quantidade':     round(qtd, 4),
        'valor':          round(qtd * preco_venda, 2),
        'resultado_pct':  round((preco_venda - preco_compra) / preco_compra * 100, 2),
    }]

    # Curva de patrimônio diária
    patrimonio = [capital_inicial * (float(p) / preco_compra) for p in df['close']]
    datas      = [str(d.date()) for d in df.index]

    return operacoes, patrimonio, datas

def simular_medias_moveis(df, capital_inicial, mm_rapida=9, mm_lenta=21):
    """Compra quando MM rápida cruza acima da MM lenta e vice-versa."""
    df = df.copy()
    df[f'mm{mm_rapida}']  = df['close'].rolling(mm_rapida).mean()
    df[f'mm{mm_lenta}']   = df['close'].rolling(mm_lenta).mean()
    df = df.dropna()

    capital    = capital_inicial
    posicao    = 0.0  # quantidade de ações
    em_posicao = False
    preco_entrada = 0.0
    data_entrada  = None
    operacoes  = []
    patrimonio = []
    datas      = []

    for i in range(1, len(df)):
        row_ant = df.iloc[i-1]
        row_atu = df.iloc[i]
        preco   = float(row_atu['close'])
        mm_r    = float(row_atu[f'mm{mm_rapida}'])
        mm_l    = float(row_atu[f'mm{mm_lenta}'])
        mm_r_ant= float(row_ant[f'mm{mm_rapida}'])
        mm_l_ant= float(row_ant[f'mm{mm_lenta}'])

        # Sinal de compra: MM rápida cruza acima da lenta
        if not em_posicao and mm_r > mm_l and mm_r_ant <= mm_l_ant:
            posicao     = capital / preco
            preco_entrada = preco
            data_entrada  = str(row_atu.name.date())
            em_posicao  = True
            operacoes.append({
                'tipo': 'COMPRA', 'data': data_entrada,
                'preco': round(preco, 2),
                'quantidade': round(posicao, 4),
                'valor': round(capital, 2),
            })

        # Sinal de venda: MM rápida cruza abaixo da lenta
        elif em_posicao and mm_r < mm_l and mm_r_ant >= mm_l_ant:
            capital = posicao * preco
            resultado_pct = (preco - preco_entrada) / preco_entrada * 100
            operacoes.append({
                'tipo': 'VENDA', 'data': str(row_atu.name.date()),
                'preco': round(preco, 2),
                'quantidade': round(posicao, 4),
                'valor': round(capital, 2),
                'resultado_pct': round(resultado_pct, 2),
            })
            posicao    = 0.0
            em_posicao = False

        # Patrimônio do dia
        pat = posicao * preco if em_posicao else capital
        patrimonio.append(pat)
        datas.append(str(row_atu.name.date()))

    # Fecha posição aberta no final
    if em_posicao and len(df) > 0:
        preco_final = float(df['close'].iloc[-1])
        capital = posicao * preco_final
        resultado_pct = (preco_final - preco_entrada) / preco_entrada * 100
        operacoes.append({
            'tipo': 'VENDA', 'data': str(df.index[-1].date()),
            'preco': round(preco_final, 2),
            'quantidade': round(posicao, 4),
            'valor': round(capital, 2),
            'resultado_pct': round(resultado_pct, 2),
        })

    return operacoes, patrimonio, datas

def simular_rsi(df, capital_inicial, rsi_compra=30, rsi_venda=70):
    """Compra quando RSI < rsi_compra, vende quando RSI > rsi_venda."""
    df = df.copy()
    delta  = df['close'].diff()
    ganhos = delta.clip(lower=0).rolling(14).mean()
    perdas = (-delta).clip(lower=0).rolling(14).mean()
    rs     = ganhos / perdas.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df = df.dropna()

    capital    = capital_inicial
    posicao    = 0.0
    em_posicao = False
    preco_entrada = 0.0
    operacoes  = []
    patrimonio = []
    datas      = []

    for i in range(len(df)):
        row   = df.iloc[i]
        preco = float(row['close'])
        rsi   = float(row['rsi']) if not np.isnan(row['rsi']) else 50

        if not em_posicao and rsi < rsi_compra:
            posicao       = capital / preco
            preco_entrada = preco
            em_posicao    = True
            operacoes.append({
                'tipo': 'COMPRA', 'data': str(row.name.date()),
                'preco': round(preco, 2),
                'quantidade': round(posicao, 4),
                'valor': round(capital, 2),
            })
        elif em_posicao and rsi > rsi_venda:
            capital = posicao * preco
            resultado_pct = (preco - preco_entrada) / preco_entrada * 100
            operacoes.append({
                'tipo': 'VENDA', 'data': str(row.name.date()),
                'preco': round(preco, 2),
                'quantidade': round(posicao, 4),
                'valor': round(capital, 2),
                'resultado_pct': round(resultado_pct, 2),
            })
            posicao    = 0.0
            em_posicao = False

        pat = posicao * preco if em_posicao else capital
        patrimonio.append(pat)
        datas.append(str(row.name.date()))

    # Fecha posição no final
    if em_posicao and len(df) > 0:
        preco_final = float(df['close'].iloc[-1])
        capital = posicao * preco_final
        resultado_pct = (preco_final - preco_entrada) / preco_entrada * 100
        operacoes.append({
            'tipo': 'VENDA', 'data': str(df.index[-1].date()),
            'preco': round(preco_final, 2),
            'quantidade': round(posicao, 4),
            'valor': round(capital, 2),
            'resultado_pct': round(resultado_pct, 2),
        })

    return operacoes, patrimonio, datas

def simular_janus_score(df, capital_inicial, score_compra=80, score_venda=60, janus_scores=None):
    """Compra quando Janus Score > score_compra, vende quando < score_venda."""
    # Se não tem histórico de scores, usa Buy and Hold como fallback
    if not janus_scores:
        print("[BT] Janus Score: sem histórico — usando Buy and Hold como base", flush=True)
        return simular_buy_and_hold(df, capital_inicial)

    capital    = capital_inicial
    posicao    = 0.0
    em_posicao = False
    preco_entrada = 0.0
    operacoes  = []
    patrimonio = []
    datas      = []

    for i, (data, row) in enumerate(df.iterrows()):
        data_str = str(data.date())
        preco    = float(row['close'])
        score    = janus_scores.get(data_str, None)

        if score is not None:
            if not em_posicao and score > score_compra:
                posicao       = capital / preco
                preco_entrada = preco
                em_posicao    = True
                operacoes.append({
                    'tipo': 'COMPRA', 'data': data_str,
                    'preco': round(preco, 2),
                    'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                    'score': score,
                })
            elif em_posicao and score < score_venda:
                capital = posicao * preco
                resultado_pct = (preco - preco_entrada) / preco_entrada * 100
                operacoes.append({
                    'tipo': 'VENDA', 'data': data_str,
                    'preco': round(preco, 2),
                    'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                    'resultado_pct': round(resultado_pct, 2),
                    'score': score,
                })
                posicao    = 0.0
                em_posicao = False

        pat = posicao * preco if em_posicao else capital
        patrimonio.append(pat)
        datas.append(data_str)

    return operacoes, patrimonio, datas

# ── BENCHMARKS ────────────────────────────────────────────────

def calcular_benchmark_ibov(df_ibov, capital_inicial, datas_estrategia):
    """Calcula curva de patrimônio do IBOVESPA no mesmo período."""
    if df_ibov is None or df_ibov.empty:
        return None
    preco_inicial = float(df_ibov['close'].iloc[0])
    patrimonio_ibov = []
    for data_str in datas_estrategia:
        try:
            dt = pd.to_datetime(data_str)
            if dt in df_ibov.index:
                preco = float(df_ibov.loc[dt, 'close'])
            else:
                # Usa o último preço disponível
                idx_prox = df_ibov.index.searchsorted(dt)
                if idx_prox >= len(df_ibov): idx_prox = len(df_ibov) - 1
                preco = float(df_ibov['close'].iloc[idx_prox])
            patrimonio_ibov.append(round(capital_inicial * preco / preco_inicial, 2))
        except:
            patrimonio_ibov.append(patrimonio_ibov[-1] if patrimonio_ibov else capital_inicial)
    return patrimonio_ibov

# ── RUNNER PRINCIPAL ──────────────────────────────────────────

def executar_backtest(params):
    """
    Executa um backtest completo.
    params = {
        'ticker': 'BBAS3',
        'estrategia': 'buy_hold' | 'medias_moveis' | 'rsi' | 'janus_score',
        'data_inicio': '2021-01-01',
        'data_fim': '2026-07-07',
        'capital_inicial': 10000,
        'parametros': {}  # parâmetros específicos da estratégia
    }
    """
    ticker        = params.get('ticker', '').upper()
    estrategia    = params.get('estrategia', 'buy_hold')
    data_inicio   = datetime.strptime(params.get('data_inicio', '2021-01-01'), '%Y-%m-%d').date()
    data_fim      = datetime.strptime(params.get('data_fim', str(date.today())), '%Y-%m-%d').date()
    capital_ini   = float(params.get('capital_inicial', 10000))
    extra         = params.get('parametros', {})

    print(f"[BT] Iniciando: {ticker} | {estrategia} | {data_inicio} → {data_fim} | R${capital_ini}", flush=True)

    # Carrega histórico do ativo
    df = carregar_historico(ticker, data_inicio, data_fim)
    if df is None or df.empty:
        return {'erro': f'Sem dados históricos para {ticker} no período selecionado'}

    print(f"[BT] Histórico carregado: {len(df)} dias", flush=True)

    # Executa estratégia
    if estrategia == 'buy_hold':
        operacoes, patrimonio, datas = simular_buy_and_hold(df, capital_ini)

    elif estrategia == 'medias_moveis':
        mm_r = int(extra.get('mm_rapida', 9))
        mm_l = int(extra.get('mm_lenta', 21))
        operacoes, patrimonio, datas = simular_medias_moveis(df, capital_ini, mm_r, mm_l)

    elif estrategia == 'rsi':
        rc = int(extra.get('rsi_compra', 30))
        rv = int(extra.get('rsi_venda', 70))
        operacoes, patrimonio, datas = simular_rsi(df, capital_ini, rc, rv)

    elif estrategia == 'janus_score':
        sc = int(extra.get('score_compra', 80))
        sv = int(extra.get('score_venda', 60))
        operacoes, patrimonio, datas = simular_janus_score(df, capital_ini, sc, sv)

    else:
        return {'erro': f'Estratégia desconhecida: {estrategia}'}

    if not patrimonio:
        return {'erro': 'Simulação não gerou dados — verifique o período'}

    # Métricas da estratégia
    metricas = calcular_metricas(patrimonio, operacoes, capital_ini)

    # Benchmarks
    df_ibov = carregar_ibovespa(data_inicio, data_fim)
    patrimonio_ibov = calcular_benchmark_ibov(df_ibov, capital_ini, datas)
    retorno_ibov = None
    if patrimonio_ibov and len(patrimonio_ibov) > 1:
        retorno_ibov = round((patrimonio_ibov[-1] - capital_ini) / capital_ini * 100, 2)

    retorno_cdi = round(calcular_cdi_periodo(data_inicio, data_fim), 2)

    # Alpha vs IBOVESPA
    alpha = None
    if retorno_ibov is not None:
        alpha = round(metricas.get('retorno_pct', 0) - retorno_ibov, 2)

    print(f"[BT] Concluído: retorno={metricas.get('retorno_pct')}% | ops={metricas.get('n_operacoes')}", flush=True)

    return {
        'ticker':         ticker,
        'estrategia':     estrategia,
        'data_inicio':    str(data_inicio),
        'data_fim':       str(data_fim),
        'metricas':       metricas,
        'benchmarks': {
            'ibovespa': {
                'retorno_pct':  retorno_ibov,
                'patrimonio':   patrimonio_ibov,
            },
            'cdi': {
                'retorno_pct':  retorno_cdi,
            },
            'alpha_ibov': alpha,
        },
        'curva_patrimonio': {
            'datas':      datas,
            'valores':    [round(v, 2) for v in patrimonio],
        },
        'operacoes':   operacoes,
        'n_dias':      len(df),
    }
