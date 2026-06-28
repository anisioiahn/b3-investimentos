"""
Módulo de banco de dados — Supabase/PostgreSQL
Guarda alertas, carteira e cache de cotações de forma permanente.
"""
import os, json, psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))

def agora_str():
    return datetime.now(TZ_BRASILIA).isoformat()

def get_conn():
    """Retorna conexão com o banco. Usa DATABASE_URL do ambiente."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

def init_db():
    """Cria as tabelas se não existirem."""
    sql = """
    CREATE TABLE IF NOT EXISTS alertas (
        id SERIAL PRIMARY KEY,
        ticker TEXT NOT NULL,
        nome TEXT,
        cor TEXT,
        valor NUMERIC NOT NULL,
        direcao TEXT NOT NULL CHECK (direcao IN ('acima','abaixo')),
        criado_em TEXT,
        UNIQUE(ticker, direcao)
    );

    CREATE TABLE IF NOT EXISTS alertas_disparados (
        id SERIAL PRIMARY KEY,
        ticker TEXT,
        nome TEXT,
        cor TEXT,
        valor NUMERIC,
        direcao TEXT,
        preco_no_disparo NUMERIC,
        disparado_em TEXT
    );

    CREATE TABLE IF NOT EXISTS carteira (
        id SERIAL PRIMARY KEY,
        ticker TEXT UNIQUE NOT NULL,
        nome TEXT,
        cor TEXT,
        setor_id TEXT,
        setor_nome TEXT,
        preco_medio NUMERIC,
        quantidade NUMERIC,
        data_compra TEXT,
        corretora TEXT,
        adicionado_em TEXT
    );

    CREATE TABLE IF NOT EXISTS cache_cotacoes (
        id INTEGER PRIMARY KEY DEFAULT 1,
        dados JSONB,
        atualizado_em TEXT
    );
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        conn.close()
        print("[DB] Tabelas criadas/verificadas com sucesso", flush=True)
        return True
    except Exception as e:
        print(f"[DB] Erro ao inicializar: {e}", flush=True)
        return False

# ── ALERTAS ──────────────────────────────────────────────────

def db_listar_alertas():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alertas ORDER BY criado_em DESC")
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r['valor'] = float(r['valor'])
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar alertas: {e}", flush=True)
        return []

def db_salvar_alerta(ticker, nome, cor, valor, direcao):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alertas (ticker, nome, cor, valor, direcao, criado_em)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, direcao) DO UPDATE
                SET nome=%s, cor=%s, valor=%s, criado_em=%s
            """, (ticker, nome, cor, valor, direcao, agora_str(),
                  nome, cor, valor, agora_str()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar alerta: {e}", flush=True)
        return False

def db_remover_alerta(ticker, direcao=None):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if direcao:
                cur.execute("DELETE FROM alertas WHERE ticker=%s AND direcao=%s", (ticker, direcao))
            else:
                cur.execute("DELETE FROM alertas WHERE ticker=%s", (ticker,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro remover alerta: {e}", flush=True)
        return False

def db_listar_disparados(limite=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alertas_disparados ORDER BY disparado_em DESC LIMIT %s", (limite,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('valor'): r['valor'] = float(r['valor'])
                if r.get('preco_no_disparo'): r['preco_no_disparo'] = float(r['preco_no_disparo'])
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar disparados: {e}", flush=True)
        return []

def db_registrar_disparado(alerta, preco_atual):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alertas_disparados (ticker, nome, cor, valor, direcao, preco_no_disparo, disparado_em)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (alerta['ticker'], alerta.get('nome'), alerta.get('cor'),
                  alerta['valor'], alerta['direcao'], preco_atual, agora_str()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro registrar disparado: {e}", flush=True)
        return False

def db_limpar_disparados():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alertas_disparados")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro limpar disparados: {e}", flush=True)
        return False

# ── CARTEIRA ─────────────────────────────────────────────────

def db_listar_carteira():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM carteira ORDER BY ticker")
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('preco_medio'): r['preco_medio'] = float(r['preco_medio'])
                if r.get('quantidade'): r['quantidade'] = float(r['quantidade'])
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar carteira: {e}", flush=True)
        return []

def db_salvar_posicao(ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira (ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, adicionado_em)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker) DO UPDATE
                SET nome=%s, cor=%s, setor_id=%s, setor_nome=%s,
                    preco_medio=%s, quantidade=%s, data_compra=%s, corretora=%s, adicionado_em=%s
            """, (ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str(),
                  nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar posição: {e}", flush=True)
        return False

def db_remover_posicao(ticker):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM carteira WHERE ticker=%s", (ticker,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro remover posição: {e}", flush=True)
        return False

# ── CACHE COTAÇÕES ───────────────────────────────────────────

def db_salvar_cache(dados):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cache_cotacoes (id, dados, atualizado_em)
                VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET dados=%s, atualizado_em=%s
            """, (json.dumps(dados, ensure_ascii=False), agora_str(),
                  json.dumps(dados, ensure_ascii=False), agora_str()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar cache: {e}", flush=True)
        return False

def db_carregar_cache():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT dados, atualizado_em FROM cache_cotacoes WHERE id=1")
            row = cur.fetchone()
        conn.close()
        if row:
            dados = row['dados']
            if isinstance(dados, str):
                dados = json.loads(dados)
            print(f"[DB] Cache carregado: {row['atualizado_em']}", flush=True)
            return dados
    except Exception as e:
        print(f"[DB] Erro carregar cache: {e}", flush=True)
    return None

