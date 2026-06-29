"""
Módulo de banco de dados — Supabase/PostgreSQL
Versão 3.0 — Multi-tenant com autenticação
"""
import os, json, psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
def agora_str(): return datetime.now(TZ_BRASILIA).isoformat()
def agora_utc(): return datetime.now(timezone.utc).isoformat()

def get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url: raise Exception("DATABASE_URL não configurada")
    return psycopg2.connect(url, sslmode="require")

def init_db():
    sql = """
    -- USUÁRIOS
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        nome TEXT,
        senha_hash TEXT NOT NULL,
        plano TEXT DEFAULT 'free',
        email_verificado BOOLEAN DEFAULT FALSE,
        codigo_verificacao TEXT,
        codigo_expira TEXT,
        token_reset TEXT,
        token_reset_expira TEXT,
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TEXT,
        ultimo_acesso TEXT
    );

    -- SESSÕES JWT
    CREATE TABLE IF NOT EXISTS sessoes (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        token TEXT UNIQUE NOT NULL,
        expira_em TEXT,
        criado_em TEXT
    );

    -- PLANOS (configurados pelo admin)
    CREATE TABLE IF NOT EXISTS planos (
        id SERIAL PRIMARY KEY,
        nome TEXT UNIQUE NOT NULL,
        preco_mensal NUMERIC DEFAULT 0,
        preco_anual NUMERIC DEFAULT 0,
        desconto_anual_pct INTEGER DEFAULT 0,
        max_alertas INTEGER DEFAULT -1,
        max_carteira INTEGER DEFAULT -1,
        ativo BOOLEAN DEFAULT TRUE,
        descricao TEXT,
        atualizado_em TEXT
    );

    -- CONFIGURAÇÕES DO SISTEMA (admin)
    CREATE TABLE IF NOT EXISTS config_sistema (
        chave TEXT PRIMARY KEY,
        valor TEXT,
        atualizado_em TEXT
    );

    -- ADMIN
    CREATE TABLE IF NOT EXISTS admins (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        senha_hash TEXT NOT NULL,
        criado_em TEXT
    );

    -- ALERTAS por usuário
    CREATE TABLE IF NOT EXISTS alertas (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        ticker TEXT NOT NULL,
        nome TEXT,
        cor TEXT,
        valor NUMERIC NOT NULL,
        direcao TEXT NOT NULL CHECK (direcao IN ('acima','abaixo')),
        criado_em TEXT,
        UNIQUE(usuario_id, ticker, direcao)
    );

    -- ALERTAS DISPARADOS por usuário
    CREATE TABLE IF NOT EXISTS alertas_disparados (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        ticker TEXT, nome TEXT, cor TEXT,
        valor NUMERIC, direcao TEXT,
        preco_no_disparo NUMERIC,
        disparado_em TEXT
    );

    -- CARTEIRA por usuário
    CREATE TABLE IF NOT EXISTS carteira (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        ticker TEXT NOT NULL,
        nome TEXT, cor TEXT, setor_id TEXT, setor_nome TEXT,
        preco_medio NUMERIC, quantidade NUMERIC,
        data_compra TEXT, corretora TEXT, adicionado_em TEXT,
        UNIQUE(usuario_id, ticker)
    );

    -- PUSH SUBSCRIPTIONS por usuário
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        subscription_json TEXT NOT NULL,
        criado_em TEXT
    );

    -- CACHE COTAÇÕES (compartilhado)
    CREATE TABLE IF NOT EXISTS cache_cotacoes (
        id INTEGER PRIMARY KEY DEFAULT 1,
        dados JSONB,
        atualizado_em TEXT
    );

    -- Inserir planos padrão se não existirem
    INSERT INTO planos (nome, preco_mensal, preco_anual, desconto_anual_pct, max_alertas, max_carteira, descricao, atualizado_em)
    VALUES
        ('free', 0, 0, 0, 5, 5, 'Plano gratuito', NOW()::TEXT),
        ('pro', 29.90, 299.00, 17, -1, -1, 'Plano Profissional - recursos ilimitados', NOW()::TEXT)
    ON CONFLICT (nome) DO NOTHING;

    -- Config padrão
    INSERT INTO config_sistema (chave, valor, atualizado_em)
    VALUES
        ('fonte_1_nome', 'Infomoney', NOW()::TEXT),
        ('fonte_1_url', 'https://www.infomoney.com.br/tudo-sobre/{ticker}/feed/', NOW()::TEXT),
        ('fonte_2_nome', 'Valor Econômico', NOW()::TEXT),
        ('fonte_2_url', 'https://valor.globo.com/financas/rss20.xml', NOW()::TEXT),
        ('fonte_3_nome', 'MoneyTimes', NOW()::TEXT),
        ('fonte_3_url', 'https://www.moneytimes.com.br/mercados/feed/', NOW()::TEXT)
    ON CONFLICT (chave) DO NOTHING;
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur: cur.execute(sql)
        conn.commit(); conn.close()
        print("[DB] Tabelas v3.0 criadas/verificadas", flush=True)
        return True
    except Exception as e:
        print(f"[DB] Erro init: {e}", flush=True)
        return False

# ── USUÁRIOS ──────────────────────────────────────────────────
def db_criar_usuario(email, nome, senha_hash, codigo, expira):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO usuarios (email, nome, senha_hash, codigo_verificacao, codigo_expira, criado_em)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (email.lower(), nome, senha_hash, codigo, expira, agora_str()))
            uid = cur.fetchone()[0]
        conn.commit(); conn.close()
        return uid
    except Exception as e:
        print(f"[DB] Erro criar usuário: {e}", flush=True)
        return None

def db_buscar_usuario_email(email):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM usuarios WHERE email=%s", (email.lower(),))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[DB] Erro buscar usuário: {e}", flush=True)
        return None

def db_buscar_usuario_id(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM usuarios WHERE id=%s", (uid,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        return None

def db_verificar_email(email, codigo):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE usuarios SET email_verificado=TRUE, codigo_verificacao=NULL, codigo_expira=NULL
                WHERE email=%s AND codigo_verificacao=%s AND codigo_expira > %s
            """, (email.lower(), codigo, agora_str()))
            ok = cur.rowcount > 0
        conn.commit(); conn.close()
        return ok
    except Exception as e:
        print(f"[DB] Erro verificar email: {e}", flush=True)
        return False

