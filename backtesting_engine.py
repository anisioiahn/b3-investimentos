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

    elif estrategia == 'personalizada':
        regras = params.get('regras', {})
        if not regras.get('compra') and not regras.get('venda'):
            return {'erro': 'Estratégia personalizada sem regras definidas'}
        operacoes, patrimonio, datas = simular_personalizada(df, capital_ini, regras)

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

    # Janus Score e Selos
    janus_score_est = calcular_janus_score_estrategia(metricas)
    selos           = calcular_selos(metricas, {
        'alpha_ibov': alpha,
        'ibovespa':   {'retorno_pct': retorno_ibov},
    }, janus_score_est)

    return {
        'ticker':         ticker,
        'estrategia':     estrategia,
        'data_inicio':    str(data_inicio),
        'data_fim':       str(data_fim),
        'metricas':       metricas,
        'janus_score':    janus_score_est,
        'selos':          selos,
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

# ── ESTRATÉGIA PERSONALIZADA ──────────────────────────────────

def avaliar_condicao(row, row_ant, indicadores, condicao):
    """
    Avalia uma condição individual.
    condicao = {
        'indicador': 'mm9' | 'mm21' | 'rsi' | 'macd' | 'preco' | 'janus_score',
        'operador':  'cruza_acima' | 'cruza_abaixo' | 'maior' | 'menor' | 'igual',
        'valor':     'mm21' | 'mm50' | 30 | 70 | ...
    }
    """
    ind  = condicao.get('indicador', '')
    op   = condicao.get('operador', '')
    val  = condicao.get('valor', 0)

    def get_val(nome, r):
        """Pega valor de um indicador no row."""
        mapa = {
            'preco': 'close', 'mm9': 'mm9', 'mm21': 'mm21',
            'mm50': 'mm50', 'mm200': 'mm200', 'rsi': 'rsi',
            'macd': 'macd', 'macd_signal': 'macd_signal',
            'bb_sup': 'bb_sup', 'bb_inf': 'bb_inf',
        }
        col = mapa.get(nome, nome)
        v = r.get(col)
        return float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None

    v_atual = get_val(ind, row)
    if v_atual is None: return False

    # Valor de comparação — pode ser número ou outro indicador
    if isinstance(val, str):
        v_comp = get_val(val, row)
        v_comp_ant = get_val(val, row_ant) if row_ant else None
    else:
        v_comp = float(val)
        v_comp_ant = float(val)

    if v_comp is None: return False

    v_ant = get_val(ind, row_ant) if row_ant else None

    # Operadores
    if op == 'maior':        return v_atual > v_comp
    if op == 'menor':        return v_atual < v_comp
    if op == 'igual':        return abs(v_atual - v_comp) < 0.01
    if op == 'maior_igual':  return v_atual >= v_comp
    if op == 'menor_igual':  return v_atual <= v_comp
    if op == 'cruza_acima':
        return v_ant is not None and v_comp_ant is not None and v_ant <= v_comp_ant and v_atual > v_comp
    if op == 'cruza_abaixo':
        return v_ant is not None and v_comp_ant is not None and v_ant >= v_comp_ant and v_atual < v_comp

    return False

def avaliar_grupo_condicoes(row, row_ant, indicadores, condicoes, operador_logico='E'):
    """Avalia um grupo de condições com AND ou OR."""
    if not condicoes: return False
    resultados = [avaliar_condicao(row, row_ant, indicadores, c) for c in condicoes]
    if operador_logico == 'OU':
        return any(resultados)
    return all(resultados)  # E (padrão)

def simular_personalizada(df, capital_inicial, regras):
    """
    Simula estratégia com regras personalizadas.
    regras = {
        'compra': {
            'condicoes': [...],
            'operador': 'E' | 'OU'
        },
        'venda': {
            'condicoes': [...],
            'operador': 'E' | 'OU'
        },
        'stop_loss': -10,    # opcional, % de perda máxima
        'stop_gain': 30,     # opcional, % de ganho alvo
    }
    """
    df = calcular_indicadores(df)
    df_dict = df.reset_index().to_dict('records')

    capital    = capital_inicial
    posicao    = 0.0
    em_posicao = False
    preco_entrada = 0.0
    data_entrada  = None
    operacoes  = []
    patrimonio = []
    datas      = []

    regras_compra = regras.get('compra', {})
    regras_venda  = regras.get('venda',  {})
    stop_loss     = regras.get('stop_loss')
    stop_gain     = regras.get('stop_gain')

    for i, row in enumerate(df_dict):
        row_ant = df_dict[i-1] if i > 0 else None
        preco   = float(row.get('close', 0))
        if not preco: continue
        data_str = str(row.get('data', ''))[:10]

        # Verifica stop loss/gain se em posição
        if em_posicao and preco_entrada > 0:
            var_pct = (preco - preco_entrada) / preco_entrada * 100
            if stop_loss and var_pct <= stop_loss:
                capital = posicao * preco
                operacoes.append({
                    'tipo': 'VENDA', 'data': data_str, 'motivo': 'STOP_LOSS',
                    'preco': round(preco, 2), 'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                    'resultado_pct': round(var_pct, 2),
                })
                posicao = 0.0; em_posicao = False
            elif stop_gain and var_pct >= stop_gain:
                capital = posicao * preco
                operacoes.append({
                    'tipo': 'VENDA', 'data': data_str, 'motivo': 'STOP_GAIN',
                    'preco': round(preco, 2), 'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                    'resultado_pct': round(var_pct, 2),
                })
                posicao = 0.0; em_posicao = False

        # Sinal de compra
        if not em_posicao:
            conds = regras_compra.get('condicoes', [])
            op_log = regras_compra.get('operador', 'E')
            if conds and avaliar_grupo_condicoes(row, row_ant, df, conds, op_log):
                posicao       = capital / preco
                preco_entrada = preco
                data_entrada  = data_str
                em_posicao    = True
                operacoes.append({
                    'tipo': 'COMPRA', 'data': data_str,
                    'preco': round(preco, 2), 'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                })

        # Sinal de venda
        elif em_posicao:
            conds = regras_venda.get('condicoes', [])
            op_log = regras_venda.get('operador', 'E')
            if conds and avaliar_grupo_condicoes(row, row_ant, df, conds, op_log):
                capital = posicao * preco
                var_pct = (preco - preco_entrada) / preco_entrada * 100
                operacoes.append({
                    'tipo': 'VENDA', 'data': data_str,
                    'preco': round(preco, 2), 'quantidade': round(posicao, 4),
                    'valor': round(capital, 2),
                    'resultado_pct': round(var_pct, 2),
                })
                posicao = 0.0; em_posicao = False

        pat = posicao * preco if em_posicao else capital
        patrimonio.append(pat)
        datas.append(data_str)

    # Fecha posição aberta no final
    if em_posicao and df_dict:
        preco_final = float(df_dict[-1].get('close', 0))
        if preco_final:
            capital = posicao * preco_final
            var_pct = (preco_final - preco_entrada) / preco_entrada * 100
            operacoes.append({
                'tipo': 'VENDA', 'data': datas[-1] if datas else '',
                'preco': round(preco_final, 2), 'quantidade': round(posicao, 4),
                'valor': round(capital, 2), 'resultado_pct': round(var_pct, 2),
            })

    return operacoes, patrimonio, datas

# ── MÚLTIPLOS ATIVOS ──────────────────────────────────────────

def executar_backtest_multiplos(params):
    """
    Executa backtest em múltiplos ativos com alocação configurável.
    params = {
        'tickers': ['BBAS3', 'VALE3', 'PETR4'],
        'alocacao': {'BBAS3': 33, 'VALE3': 33, 'PETR4': 34},  # % por ativo
        'estrategia': 'buy_hold' | 'medias_moveis' | 'rsi' | 'personalizada',
        'data_inicio': '2021-01-01',
        'data_fim': '2026-07-07',
        'capital_inicial': 10000,
        'parametros': {},
        'regras': {},  # para estratégia personalizada
    }
    """
    tickers     = params.get('tickers', [])
    alocacao    = params.get('alocacao', {})
    estrategia  = params.get('estrategia', 'buy_hold')
    data_inicio = datetime.strptime(params.get('data_inicio', '2021-01-01'), '%Y-%m-%d').date()
    data_fim    = datetime.strptime(params.get('data_fim', str(date.today())), '%Y-%m-%d').date()
    capital_ini = float(params.get('capital_inicial', 10000))
    extra       = params.get('parametros', {})
    regras      = params.get('regras', {})

    if not tickers:
        return {'erro': 'Nenhum ativo selecionado'}

    # Alocação igualitária se não configurada
    if not alocacao:
        pct = 100 / len(tickers)
        alocacao = {t: pct for t in tickers}

    resultados_individuais = {}
    patrimonio_consolidado = None
    datas_ref = None
    total_operacoes = []

    for ticker in tickers:
        pct    = alocacao.get(ticker, 100 / len(tickers))
        cap_t  = capital_ini * pct / 100

        # Roda estratégia individual
        params_t = {**params, 'ticker': ticker, 'capital_inicial': cap_t}
        resultado = executar_backtest(params_t)

        if 'erro' in resultado:
            print(f"[BT] {ticker}: {resultado['erro']}", flush=True)
            continue

        resultados_individuais[ticker] = resultado

        # Consolida curva de patrimônio
        curva = resultado.get('curva_patrimonio', {})
        valores = curva.get('valores', [])
        datas   = curva.get('datas',   [])

        if patrimonio_consolidado is None:
            patrimonio_consolidado = valores.copy()
            datas_ref = datas
        else:
            # Soma alinhando por data
            for i, (d, v) in enumerate(zip(datas, valores)):
                if i < len(patrimonio_consolidado):
                    patrimonio_consolidado[i] += v
                else:
                    patrimonio_consolidado.append(v)

        # Adiciona operações com ticker identificado
        for op in resultado.get('operacoes', []):
            op['ticker'] = ticker
            total_operacoes.append(op)

    if not resultados_individuais:
        return {'erro': 'Nenhum ativo retornou dados para o período'}

    # Métricas consolidadas
    metricas = calcular_metricas(
        patrimonio_consolidado or [], total_operacoes, capital_ini
    )

    # Benchmark IBOVESPA
    df_ibov = carregar_ibovespa(data_inicio, data_fim)
    patrimonio_ibov = calcular_benchmark_ibov(df_ibov, capital_ini, datas_ref or [])
    retorno_ibov = None
    if patrimonio_ibov:
        retorno_ibov = round((patrimonio_ibov[-1] - capital_ini) / capital_ini * 100, 2)

    retorno_cdi = round(calcular_cdi_periodo(data_inicio, data_fim), 2)
    alpha = round(metricas.get('retorno_pct', 0) - retorno_ibov, 2) if retorno_ibov else None

    return {
        'tickers':     tickers,
        'estrategia':  estrategia,
        'data_inicio': str(data_inicio),
        'data_fim':    str(data_fim),
        'metricas':    metricas,
        'benchmarks': {
            'ibovespa': {'retorno_pct': retorno_ibov, 'patrimonio': patrimonio_ibov},
            'cdi':      {'retorno_pct': retorno_cdi},
            'alpha_ibov': alpha,
        },
        'curva_patrimonio': {
            'datas':   datas_ref or [],
            'valores': [round(v, 2) for v in (patrimonio_consolidado or [])],
        },
        'individuais': {
            t: {
                'retorno_pct': r.get('metricas', {}).get('retorno_pct'),
                'capital_final': r.get('metricas', {}).get('capital_final'),
                'n_operacoes': r.get('metricas', {}).get('n_operacoes'),
            }
            for t, r in resultados_individuais.items()
        },
        'operacoes': sorted(total_operacoes, key=lambda x: x.get('data', '')),
        'n_dias': len(datas_ref or []),
    }

# ── JANUS SCORE DE ESTRATÉGIAS ───────────────────────────────

def calcular_janus_score_estrategia(metricas, n_anos=None):
    """
    Calcula o Janus Score de uma estratégia (0-100).
    Baseado em múltiplos fatores de qualidade e consistência.
    """
    import math

    score = 0.0
    detalhes = {}

    # 1. RENTABILIDADE ANUALIZADA (0-20 pts)
    ret_anual = metricas.get('retorno_anualizado', 0) or 0
    pts_ret = min(20, max(0, ret_anual / 2))  # 40%aa = 20pts
    score += pts_ret
    detalhes['rentabilidade'] = round(pts_ret, 1)

    # 2. SHARPE RATIO (0-20 pts)
    sharpe = metricas.get('sharpe', 0) or 0
    pts_sharpe = min(20, max(0, sharpe * 10))  # Sharpe 2.0 = 20pts
    score += pts_sharpe
    detalhes['sharpe'] = round(pts_sharpe, 1)

    # 3. DRAWDOWN MÁXIMO (0-20 pts) — menor é melhor
    drawdown = abs(metricas.get('drawdown_max', -100) or -100)
    if drawdown <= 5:    pts_dd = 20
    elif drawdown <= 10: pts_dd = 16
    elif drawdown <= 15: pts_dd = 12
    elif drawdown <= 20: pts_dd = 8
    elif drawdown <= 30: pts_dd = 4
    else:                pts_dd = 0
    score += pts_dd
    detalhes['drawdown'] = round(pts_dd, 1)

    # 4. TAXA DE ACERTO (0-15 pts)
    acerto = metricas.get('taxa_acerto', 0) or 0
    pts_acerto = min(15, max(0, (acerto - 30) / 3))  # 75% = 15pts
    score += pts_acerto
    detalhes['taxa_acerto'] = round(pts_acerto, 1)

    # 5. PROFIT FACTOR (0-15 pts)
    pf = metricas.get('profit_factor', 0) or 0
    pts_pf = min(15, max(0, (pf - 1) * 7.5))  # PF=3 = 15pts
    score += pts_pf
    detalhes['profit_factor'] = round(pts_pf, 1)

    # 6. NÚMERO DE OPERAÇÕES (0-5 pts) — evita over/underfitting
    n_ops = metricas.get('n_operacoes', 0) or 0
    if n_ops >= 30:   pts_ops = 5
    elif n_ops >= 15: pts_ops = 3
    elif n_ops >= 5:  pts_ops = 1
    else:             pts_ops = 0
    score += pts_ops
    detalhes['operacoes'] = round(pts_ops, 1)

    # 7. SORTINO RATIO (0-5 pts)
    sortino = metricas.get('sortino', 0) or 0
    pts_sortino = min(5, max(0, sortino * 2.5))
    score += pts_sortino
    detalhes['sortino'] = round(pts_sortino, 1)

    score = max(0, min(100, round(score, 1)))

    # Classificação
    if score >= 85:   classe = 'Excepcional'
    elif score >= 70: classe = 'Excelente'
    elif score >= 55: classe = 'Bom'
    elif score >= 40: classe = 'Regular'
    else:             classe = 'Fraco'

    return {
        'score': score,
        'classe': classe,
        'detalhes': detalhes
    }

def calcular_selos(metricas, benchmarks, janus_score):
    """
    Calcula selos automáticos baseados nas métricas da estratégia.
    Retorna lista de selos conquistados.
    """
    selos = []
    score    = janus_score.get('score', 0)
    drawdown = abs(metricas.get('drawdown_max', -999) or -999)
    sharpe   = metricas.get('sharpe', 0) or 0
    ret_anual= metricas.get('retorno_anualizado', 0) or 0
    acerto   = metricas.get('taxa_acerto', 0) or 0
    pf       = metricas.get('profit_factor', 0) or 0
    n_ops    = metricas.get('n_operacoes', 0) or 0
    alpha    = benchmarks.get('alpha_ibov', 0) or 0

    # Selos de qualidade geral
    if score >= 85:
        selos.append({'id':'ouro',    'nome':'🥇 Ouro Janus',   'desc':'Estratégia excepcional em todos os critérios'})
    elif score >= 70:
        selos.append({'id':'prata',   'nome':'🥈 Prata Janus',  'desc':'Excelente relação risco/retorno'})
    elif score >= 55:
        selos.append({'id':'bronze',  'nome':'🥉 Bronze Janus', 'desc':'Boa estratégia com resultados consistentes'})

    # Selos técnicos específicos
    if drawdown <= 10:
        selos.append({'id':'baixo_dd', 'nome':'🛡️ Baixo Risco',  'desc':f'Drawdown máximo de apenas {drawdown:.1f}%'})

    if sharpe >= 1.5:
        selos.append({'id':'sharpe',  'nome':'📐 Alto Sharpe',   'desc':f'Sharpe Ratio de {sharpe:.2f}'})

    if acerto >= 65:
        selos.append({'id':'acerto',  'nome':'🎯 Alta Precisão', 'desc':f'{acerto:.0f}% de operações vencedoras'})

    if alpha >= 20:
        selos.append({'id':'alpha',   'nome':'🚀 Bate o IBOV',   'desc':f'+{alpha:.1f}% acima do Ibovespa'})

    if pf >= 2.5:
        selos.append({'id':'pf',      'nome':'💰 Alto Lucro',    'desc':f'Profit Factor de {pf:.1f}'})

    if ret_anual >= 25:
        selos.append({'id':'retorno', 'nome':'📈 Alto Retorno',  'desc':f'{ret_anual:.1f}% ao ano'})

    if n_ops >= 50:
        selos.append({'id':'robusto', 'nome':'⚙️ Robusto',       'desc':f'{n_ops} operações testadas'})

    return selos