def db_atualizar_ultimo_acesso(uid):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET ultimo_acesso=%s WHERE id=%s", (agora_str(), uid))
        conn.commit(); conn.close()
    except: pass

def db_salvar_reset_token(email, token, expira):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET token_reset=%s, token_reset_expira=%s WHERE email=%s",
                       (token, expira, email.lower()))
            ok = cur.rowcount > 0
        conn.commit(); conn.close()
        return ok
    except: return False

def db_reset_senha(token, nova_senha_hash):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE usuarios SET senha_hash=%s, token_reset=NULL, token_reset_expira=NULL
                WHERE token_reset=%s AND token_reset_expira > %s
            """, (nova_senha_hash, token, agora_str()))
            ok = cur.rowcount > 0
        conn.commit(); conn.close()
        return ok
    except: return False

def db_reenviar_codigo(email, codigo, expira):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET codigo_verificacao=%s, codigo_expira=%s WHERE email=%s",
                       (codigo, expira, email.lower()))
        conn.commit(); conn.close()
        return True
    except: return False

def db_listar_usuarios():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email, nome, plano, email_verificado, ativo, criado_em, ultimo_acesso FROM usuarios ORDER BY criado_em DESC")
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except: return []

def db_atualizar_plano_usuario(uid, plano):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET plano=%s WHERE id=%s", (plano, uid))
        conn.commit(); conn.close()
        return True
    except: return False

# ── ADMIN ──────────────────────────────────────────────────────
def db_buscar_admin(email):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM admins WHERE email=%s", (email.lower(),))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except: return None

def db_criar_admin(email, senha_hash):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admins (email, senha_hash, criado_em) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (email.lower(), senha_hash, agora_str()))
        conn.commit(); conn.close()
        return True
    except: return False

# ── PLANOS ────────────────────────────────────────────────────
def db_listar_planos():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM planos ORDER BY preco_mensal")
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for k in ['preco_mensal','preco_anual']:
                    if r.get(k): r[k] = float(r[k])
        conn.close()
        return rows
    except: return []

def db_salvar_plano(nome, preco_mensal, preco_anual, desconto, max_alertas, max_carteira, descricao):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO planos (nome, preco_mensal, preco_anual, desconto_anual_pct, max_alertas, max_carteira, descricao, atualizado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (nome) DO UPDATE SET preco_mensal=%s, preco_anual=%s, desconto_anual_pct=%s,
                max_alertas=%s, max_carteira=%s, descricao=%s, atualizado_em=%s
            """, (nome, preco_mensal, preco_anual, desconto, max_alertas, max_carteira, descricao, agora_str(),
                  preco_mensal, preco_anual, desconto, max_alertas, max_carteira, descricao, agora_str()))
        conn.commit(); conn.close()
        return True
    except: return False

# ── CONFIG SISTEMA ────────────────────────────────────────────
def db_get_config(chave, default=None):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM config_sistema WHERE chave=%s", (chave,))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except: return default

def db_set_config(chave, valor):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO config_sistema (chave, valor, atualizado_em) VALUES (%s,%s,%s)
                ON CONFLICT (chave) DO UPDATE SET valor=%s, atualizado_em=%s
            """, (chave, valor, agora_str(), valor, agora_str()))
        conn.commit(); conn.close()
        return True
    except: return False

def db_get_all_config():
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT chave, valor FROM config_sistema")
            rows = {r['chave']: r['valor'] for r in cur.fetchall()}
        conn.close()
        return rows
    except: return {}

# ── ALERTAS por usuário ────────────────────────────────────────
def db_listar_alertas(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alertas WHERE usuario_id=%s ORDER BY criado_em DESC", (uid,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('valor'): r['valor'] = float(r['valor'])
        conn.close()
        return rows
    except: return []

def db_salvar_alerta(uid, ticker, nome, cor, valor, direcao):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alertas (usuario_id, ticker, nome, cor, valor, direcao, criado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id, ticker, direcao) DO UPDATE SET nome=%s, cor=%s, valor=%s, criado_em=%s
            """, (uid, ticker, nome, cor, valor, direcao, agora_str(), nome, cor, valor, agora_str()))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar alerta: {e}", flush=True)
        return False

def db_remover_alerta(uid, ticker, direcao=None):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if direcao:
                cur.execute("DELETE FROM alertas WHERE usuario_id=%s AND ticker=%s AND direcao=%s", (uid, ticker, direcao))
            else:
                cur.execute("DELETE FROM alertas WHERE usuario_id=%s AND ticker=%s", (uid, ticker))
        conn.commit(); conn.close()
        return True
    except: return False

def db_listar_disparados(uid, limite=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alertas_disparados WHERE usuario_id=%s ORDER BY disparado_em DESC LIMIT %s", (uid, limite))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('valor'): r['valor'] = float(r['valor'])
                if r.get('preco_no_disparo'): r['preco_no_disparo'] = float(r['preco_no_disparo'])
        conn.close()
        return rows
    except: return []

def db_registrar_disparado(uid, alerta, preco_atual):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alertas_disparados (usuario_id, ticker, nome, cor, valor, direcao, preco_no_disparo, disparado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (uid, alerta['ticker'], alerta.get('nome'), alerta.get('cor'),
                  alerta['valor'], alerta['direcao'], preco_atual, agora_str()))
        conn.commit(); conn.close()
        return True
    except: return False

def db_limpar_disparados(uid):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alertas_disparados WHERE usuario_id=%s", (uid,))
        conn.commit(); conn.close()
        return True
    except: return False

def db_listar_todos_alertas():
    """Para verificação automática de alertas — retorna todos os usuários."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.*, u.id as uid FROM alertas a
                JOIN usuarios u ON u.id = a.usuario_id
                WHERE u.ativo = TRUE
            """)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('valor'): r['valor'] = float(r['valor'])
        conn.close()
        return rows
    except: return []

# ── CARTEIRA por usuário ───────────────────────────────────────
def db_listar_carteira(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM carteira WHERE usuario_id=%s ORDER BY ticker", (uid,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('preco_medio'): r['preco_medio'] = float(r['preco_medio'])
                if r.get('quantidade'): r['quantidade'] = float(r['quantidade'])
        conn.close()
        return rows
    except: return []

def db_salvar_posicao(uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira (usuario_id, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, adicionado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id, ticker) DO UPDATE SET
                nome=%s, cor=%s, setor_id=%s, setor_nome=%s, preco_medio=%s, quantidade=%s, data_compra=%s, corretora=%s, adicionado_em=%s
            """, (uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str(),
                  nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str()))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar posição: {e}", flush=True)
        return False

def db_remover_posicao(uid, ticker):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM carteira WHERE usuario_id=%s AND ticker=%s", (uid, ticker.upper()))
        conn.commit(); conn.close()
        return True
    except: return False

# ── PUSH por usuário ──────────────────────────────────────────
def db_listar_push(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT subscription_json FROM push_subscriptions WHERE usuario_id=%s", (uid,))
            return [json.loads(r['subscription_json']) for r in cur.fetchall()]
        conn.close()
    except: return []

def db_salvar_push(uid, sub_json):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO push_subscriptions (usuario_id, subscription_json, criado_em)
                SELECT %s, %s, %s WHERE NOT EXISTS (
                    SELECT 1 FROM push_subscriptions WHERE usuario_id=%s AND subscription_json=%s)
            """, (uid, sub_json, agora_str(), uid, sub_json))
        conn.commit(); conn.close()
        return True
    except: return False

def db_remover_push(uid, sub_json):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_subscriptions WHERE usuario_id=%s AND subscription_json=%s", (uid, sub_json))
        conn.commit(); conn.close()
        return True
    except: return False

# ── CACHE COTAÇÕES ────────────────────────────────────────────
def db_salvar_cache(dados):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cache_cotacoes (id, dados, atualizado_em) VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET dados=%s, atualizado_em=%s
            """, (json.dumps(dados, ensure_ascii=False), agora_str(),
                  json.dumps(dados, ensure_ascii=False), agora_str()))
        conn.commit(); conn.close()
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
            if isinstance(dados, str): dados = json.loads(dados)
            print(f"[DB] Cache restaurado: {row['atualizado_em']}", flush=True)
            return dados
    except Exception as e:
        print(f"[DB] Erro carregar cache: {e}", flush=True)
    return None
