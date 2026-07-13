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
        status TEXT NOT NULL DEFAULT 'confirmada' CHECK (status IN ('confirmada','pendente')),
        origem TEXT DEFAULT 'manual',
        categoria_id INTEGER DEFAULT NULL,
        UNIQUE(usuario_id, ticker)
    );

    -- CATEGORIAS DE CARTEIRA por usuário
    CREATE TABLE IF NOT EXISTS carteira_categorias (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        cor TEXT DEFAULT '#0066cc',
        icone TEXT DEFAULT '📁',
        criado_em TEXT,
        UNIQUE(usuario_id, nome)
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
        ('free', 0, 0, 0, -1, -1, 'Plano gratuito', NOW()::TEXT),
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

        # Migration: remove limites do plano free
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE planos SET max_alertas=-1, max_carteira=-1
                    WHERE nome='free'
                """)
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[DB] Aviso migration plano free: {e}", flush=True)
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE carteira
                    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'confirmada'
                """)
                cur.execute("""
                    ALTER TABLE carteira
                    ADD COLUMN IF NOT EXISTS origem TEXT DEFAULT 'manual'
                """)
                cur.execute("""
                    ALTER TABLE carteira
                    ADD COLUMN IF NOT EXISTS categoria_id INTEGER DEFAULT NULL
                """)
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[DB] Aviso migration carteira: {e}", flush=True)

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
            cur.execute("SELECT * FROM alertas WHERE usuario_id=%s ORDER BY ticker ASC, direcao ASC", (uid,))
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

def db_ja_disparado_hoje(uid, ticker, direcao):
    """Verifica se já existe disparo para este ticker/direção hoje."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM alertas_disparados
                WHERE usuario_id=%s AND ticker=%s AND direcao=%s
                AND disparado_em::date = CURRENT_DATE
            """, (uid, ticker, direcao))
            return cur.fetchone()[0] > 0
    except: return False
    finally: conn.close()

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
    """Retorna TODAS as posições (confirmadas e pendentes). O front separa por 'status'."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT c.*, COALESCE(a.asset_type, 'ACAO') as asset_type
                FROM carteira c
                LEFT JOIN assets a ON a.ticker = c.ticker AND a.status = 'ATIVO'
                WHERE c.usuario_id = %s
                ORDER BY c.status, c.ticker
            """, (uid,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('preco_medio'): r['preco_medio'] = float(r['preco_medio'])
                if r.get('quantidade'): r['quantidade'] = float(r['quantidade'])
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar carteira: {e}", flush=True)
        return []

def db_salvar_posicao(uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, categoria_id=None):
    """Salva/atualiza posição como CONFIRMADA (fluxo manual normal)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira (usuario_id, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, adicionado_em, status, origem, categoria_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmada','manual',%s)
                ON CONFLICT (usuario_id, ticker) DO UPDATE SET
                nome=%s, cor=%s, setor_id=%s, setor_nome=%s, preco_medio=%s, quantidade=%s, data_compra=%s, corretora=%s, adicionado_em=%s, status='confirmada',
                categoria_id=COALESCE(%s, carteira.categoria_id)
            """, (uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str(), categoria_id,
                  nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, agora_str(), categoria_id))
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

# ── CARTEIRA PENDENTE (sugestões do Janus Index) ────────────────
def db_salvar_posicao_pendente(uid, ticker, nome, cor, setor_id, setor_nome,
                                 preco_medio, quantidade, data_compra, corretora, origem='janus_sugestao'):
    """Salva uma posição com status='pendente'. Não sobrescreve se o ticker já existir (confirmada ou pendente)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira (usuario_id, ticker, nome, cor, setor_id, setor_nome,
                    preco_medio, quantidade, data_compra, corretora, adicionado_em, status, origem)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pendente',%s)
                ON CONFLICT (usuario_id, ticker) DO NOTHING
            """, (uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade,
                  data_compra, corretora, agora_str(), origem))
            inserido = cur.rowcount > 0
        conn.commit(); conn.close()
        return inserido
    except Exception as e:
        print(f"[DB] Erro salvar posição pendente: {e}", flush=True)
        return False

def db_confirmar_posicao_pendente(uid, ticker, preco_medio, quantidade, data_compra, corretora):
    """Confirma uma posição pendente, transformando-a em confirmada (com valores possivelmente editados pelo usuário)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE carteira SET
                    preco_medio=%s, quantidade=%s, data_compra=%s, corretora=%s,
                    status='confirmada', adicionado_em=%s
                WHERE usuario_id=%s AND ticker=%s AND status='pendente'
            """, (preco_medio, quantidade, data_compra, corretora, agora_str(), uid, ticker))
            ok = cur.rowcount > 0
        conn.commit(); conn.close()
        return ok
    except Exception as e:
        print(f"[DB] Erro confirmar posição pendente: {e}", flush=True)
        return False

def db_descartar_posicao_pendente(uid, ticker):
    """Remove uma posição pendente sem confirmar (descarta a sugestão)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM carteira WHERE usuario_id=%s AND ticker=%s AND status='pendente'
            """, (uid, ticker))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro descartar posição pendente: {e}", flush=True)
        return False

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

def db_listar_todas_subscriptions():
    """Retorna todas as subscriptions de push de todos os usuários."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT subscription_json FROM push_subscriptions")
            subs = []
            for row in cur.fetchall():
                try: subs.append(json.loads(row[0]))
                except: pass
        conn.close()
        return subs
    except Exception as e:
        print(f"[DB] Erro listar subscriptions: {e}", flush=True)
        return []

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

# ── CATEGORIAS DE CARTEIRA ────────────────────────────────────
def db_listar_categorias(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT c.*, COUNT(p.id) as total_ativos
                FROM carteira_categorias c
                LEFT JOIN carteira p ON p.categoria_id = c.id AND p.usuario_id = %s AND p.status = 'confirmada'
                WHERE c.usuario_id = %s
                GROUP BY c.id
                ORDER BY c.nome
            """, (uid, uid))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro listar categorias: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_criar_categoria(uid, nome, cor='#0066cc', icone='📁'):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira_categorias (usuario_id, nome, cor, icone, criado_em)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (usuario_id, nome) DO NOTHING
                RETURNING id
            """, (uid, nome.strip(), cor, icone, agora_str()))
            row = cur.fetchone()
        conn.commit(); conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Erro criar categoria: {e}", flush=True)
        return None

def db_editar_categoria(uid, cat_id, nome, cor, icone):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE carteira_categorias SET nome=%s, cor=%s, icone=%s
                WHERE id=%s AND usuario_id=%s
            """, (nome.strip(), cor, icone, cat_id, uid))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro editar categoria: {e}", flush=True)
        return False

def db_excluir_categoria(uid, cat_id):
    """Remove categoria e move ativos para sem categoria (Geral)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Move ativos para sem categoria
            cur.execute("""
                UPDATE carteira SET categoria_id = NULL
                WHERE usuario_id=%s AND categoria_id=%s
            """, (uid, cat_id))
            cur.execute("""
                DELETE FROM carteira_categorias WHERE id=%s AND usuario_id=%s
            """, (cat_id, uid))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro excluir categoria: {e}", flush=True)
        return False

def db_mover_ativo_categoria(uid, ticker, categoria_id):
    """Move um ativo para uma categoria (ou None para Geral)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE carteira SET categoria_id=%s
                WHERE usuario_id=%s AND ticker=%s
            """, (categoria_id, uid, ticker.upper()))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro mover ativo: {e}", flush=True)
        return False

# ── DIVIDEND PROFILE ──────────────────────────────────────────
def db_init_dividend_tables(conn):
    """Cria tabelas de dividendos se não existirem."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dividend_profile (
                id SERIAL PRIMARY KEY,
                asset_id INTEGER REFERENCES assets(asset_id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                reference_date TEXT NOT NULL,
                -- Dados básicos
                last_dividend_date TEXT,
                last_dividend_value NUMERIC,
                dividend_yield_12m NUMERIC,
                dividend_yield_5y NUMERIC,
                trailing_annual_rate NUMERIC,
                payout_ratio NUMERIC,
                -- Dados calculados pelo Janus
                payments_per_year INTEGER,
                years_paying INTEGER,
                growing_dividends BOOLEAN,
                dividend_consistency NUMERIC,  -- % de períodos que pagou (0-100)
                average_yield NUMERIC,
                -- Janus Dividend Score
                janus_dividend_score NUMERIC,
                score_yield NUMERIC,
                score_growth NUMERIC,
                score_consistency NUMERIC,
                score_payout NUMERIC,
                score_coverage NUMERIC,
                -- Controle
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(asset_id, reference_date)
            );

            CREATE TABLE IF NOT EXISTS dividend_history (
                id SERIAL PRIMARY KEY,
                asset_id INTEGER REFERENCES assets(asset_id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                payment_date TEXT,
                ex_date TEXT,
                value NUMERIC,
                dividend_type TEXT DEFAULT 'DIVIDENDO',
                created_at TEXT,
                UNIQUE(asset_id, ex_date)
            );

            CREATE INDEX IF NOT EXISTS idx_dividend_profile_asset ON dividend_profile(asset_id);
            CREATE INDEX IF NOT EXISTS idx_dividend_history_asset ON dividend_history(asset_id);
            CREATE INDEX IF NOT EXISTS idx_dividend_history_date ON dividend_history(payment_date);
        """)
    conn.commit()

def db_salvar_dividend_profile(conn, asset_id, ticker, data):
    """Salva ou atualiza o perfil de dividendos de um ativo."""
    from datetime import datetime, timezone, timedelta
    ref = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO dividend_profile (
                asset_id, ticker, reference_date,
                last_dividend_date, last_dividend_value,
                dividend_yield_12m, dividend_yield_5y, trailing_annual_rate,
                payout_ratio, payments_per_year, years_paying,
                growing_dividends, dividend_consistency, average_yield,
                janus_dividend_score, score_yield, score_growth,
                score_consistency, score_payout, score_coverage,
                created_at, updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            ON CONFLICT (asset_id, reference_date) DO UPDATE SET
                last_dividend_date=EXCLUDED.last_dividend_date,
                last_dividend_value=EXCLUDED.last_dividend_value,
                dividend_yield_12m=EXCLUDED.dividend_yield_12m,
                dividend_yield_5y=EXCLUDED.dividend_yield_5y,
                trailing_annual_rate=EXCLUDED.trailing_annual_rate,
                payout_ratio=EXCLUDED.payout_ratio,
                payments_per_year=EXCLUDED.payments_per_year,
                years_paying=EXCLUDED.years_paying,
                growing_dividends=EXCLUDED.growing_dividends,
                dividend_consistency=EXCLUDED.dividend_consistency,
                average_yield=EXCLUDED.average_yield,
                janus_dividend_score=EXCLUDED.janus_dividend_score,
                score_yield=EXCLUDED.score_yield,
                score_growth=EXCLUDED.score_growth,
                score_consistency=EXCLUDED.score_consistency,
                score_payout=EXCLUDED.score_payout,
                score_coverage=EXCLUDED.score_coverage,
                updated_at=EXCLUDED.updated_at
        """, (
            asset_id, ticker, ref,
            data.get("last_dividend_date"), data.get("last_dividend_value"),
            data.get("dividend_yield_12m"), data.get("dividend_yield_5y"),
            data.get("trailing_annual_rate"), data.get("payout_ratio"),
            data.get("payments_per_year"), data.get("years_paying"),
            data.get("growing_dividends"), data.get("dividend_consistency"),
            data.get("average_yield"), data.get("janus_dividend_score"),
            data.get("score_yield"), data.get("score_growth"),
            data.get("score_consistency"), data.get("score_payout"),
            data.get("score_coverage"), now, now
        ))
    conn.commit()

def db_salvar_dividend_history(conn, asset_id, ticker, pagamentos):
    """Salva histórico de pagamentos de dividendos."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    with conn.cursor() as cur:
        for pag in pagamentos:
            try:
                cur.execute("""
                    INSERT INTO dividend_history
                        (asset_id, ticker, payment_date, ex_date, value, dividend_type, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (asset_id, ex_date) DO UPDATE SET
                        value=EXCLUDED.value, payment_date=EXCLUDED.payment_date
                """, (
                    asset_id, ticker,
                    pag.get("paymentDate"), pag.get("exDate") or pag.get("date"),
                    pag.get("rate") or pag.get("value") or pag.get("amount"),
                    pag.get("type", "DIVIDENDO"), now
                ))
            except Exception:
                pass
    conn.commit()

def db_listar_dividend_ranking(limit=50):
    """Retorna ranking de ativos por Janus Dividend Score."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT dp.*, a.ticker, c.trading_name, c.sector
                FROM dividend_profile dp
                JOIN assets a ON a.asset_id = dp.asset_id
                LEFT JOIN companies c ON c.company_id = a.company_id
                WHERE dp.janus_dividend_score IS NOT NULL
                AND dp.reference_date = (
                    SELECT MAX(reference_date) FROM dividend_profile dp2
                    WHERE dp2.asset_id = dp.asset_id
                )
                ORDER BY dp.janus_dividend_score DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro dividend ranking: {e}", flush=True)
        return []
    finally:
        conn.close()

# ── CARTEIRA SNAPSHOT (histórico diário de performance) ───────
def db_init_snapshot_tables(conn):
    """Cria tabela de snapshots se não existir."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS carteira_snapshot (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                categoria_id INTEGER DEFAULT NULL,  -- NULL = total geral
                data TEXT NOT NULL,                 -- YYYY-MM-DD
                valor_investido NUMERIC,
                valor_atual NUMERIC,
                lucro NUMERIC,
                lucro_pct NUMERIC,
                total_ativos INTEGER,
                created_at TEXT,
                UNIQUE(usuario_id, categoria_id, data)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshot_usuario ON carteira_snapshot(usuario_id, data);
        """)
    conn.commit()

def db_salvar_snapshot(uid, categoria_id, data, valor_investido, valor_atual, total_ativos):
    """Salva ou atualiza snapshot diário."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    lucro = round(valor_atual - valor_investido, 2) if valor_atual else 0
    lucro_pct = round(lucro / valor_investido * 100, 4) if valor_investido else 0
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira_snapshot
                    (usuario_id, categoria_id, data, valor_investido, valor_atual,
                     lucro, lucro_pct, total_ativos, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id, categoria_id, data) DO UPDATE SET
                    valor_investido=EXCLUDED.valor_investido,
                    valor_atual=EXCLUDED.valor_atual,
                    lucro=EXCLUDED.lucro,
                    lucro_pct=EXCLUDED.lucro_pct,
                    total_ativos=EXCLUDED.total_ativos,
                    created_at=EXCLUDED.created_at
            """, (uid, categoria_id, data, valor_investido, valor_atual,
                  lucro, lucro_pct, total_ativos, now))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SNAPSHOT] Erro salvar: {e}", flush=True)

def db_listar_snapshots(uid, categoria_id=None, dias=90):
    """Retorna histórico de snapshots para gráfico."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if categoria_id is None:
                # Total geral (categoria_id IS NULL)
                cur.execute("""
                    SELECT data, valor_investido, valor_atual, lucro, lucro_pct, total_ativos
                    FROM carteira_snapshot
                    WHERE usuario_id=%s AND categoria_id IS NULL
                    ORDER BY data ASC
                    LIMIT %s
                """, (uid, dias))
            else:
                cur.execute("""
                    SELECT data, valor_investido, valor_atual, lucro, lucro_pct, total_ativos
                    FROM carteira_snapshot
                    WHERE usuario_id=%s AND categoria_id=%s
                    ORDER BY data ASC
                    LIMIT %s
                """, (uid, categoria_id, dias))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for k in ['valor_investido','valor_atual','lucro','lucro_pct']:
                    if r.get(k): r[k] = float(r[k])
        conn.close()
        return rows
    except Exception as e:
        print(f"[SNAPSHOT] Erro listar: {e}", flush=True)
        return []

def db_listar_todos_usuarios():
    """Retorna todos os usuários para o cron de snapshot."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM usuarios WHERE ativo=TRUE OR ativo IS NULL")
            rows = [r['id'] for r in cur.fetchall()]
        conn.close()
        return rows
    except: return []

# ── AGENDA DO MERCADO ─────────────────────────────────────────
def db_init_agenda_tables(conn):
    """Cria tabela de agenda se não existir."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agenda_mercado (
                id SERIAL PRIMARY KEY,
                ticker TEXT,
                tipo TEXT NOT NULL,  -- 'DIVIDENDO' | 'BALANCO' | 'OPCOES' | 'MACRO'
                titulo TEXT NOT NULL,
                descricao TEXT,
                data_evento DATE NOT NULL,
                impacto TEXT DEFAULT 'MEDIO',  -- 'ALTO' | 'MEDIO' | 'BAIXO'
                valor NUMERIC,  -- valor do dividendo se aplicável
                fonte TEXT DEFAULT 'AUTO',
                created_at TEXT,
                UNIQUE(ticker, tipo, data_evento)
            );
            CREATE INDEX IF NOT EXISTS idx_agenda_data ON agenda_mercado(data_evento);
        """)
    conn.commit()

def db_salvar_agenda_item(conn, ticker, tipo, titulo, data_evento, descricao='', impacto='MEDIO', valor=None, fonte='AUTO'):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agenda_mercado (ticker, tipo, titulo, descricao, data_evento, impacto, valor, fonte, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ticker, tipo, data_evento) DO UPDATE SET
                titulo=EXCLUDED.titulo, descricao=EXCLUDED.descricao,
                impacto=EXCLUDED.impacto, valor=EXCLUDED.valor
        """, (ticker, tipo, titulo, descricao, data_evento, impacto, valor, fonte, now))
    conn.commit()

def db_listar_agenda(dias_futuros=90):
    """Retorna eventos dos próximos N dias ordenados por data."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM agenda_mercado
                WHERE data_evento >= CURRENT_DATE
                  AND data_evento <= CURRENT_DATE + (%s * INTERVAL '1 day')
                ORDER BY data_evento ASC, impacto DESC
            """, (dias_futuros,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[AGENDA] Erro listar: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_listar_agenda_carteira(uid, dias_futuros=90):
    """Retorna eventos apenas dos ativos da carteira do usuário."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.* FROM agenda_mercado a
                INNER JOIN carteira c ON c.ticker = a.ticker
                  AND c.usuario_id = %s AND c.status = 'confirmada'
                WHERE a.data_evento >= CURRENT_DATE
                  AND a.data_evento <= CURRENT_DATE + (%s * INTERVAL '1 day')
                UNION
                SELECT * FROM agenda_mercado
                WHERE ticker IS NULL
                  AND data_evento >= CURRENT_DATE
                  AND data_evento <= CURRENT_DATE + (%s * INTERVAL '1 day')
                ORDER BY data_evento ASC
            """, (uid, dias_futuros, dias_futuros))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[AGENDA] Erro listar carteira: {e}", flush=True)
        return []
    finally:
        conn.close()

# ── ESTRATÉGIA DO USUÁRIO ─────────────────────────────────────
def db_init_estrategia_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuario_estrategia (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE UNIQUE,
                perfil TEXT DEFAULT 'Moderado',
                objetivo TEXT DEFAULT 'Equilíbrio',
                horizonte TEXT DEFAULT 'Longo Prazo',
                pct_acoes INTEGER DEFAULT 0,
                pct_fiis INTEGER DEFAULT 0,
                pct_etfs INTEGER DEFAULT 0,
                pct_exterior INTEGER DEFAULT 0,
                pct_caixa INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
    conn.commit()

def db_salvar_estrategia(uid, data):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO usuario_estrategia
                    (usuario_id, perfil, objetivo, horizonte,
                     pct_acoes, pct_fiis, pct_etfs, pct_exterior, pct_caixa, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id) DO UPDATE SET
                    perfil=EXCLUDED.perfil, objetivo=EXCLUDED.objetivo,
                    horizonte=EXCLUDED.horizonte, pct_acoes=EXCLUDED.pct_acoes,
                    pct_fiis=EXCLUDED.pct_fiis, pct_etfs=EXCLUDED.pct_etfs,
                    pct_exterior=EXCLUDED.pct_exterior, pct_caixa=EXCLUDED.pct_caixa,
                    updated_at=EXCLUDED.updated_at
            """, (uid, data.get('perfil'), data.get('objetivo'), data.get('horizonte'),
                  data.get('acoes',0), data.get('fiis',0), data.get('etfs',0),
                  data.get('exterior',0), data.get('caixa',0), now))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar estratégia: {e}", flush=True)
        return False

def db_buscar_estrategia(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM usuario_estrategia WHERE usuario_id=%s", (uid,))
            row = cur.fetchone()
        conn.close()
        if not row: return {}
        return {
            'perfil': row['perfil'], 'objetivo': row['objetivo'],
            'horizonte': row['horizonte'], 'acoes': row['pct_acoes'],
            'fiis': row['pct_fiis'], 'etfs': row['pct_etfs'],
            'exterior': row['pct_exterior'], 'caixa': row['pct_caixa']
        }
    except Exception as e:
        print(f"[DB] Erro buscar estratégia: {e}", flush=True)
        return {}

# ── HISTÓRICO DE PREÇOS ───────────────────────────────────────
def db_init_historico_table(conn):
    """Cria tabela de histórico de preços se não existir."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historico_precos (
                ticker TEXT NOT NULL,
                data DATE NOT NULL,
                open NUMERIC,
                high NUMERIC,
                low NUMERIC,
                close NUMERIC NOT NULL,
                volume BIGINT,
                intervalo TEXT NOT NULL DEFAULT '1d',
                fechamento BOOLEAN NOT NULL DEFAULT FALSE,
                variacao_pct NUMERIC,
                PRIMARY KEY (ticker, data, intervalo)
            );
            -- Migration: adiciona colunas se não existirem
            ALTER TABLE historico_precos ADD COLUMN IF NOT EXISTS fechamento BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE historico_precos ADD COLUMN IF NOT EXISTS variacao_pct NUMERIC;
            CREATE INDEX IF NOT EXISTS idx_hist_ticker_data
                ON historico_precos(ticker, data DESC);
            CREATE INDEX IF NOT EXISTS idx_hist_ticker_intervalo
                ON historico_precos(ticker, intervalo, data DESC);
        """)
    conn.commit()

def db_gravar_cotacoes_dia(cotacoes, fechamento=False):
    """
    Grava cotações do dia na tabela historico_precos.
    cotacoes = [{'ticker': 'BBAS3', 'preco': 20.35, 'variacao_pct': 1.75,
                 'open': 20.0, 'high': 20.5, 'low': 19.9, 'volume': 1234567}]
    fechamento=True quando chamado às 18h com preço de fechamento definitivo.
    """
    if not cotacoes: return 0
    from datetime import date
    hoje = date.today()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            args = [(
                c['ticker'], hoje, '1d',
                c.get('open'), c.get('high'), c.get('low'),
                c['preco'], c.get('volume'),
                fechamento, c.get('variacao_pct')
            ) for c in cotacoes if c.get('preco')]
            if not args:
                conn.close()
                return 0
            psycopg2.extras.execute_values(cur, """
                INSERT INTO historico_precos
                    (ticker, data, intervalo, open, high, low, close, volume, fechamento, variacao_pct)
                VALUES %s
                ON CONFLICT (ticker, data, intervalo) DO UPDATE SET
                    close        = EXCLUDED.close,
                    open         = COALESCE(EXCLUDED.open, historico_precos.open),
                    high         = COALESCE(EXCLUDED.high, historico_precos.high),
                    low          = COALESCE(EXCLUDED.low,  historico_precos.low),
                    volume       = COALESCE(EXCLUDED.volume, historico_precos.volume),
                    variacao_pct = EXCLUDED.variacao_pct,
                    fechamento   = EXCLUDED.fechamento
            """, args, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
        conn.commit()
        conn.close()
        return len(args)
    except Exception as e:
        print(f"[DB] Erro gravar cotações dia: {e}", flush=True)
        return 0

def db_buscar_cotacao_atual(ticker):
    """Busca cotação mais recente do banco (hoje ou último dia disponível)."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT close as preco, variacao_pct, fechamento, data
                FROM historico_precos
                WHERE ticker=%s AND intervalo='1d'
                ORDER BY data DESC LIMIT 1
            """, (ticker,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[DB] Erro buscar cotação {ticker}: {e}", flush=True)
        return None

def db_buscar_cotacoes_dia():
    """Retorna cotações de hoje (ou último dia disponível) para todos os ativos."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (ticker)
                    ticker, close as preco, variacao_pct, fechamento, data
                FROM historico_precos
                WHERE intervalo='1d'
                ORDER BY ticker, data DESC
            """)
            rows = {r['ticker']: dict(r) for r in cur.fetchall()}
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro buscar cotações dia: {e}", flush=True)
        return {}

def db_salvar_historico_lote(conn, ticker, registros, intervalo='1d'):
    """Salva lote de registros de histórico. registros = [{date, open, high, low, close, volume}]"""
    if not registros: return 0
    with conn.cursor() as cur:
        args = []
        for r in registros:
            try:
                from datetime import datetime
                dt = datetime.utcfromtimestamp(r['date']).date() if isinstance(r['date'], (int,float)) else r['date']
                args.append((
                    ticker, dt, intervalo,
                    r.get('open'), r.get('high'), r.get('low'),
                    r.get('close'), r.get('volume')
                ))
            except: continue
        if not args: return 0
        psycopg2.extras.execute_values(cur, """
            INSERT INTO historico_precos (ticker, data, intervalo, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (ticker, data, intervalo) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high,
                low=EXCLUDED.low, close=EXCLUDED.close,
                volume=EXCLUDED.volume
        """, args, template="(%s,%s,%s,%s,%s,%s,%s,%s)")
    conn.commit()
    return len(args)

def db_buscar_historico(ticker, intervalo='1d', limit=365):
    """Busca histórico local do banco — registros mais recentes."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT date, open, high, low, close, volume FROM (
                    SELECT
                        EXTRACT(EPOCH FROM data)::BIGINT as date,
                        open, high, low, close, volume
                    FROM historico_precos
                    WHERE ticker=%s AND intervalo=%s
                    ORDER BY data DESC
                    LIMIT %s
                ) sub
                ORDER BY date ASC
            """, (ticker, intervalo, limit))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    'date':   int(r['date']),
                    'open':   float(r['open'])   if r['open']   else None,
                    'high':   float(r['high'])   if r['high']   else None,
                    'low':    float(r['low'])    if r['low']    else None,
                    'close':  float(r['close'])  if r['close']  else None,
                    'volume': int(r['volume'])   if r['volume'] else None,
                })
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro buscar histórico {ticker}: {e}", flush=True)
        return []

def db_ultimo_historico(ticker, intervalo='1d'):
    """Retorna a data do registro mais recente."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(data) FROM historico_precos
                WHERE ticker=%s AND intervalo=%s
            """, (ticker, intervalo))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except: return None

def db_total_historico(ticker=None):
    """Conta total de registros no histórico."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if ticker:
                cur.execute("SELECT COUNT(*) FROM historico_precos WHERE ticker=%s", (ticker,))
            else:
                cur.execute("SELECT COUNT(*) FROM historico_precos")
            total = cur.fetchone()[0]
        conn.close()
        return total
    except: return 0

# ── BACKTESTING ───────────────────────────────────────────────
def db_init_backtesting_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtesting_resultados (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER,
                ticker TEXT,
                estrategia TEXT,
                parametros JSONB,
                data_inicio DATE,
                data_fim DATE,
                capital_inicial NUMERIC,
                capital_final NUMERIC,
                retorno_pct NUMERIC,
                retorno_ibov NUMERIC,
                retorno_cdi NUMERIC,
                alpha NUMERIC,
                drawdown_max NUMERIC,
                sharpe NUMERIC,
                n_operacoes INTEGER,
                resultado_json JSONB,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_usuario
                ON backtesting_resultados(usuario_id, created_at DESC);
        """)
    conn.commit()

def db_salvar_backtest(uid, resultado, parametros):
    from datetime import datetime, timezone, timedelta
    import json, math
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    m   = resultado.get('metricas', {})
    bm  = resultado.get('benchmarks', {})

    def safe(v):
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except: return None

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtesting_resultados
                    (usuario_id, ticker, estrategia, parametros,
                     data_inicio, data_fim, capital_inicial, capital_final,
                     retorno_pct, retorno_ibov, retorno_cdi, alpha,
                     drawdown_max, sharpe, n_operacoes,
                     resultado_json, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                uid,
                resultado.get('ticker') or ','.join(resultado.get('tickers',[])),
                resultado.get('estrategia'),
                json.dumps(parametros),
                resultado.get('data_inicio'), resultado.get('data_fim'),
                safe(m.get('capital_inicial')), safe(m.get('capital_final')),
                safe(m.get('retorno_pct')),
                safe(bm.get('ibovespa',{}).get('retorno_pct')),
                safe(bm.get('cdi',{}).get('retorno_pct')),
                safe(bm.get('alpha_ibov')),
                safe(m.get('drawdown_max')),
                safe(m.get('sharpe')),
                m.get('n_operacoes'),
                json.dumps(resultado, default=str),
                now
            ))
            row = cur.fetchone()
        conn.commit(); conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Erro salvar backtest: {e}", flush=True)
        return None

def db_listar_backtests(uid, limit=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                ALTER TABLE backtesting_resultados
                ADD COLUMN IF NOT EXISTS publicada BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS estrategia_id INTEGER
            """)
            conn.commit()
            cur.execute("""
                SELECT id, ticker, estrategia, data_inicio, data_fim,
                       capital_inicial, capital_final, retorno_pct,
                       retorno_ibov, alpha, drawdown_max, sharpe,
                       n_operacoes, created_at,
                       COALESCE(publicada, FALSE) as publica,
                       estrategia_id
                FROM backtesting_resultados
                WHERE usuario_id=%s
                ORDER BY created_at DESC LIMIT %s
            """, (uid, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro listar backtests: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_marcar_publicada(bt_id, usuario_id, estrategia_id, publicada=True):
    """Marca/desmarca uma simulação como publicada."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE backtesting_resultados
                SET publicada=%s, estrategia_id=%s
                WHERE id=%s AND usuario_id=%s
            """, (publicada, estrategia_id, bt_id, usuario_id))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB] Erro marcar publicada: {e}", flush=True)

# ── BACKTESTING v2 — ESTRATÉGIAS COMPARTILHADAS ───────────────
def db_init_backtesting_v2_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtesting_estrategias (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER,
                nome TEXT NOT NULL,
                descricao TEXT,
                tipo TEXT DEFAULT 'personalizada',
                regras JSONB NOT NULL,
                simulacao_params JSONB,
                publica BOOLEAN DEFAULT FALSE,
                usos INTEGER DEFAULT 0,
                retorno_medio NUMERIC,
                sharpe_medio NUMERIC,
                versao TEXT DEFAULT 'v1.0',
                versao_anterior_id INTEGER,
                notas_versao TEXT,
                fork_de INTEGER,
                fork_de_versao TEXT,
                fork_de_nome TEXT,
                forks INTEGER DEFAULT 0,
                ranking_score NUMERIC DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_est_publica
                ON backtesting_estrategias(publica, ranking_score DESC);
            CREATE INDEX IF NOT EXISTS idx_bt_est_usuario
                ON backtesting_estrategias(usuario_id);
        """)
        # Colunas para bancos já existentes
        for col in [
            "ADD COLUMN IF NOT EXISTS simulacao_params JSONB",
            "ADD COLUMN IF NOT EXISTS versao TEXT DEFAULT 'v1.0'",
            "ADD COLUMN IF NOT EXISTS versao_anterior_id INTEGER",
            "ADD COLUMN IF NOT EXISTS notas_versao TEXT",
            "ADD COLUMN IF NOT EXISTS fork_de INTEGER",
            "ADD COLUMN IF NOT EXISTS fork_de_versao TEXT",
            "ADD COLUMN IF NOT EXISTS fork_de_nome TEXT",
            "ADD COLUMN IF NOT EXISTS forks INTEGER DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS ranking_score NUMERIC DEFAULT 0",
        ]:
            try:
                cur.execute(f"ALTER TABLE backtesting_estrategias {col}")
                conn.commit()
            except Exception as e:
                conn.rollback()
    conn.commit()

def db_salvar_estrategia_bt(uid, nome, descricao, tipo, regras, publica=False,
                            retorno_medio=None, sharpe_medio=None,
                            simulacao_params=None,
                            fork_de=None, fork_de_nome=None, fork_de_versao=None):
    import json
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtesting_estrategias
                    (usuario_id, nome, descricao, tipo, regras, simulacao_params,
                     publica, retorno_medio, sharpe_medio,
                     fork_de, fork_de_nome, fork_de_versao,
                     created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (uid, nome, descricao, tipo,
                  json.dumps(regras),
                  json.dumps(simulacao_params) if simulacao_params else None,
                  publica, retorno_medio, sharpe_medio,
                  fork_de, fork_de_nome, fork_de_versao,
                  now, now))
            row = cur.fetchone()
        conn.commit(); conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Erro salvar estratégia BT: {e}", flush=True)
        return None

def db_incrementar_uso_estrategia(estrategia_id, retorno_pct, sharpe):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE backtesting_estrategias SET
                    usos = usos + 1,
                    retorno_medio = COALESCE((retorno_medio * usos + %s) / (usos + 1), %s),
                    sharpe_medio  = COALESCE((sharpe_medio  * usos + %s) / (usos + 1), %s)
                WHERE id = %s
            """, (retorno_pct, retorno_pct, sharpe, sharpe, estrategia_id))
        conn.commit(); conn.close()
    except: pass

# ── BACKTESTING — AVALIAÇÕES E COMENTÁRIOS ────────────────────
def db_init_backtesting_social_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtesting_avaliacoes (
                id SERIAL PRIMARY KEY,
                estrategia_id INTEGER,
                usuario_id INTEGER,
                estrelas INTEGER CHECK (estrelas BETWEEN 1 AND 5),
                created_at TEXT,
                UNIQUE(estrategia_id, usuario_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtesting_comentarios (
                id SERIAL PRIMARY KEY,
                estrategia_id INTEGER,
                usuario_id INTEGER,
                nome_usuario TEXT,
                comentario TEXT NOT NULL,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bt_aval_estrategia
                ON backtesting_avaliacoes(estrategia_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bt_coment_estrategia
                ON backtesting_comentarios(estrategia_id, created_at DESC)
        """)
    conn.commit()

def db_avaliar_estrategia(estrategia_id, usuario_id, estrelas):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtesting_avaliacoes (estrategia_id, usuario_id, estrelas, created_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (estrategia_id, usuario_id)
                DO UPDATE SET estrelas=%s, created_at=%s
            """, (estrategia_id, usuario_id, estrelas, now, estrelas, now))
            # Atualiza média na tabela de estratégias
            cur.execute("""
                UPDATE backtesting_estrategias SET
                    sharpe_medio = (
                        SELECT AVG(estrelas) FROM backtesting_avaliacoes
                        WHERE estrategia_id=%s
                    )
                WHERE id=%s
            """, (estrategia_id, estrategia_id))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro avaliar: {e}", flush=True)
        return False

def db_comentar_estrategia(estrategia_id, usuario_id, nome_usuario, comentario):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtesting_comentarios
                    (estrategia_id, usuario_id, nome_usuario, comentario, created_at)
                VALUES (%s,%s,%s,%s,%s) RETURNING id
            """, (estrategia_id, usuario_id, nome_usuario, comentario, now))
            row = cur.fetchone()
        conn.commit(); conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Erro comentar: {e}", flush=True)
        return None

def db_listar_comentarios(estrategia_id, limit=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nome_usuario, comentario, created_at
                FROM backtesting_comentarios
                WHERE estrategia_id=%s
                ORDER BY created_at DESC LIMIT %s
            """, (estrategia_id, limit))
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar comentários: {e}", flush=True)
        return []

def db_media_estrelas(estrategia_id, usuario_id=None):
    """Retorna média de estrelas e a avaliação do usuário se uid fornecido."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(estrelas)::NUMERIC(3,1), COUNT(*)
                FROM backtesting_avaliacoes WHERE estrategia_id=%s
            """, (estrategia_id,))
            row = cur.fetchone()
            media = float(row[0]) if row[0] else 0
            total = int(row[1]) if row[1] else 0
            minha = None
            if usuario_id:
                cur.execute("""
                    SELECT estrelas FROM backtesting_avaliacoes
                    WHERE estrategia_id=%s AND usuario_id=%s
                """, (estrategia_id, usuario_id))
                r = cur.fetchone()
                minha = r[0] if r else None
        conn.close()
        return {'media': media, 'total': total, 'minha_avaliacao': minha}
    except Exception as e:
        print(f"[DB] Erro média estrelas: {e}", flush=True)
        return {'media': 0, 'total': 0, 'minha_avaliacao': None}

def db_listar_estrategias_bt(uid=None, publicas=False, limit=20, ordem='ranking'):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if publicas:
                order = {
                    'ranking': 'e.usos DESC, e.retorno_medio DESC NULLS LAST',
                    'retorno': 'e.retorno_medio DESC NULLS LAST',
                    'sharpe':  'e.sharpe_medio DESC NULLS LAST',
                    'usos':    'e.usos DESC',
                    'recentes':'e.created_at DESC',
                }.get(ordem, 'e.usos DESC')
                cur.execute(f"""
                    SELECT e.*, COALESCE(u.nome, 'Usuário') as autor,
                        COALESCE(AVG(a.estrelas),0)::NUMERIC(3,1) as media_estrelas,
                        COUNT(DISTINCT a.id) as total_avaliacoes,
                        COUNT(DISTINCT c.id) as total_comentarios,
                        COUNT(DISTINCT f.id) as total_forks
                    FROM backtesting_estrategias e
                    LEFT JOIN usuarios u ON u.id = e.usuario_id
                    LEFT JOIN backtesting_avaliacoes a ON a.estrategia_id = e.id
                    LEFT JOIN backtesting_comentarios c ON c.estrategia_id = e.id
                    LEFT JOIN backtesting_estrategias f ON f.fork_de = e.id AND f.publica = TRUE
                    WHERE e.publica = TRUE
                    GROUP BY e.id, u.nome
                    ORDER BY {order}
                    LIMIT %s
                """, (limit,))
            else:
                cur.execute("""
                    SELECT * FROM backtesting_estrategias
                    WHERE usuario_id=%s ORDER BY created_at DESC LIMIT %s
                """, (uid, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro listar estratégias BT: {e}", flush=True)
        return []
    finally:
        conn.close()

# ── PRESENÇA / SESSÕES ATIVAS ─────────────────────────────────
def db_init_presenca_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios_presenca (
                usuario_id INTEGER PRIMARY KEY,
                ultimo_acesso TEXT NOT NULL,
                pagina TEXT,
                user_agent TEXT
            )
        """)
    conn.commit()

def db_registrar_presenca(usuario_id, pagina=None, user_agent=None):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO usuarios_presenca (usuario_id, ultimo_acesso, pagina, user_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (usuario_id) DO UPDATE SET
                    ultimo_acesso = %s, pagina = %s, user_agent = %s
            """, (usuario_id, now, pagina, user_agent,
                  now, pagina, user_agent))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB] Erro presença: {e}", flush=True)

def db_usuarios_online(minutos=5):
    """Retorna usuários que fizeram ping nos últimos N minutos."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.usuario_id, p.ultimo_acesso, p.pagina,
                       u.nome, u.email
                FROM usuarios_presenca p
                JOIN usuarios u ON u.id = p.usuario_id
                WHERE p.ultimo_acesso::timestamp with time zone >= NOW() - INTERVAL '%s minutes'
                ORDER BY p.ultimo_acesso DESC
            """ % minutos)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro online: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_historico_acessos_diario():
    """Retorna contagem de usuários únicos por dia nos últimos 30 dias."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DATE(ultimo_acesso::timestamp with time zone) as dia,
                       COUNT(DISTINCT usuario_id) as usuarios
                FROM usuarios_presenca
                WHERE ultimo_acesso::timestamp with time zone >= NOW() - INTERVAL '30 days'
                GROUP BY dia ORDER BY dia DESC
                LIMIT 30
            """)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r['dia'] = str(r['dia'])
                r['usuarios'] = int(r['usuarios'])
            return rows
    except Exception as e:
        print(f"[DB] Erro histórico acessos: {e}", flush=True)
        return []
    finally:
        conn.close()

# ── BACKTESTING v2.2 — FORK / VERSÃO / RANKING ───────────────

def db_fork_estrategia(uid, estrategia_id, nome, descricao):
    """Cria um fork de uma estratégia existente."""
    import json
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Busca original
            cur.execute("SELECT * FROM backtesting_estrategias WHERE id=%s", (estrategia_id,))
            orig = cur.fetchone()
            if not orig: return None
            orig = dict(orig)
            # Cria fork
            cur.execute("""
                INSERT INTO backtesting_estrategias
                    (usuario_id, nome, descricao, tipo, regras, simulacao_params,
                     publica, fork_de, fork_de_versao, fork_de_nome,
                     versao, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s,'v1.0',%s,%s)
                RETURNING id
            """, (uid, nome, descricao, orig['tipo'],
                  json.dumps(orig['regras']) if isinstance(orig['regras'], dict) else orig['regras'],
                  json.dumps(orig['simulacao_params']) if orig.get('simulacao_params') else None,
                  estrategia_id, orig.get('versao','v1.0'), orig['nome'],
                  now, now))
            novo_id = cur.fetchone()['id']
            # Incrementa forks na original
            cur.execute("UPDATE backtesting_estrategias SET forks=COALESCE(forks,0)+1 WHERE id=%s",
                       (estrategia_id,))
        conn.commit(); conn.close()
        return novo_id
    except Exception as e:
        print(f"[DB] Erro fork: {e}", flush=True)
        return None

def db_nova_versao_estrategia(uid, estrategia_id, regras, notas_versao, simulacao_params=None):
    """Cria nova versão de uma estratégia existente."""
    import json, re
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM backtesting_estrategias
                WHERE id=%s AND usuario_id=%s
            """, (estrategia_id, uid))
            orig = cur.fetchone()
            if not orig: return None
            orig = dict(orig)

            # Incrementa versão: v1.0 → v1.1, v1.9 → v2.0
            versao_atual = orig.get('versao','v1.0')
            match = re.match(r'v(\d+)\.(\d+)', versao_atual)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                nova_versao = f'v{major}.{minor+1}' if minor < 9 else f'v{major+1}.0'
            else:
                nova_versao = 'v1.1'

            # Cria nova versão
            cur.execute("""
                INSERT INTO backtesting_estrategias
                    (usuario_id, nome, descricao, tipo, regras, simulacao_params,
                     publica, versao, versao_anterior_id, notas_versao,
                     fork_de, fork_de_versao, fork_de_nome,
                     created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (uid, orig['nome'], orig['descricao'], orig['tipo'],
                  json.dumps(regras) if isinstance(regras, dict) else regras,
                  json.dumps(simulacao_params) if simulacao_params else orig.get('simulacao_params'),
                  orig['publica'], nova_versao, estrategia_id, notas_versao,
                  orig.get('fork_de'), orig.get('fork_de_versao'), orig.get('fork_de_nome'),
                  now, now))
            novo_id = cur.fetchone()['id']
        conn.commit(); conn.close()
        return {'id': novo_id, 'versao': nova_versao}
    except Exception as e:
        print(f"[DB] Erro nova versão: {e}", flush=True)
        return None

def db_listar_versoes(estrategia_id):
    """Lista todas as versões de uma estratégia."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Busca versão atual e todas as anteriores
            cur.execute("""
                WITH RECURSIVE versoes AS (
                    SELECT id, versao, notas_versao, created_at, versao_anterior_id,
                           retorno_medio, sharpe_medio
                    FROM backtesting_estrategias WHERE id=%s
                    UNION ALL
                    SELECT e.id, e.versao, e.notas_versao, e.created_at, e.versao_anterior_id,
                           e.retorno_medio, e.sharpe_medio
                    FROM backtesting_estrategias e
                    JOIN versoes v ON e.id = v.versao_anterior_id
                )
                SELECT * FROM versoes ORDER BY created_at DESC
            """, (estrategia_id,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro listar versões: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_calcular_ranking_score(estrategia_id):
    """Calcula e atualiza o ranking score híbrido (técnico + comunidade)."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT e.retorno_medio, e.sharpe_medio, e.usos, e.forks,
                       COALESCE(AVG(a.estrelas),0) as media_estrelas,
                       COUNT(DISTINCT a.id) as total_aval,
                       COUNT(DISTINCT c.id) as total_coment
                FROM backtesting_estrategias e
                LEFT JOIN backtesting_avaliacoes a ON a.estrategia_id = e.id
                LEFT JOIN backtesting_comentarios c ON c.estrategia_id = e.id
                WHERE e.id = %s
                GROUP BY e.id, e.retorno_medio, e.sharpe_medio, e.usos, e.forks
            """, (estrategia_id,))
            row = cur.fetchone()
            if not row: return 0

            # Score técnico (50%)
            retorno  = min(30, max(0, (row['retorno_medio'] or 0) / 5))
            sharpe   = min(20, max(0, (row['sharpe_medio']  or 0) * 10))
            score_tec = retorno + sharpe  # 0-50

            # Score comunidade (50%)
            estrelas = float(row['media_estrelas'] or 0)
            sc_aval  = (estrelas / 5) * 20   # 0-20
            sc_usos  = min(15, (row['usos'] or 0) / 100 * 15)  # 0-15
            sc_coment= min(10, (row['total_coment'] or 0) / 5 * 10)  # 0-10
            sc_forks = min(5,  (row['forks'] or 0) / 2 * 5)   # 0-5
            score_com = sc_aval + sc_usos + sc_coment + sc_forks  # 0-50

            ranking_score = round(score_tec + score_com, 1)

            cur.execute("""
                UPDATE backtesting_estrategias
                SET ranking_score=%s WHERE id=%s
            """, (ranking_score, estrategia_id))
        conn.commit(); conn.close()
        return ranking_score
    except Exception as e:
        print(f"[DB] Erro ranking: {e}", flush=True)
        return 0

# ── COCKPIT FASE 3 ────────────────────────────────────────────
def db_init_cockpit_fase3_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cockpit_ajia_cache (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL UNIQUE,
                resumo TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS cockpit_mudancas_dia (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                ticker TEXT NOT NULL,
                tipo TEXT NOT NULL,
                descricao TEXT NOT NULL,
                valor_anterior NUMERIC,
                valor_atual NUMERIC,
                icone TEXT DEFAULT '📊'
            );
            CREATE INDEX IF NOT EXISTS idx_mudancas_data
                ON cockpit_mudancas_dia(data DESC);
        """)
    conn.commit()

def db_salvar_ajia_cache(resumo):
    from datetime import date, datetime, timezone, timedelta
    hoje = date.today()
    now  = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cockpit_ajia_cache (data, resumo, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (data) DO UPDATE SET resumo=%s, created_at=%s
            """, (hoje, resumo, now, resumo, now))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB] Erro salvar AJIA cache: {e}", flush=True)

def db_buscar_ajia_cache():
    from datetime import date
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT resumo FROM cockpit_ajia_cache
                WHERE data = %s
            """, (date.today(),))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except: return None

def db_salvar_mudancas_dia(mudancas):
    """Salva lista de mudanças do dia."""
    from datetime import date
    hoje = date.today()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Remove mudanças antigas do dia
            cur.execute("DELETE FROM cockpit_mudancas_dia WHERE data=%s", (hoje,))
            if mudancas:
                import psycopg2.extras
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO cockpit_mudancas_dia
                        (data, ticker, tipo, descricao, valor_anterior, valor_atual, icone)
                    VALUES %s
                """, [(hoje, m['ticker'], m['tipo'], m['descricao'],
                       m.get('valor_anterior'), m.get('valor_atual'), m.get('icone','📊'))
                      for m in mudancas])
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB] Erro mudanças dia: {e}", flush=True)

def db_buscar_mudancas_dia():
    from datetime import date
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ticker, tipo, descricao, valor_anterior, valor_atual, icone
                FROM cockpit_mudancas_dia
                WHERE data = %s
                ORDER BY tipo, ticker
                LIMIT 20
            """, (date.today(),))
            return [dict(r) for r in cur.fetchall()]
    except: return []
    finally:
        conn.close()

# ── MÓDULO OPORTUNIDADES ──────────────────────────────────────

def db_termometro_setorial(periodo_anos=2):
    """
    Calcula retorno percentual acumulado de cada setor no período.
    Usa retorno individual de cada ativo (não preço absoluto médio)
    para evitar distorções por composição do setor.
    """
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH -- Preço inicial e final de cada ativo no período
                retornos_ativo AS (
                    SELECT
                        a.ticker,
                        c.sector as setor,
                        -- Preço no início do período (primeira data disponível após o corte)
                        FIRST_VALUE(h.close) OVER (
                            PARTITION BY a.ticker ORDER BY h.data ASC
                        ) as preco_inicio,
                        -- Preço atual (última data disponível)
                        LAST_VALUE(h.close) OVER (
                            PARTITION BY a.ticker ORDER BY h.data ASC
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        ) as preco_atual,
                        -- Pico e vale individuais
                        MAX(h.close) OVER (PARTITION BY a.ticker) as pico_ativo,
                        MIN(h.close) OVER (PARTITION BY a.ticker) as vale_ativo,
                        h.data
                    FROM historico_precos h
                    JOIN assets a ON a.ticker = h.ticker
                    JOIN companies c ON c.company_id = a.company_id
                    WHERE h.intervalo = '1d'
                      AND h.data >= CURRENT_DATE - INTERVAL '%s years'
                      AND c.sector IS NOT NULL
                      AND a.asset_type = 'ACAO'
                      AND a.status = 'ATIVO'
                ),
                -- Pega só uma linha por ativo (valores já calculados via window)
                resumo_ativo AS (
                    SELECT DISTINCT ON (ticker)
                        ticker, setor,
                        preco_inicio, preco_atual, pico_ativo, vale_ativo
                    FROM retornos_ativo
                    WHERE preco_inicio > 0
                ),
                -- Retorno percentual de cada ativo
                retorno_pct AS (
                    SELECT
                        setor,
                        ticker,
                        (preco_atual - preco_inicio) / preco_inicio * 100 as retorno,
                        (preco_atual - pico_ativo)   / pico_ativo   * 100 as dist_pico,
                        (preco_atual - vale_ativo)   / vale_ativo   * 100 as dist_vale
                    FROM resumo_ativo
                    WHERE preco_inicio > 0 AND preco_atual > 0
                )
                -- Média dos retornos por setor
                SELECT
                    setor,
                    COUNT(ticker) as n_ativos,
                    ROUND(AVG(retorno)::numeric, 1)   as retorno_periodo_pct,
                    ROUND(AVG(dist_pico)::numeric, 1) as dist_pico_pct,
                    ROUND(AVG(dist_vale)::numeric, 1) as dist_vale_pct
                FROM retorno_pct
                GROUP BY setor
                HAVING COUNT(ticker) >= 3
                ORDER BY dist_pico_pct ASC
            """ % periodo_anos)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro termômetro setorial: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_oportunidades_janus(limit=20):
    """
    Ativos com alto Janus Score + preço abaixo da média histórica + RSI baixo.
    Score de Oportunidade = Score Janus * (1 - posição_relativa) * (1 - RSI/100)
    """
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Última data de ranking
            cur.execute("SELECT MAX(reference_date) as dt FROM ranking_snapshots")
            ref = cur.fetchone()
            if not ref or not ref['dt']: return []
            ref_date = ref['dt']

            cur.execute("""
                WITH ultimos_precos AS (
                    SELECT ticker,
                        MAX(CASE WHEN data >= CURRENT_DATE - INTERVAL '1 year'  THEN close END) as max_1a,
                        MIN(CASE WHEN data >= CURRENT_DATE - INTERVAL '1 year'  THEN close END) as min_1a,
                        MAX(CASE WHEN data >= CURRENT_DATE - INTERVAL '2 years' THEN close END) as max_2a,
                        MIN(CASE WHEN data >= CURRENT_DATE - INTERVAL '2 years' THEN close END) as min_2a,
                        AVG(CASE WHEN data >= CURRENT_DATE - INTERVAL '1 year'  THEN close END) as media_1a,
                        AVG(CASE WHEN data >= CURRENT_DATE - INTERVAL '2 years' THEN close END) as media_2a,
                        (SELECT close FROM historico_precos h2
                         WHERE h2.ticker = h.ticker AND h2.intervalo='1d'
                         ORDER BY data DESC LIMIT 1) as preco_atual
                    FROM historico_precos h
                    WHERE intervalo = '1d'
                      AND data >= CURRENT_DATE - INTERVAL '2 years'
                    GROUP BY ticker
                    HAVING COUNT(*) >= 100
                ),
                rsi_calc AS (
                    SELECT ticker,
                        AVG(CASE WHEN diff > 0 THEN diff ELSE 0 END) as ganho_med,
                        AVG(CASE WHEN diff < 0 THEN ABS(diff) ELSE 0 END) as perda_med
                    FROM (
                        SELECT ticker,
                            close - LAG(close) OVER (PARTITION BY ticker ORDER BY data) as diff
                        FROM historico_precos
                        WHERE intervalo='1d'
                          AND data >= CURRENT_DATE - INTERVAL '30 days'
                    ) t
                    WHERE diff IS NOT NULL
                    GROUP BY ticker
                )
                SELECT
                    a.ticker,
                    c.trading_name as nome,
                    c.sector as setor,
                    a.asset_type,
                    r.janus_score as score,
                    up.preco_atual,
                    up.media_1a,
                    up.media_2a,
                    up.max_1a,
                    up.min_1a,
                    up.max_2a,
                    up.min_2a,
                    ROUND(((up.preco_atual - up.media_1a) / NULLIF(up.media_1a,0) * 100)::numeric, 1) as dist_media_1a,
                    ROUND(((up.preco_atual - up.media_2a) / NULLIF(up.media_2a,0) * 100)::numeric, 1) as dist_media_2a,
                    ROUND(((up.preco_atual - up.max_2a)  / NULLIF(up.max_2a,0)  * 100)::numeric, 1) as dist_pico_2a,
                    CASE
                        WHEN rc.perda_med = 0 THEN 100
                        ELSE ROUND((100 - (100 / (1 + rc.ganho_med / NULLIF(rc.perda_med,0))))::numeric, 1)
                    END as rsi,
                    ROUND((
                        r.janus_score * 0.5 +
                        LEAST(50, GREATEST(0, -((up.preco_atual - up.media_2a) / NULLIF(up.media_2a,0) * 100))) +
                        CASE
                            WHEN rc.perda_med = 0 THEN 0
                            ELSE LEAST(30, GREATEST(0, 30 - (100 - (100 / (1 + rc.ganho_med / NULLIF(rc.perda_med,0)))) * 0.3))
                        END
                    )::numeric / 1.3, 1) as score_oportunidade
                FROM ranking_snapshots r
                JOIN assets a ON a.asset_id = r.asset_id
                JOIN companies c ON c.company_id = a.company_id
                JOIN ultimos_precos up ON up.ticker = a.ticker
                LEFT JOIN rsi_calc rc ON rc.ticker = a.ticker
                WHERE r.reference_date = %s
                  AND r.janus_score >= 60
                  AND up.preco_atual IS NOT NULL
                  AND a.asset_type = 'ACAO'
                ORDER BY score_oportunidade DESC
                LIMIT %s
            """, (ref_date, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro oportunidades: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_top_dividendos_oportunidades(limit=20):
    """Top pagadores de dividendos com dados históricos."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    dp.ticker,
                    c.trading_name as nome,
                    c.sector as setor,
                    a.asset_type,
                    dp.dividend_yield_12m as dy_12m,
                    dp.dividend_yield_5y as dy_5y,
                    dp.janus_dividend_score as dividend_score,
                    dp.trailing_annual_rate,
                    r.janus_score as score
                FROM dividend_profile dp
                JOIN assets a ON a.ticker = dp.ticker
                JOIN companies c ON c.company_id = a.company_id
                LEFT JOIN ranking_snapshots r ON r.asset_id = a.asset_id
                    AND r.reference_date = (SELECT MAX(reference_date) FROM ranking_snapshots)
                WHERE dp.dividend_yield_12m > 0
                  AND a.status = 'ATIVO'
                ORDER BY dp.janus_dividend_score DESC NULLS LAST, dp.dividend_yield_12m DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro top dividendos: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_termometro_setor_detalhe(setor, periodo_anos=2):
    """
    Retorna os ativos de um setor com todas as variáveis do cálculo.
    """
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH retornos_ativo AS (
                    SELECT
                        a.ticker,
                        c.trading_name as nome,
                        FIRST_VALUE(h.close) OVER (
                            PARTITION BY a.ticker ORDER BY h.data ASC
                        ) as preco_inicio,
                        LAST_VALUE(h.close) OVER (
                            PARTITION BY a.ticker ORDER BY h.data ASC
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        ) as preco_atual,
                        MAX(h.close) OVER (PARTITION BY a.ticker) as pico_ativo,
                        MIN(h.close) OVER (PARTITION BY a.ticker) as vale_ativo,
                        MIN(h.data) OVER (PARTITION BY a.ticker) as data_inicio,
                        MAX(h.data) OVER (PARTITION BY a.ticker) as data_fim
                    FROM historico_precos h
                    JOIN assets a ON a.ticker = h.ticker
                    JOIN companies c ON c.company_id = a.company_id
                    WHERE h.intervalo = '1d'
                      AND h.data >= CURRENT_DATE - INTERVAL '%s years'
                      AND c.sector = %%s
                      AND a.asset_type = 'ACAO'
                      AND a.status = 'ATIVO'
                ),
                resumo AS (
                    SELECT DISTINCT ON (ticker)
                        ticker, nome, preco_inicio, preco_atual,
                        pico_ativo, vale_ativo, data_inicio, data_fim
                    FROM retornos_ativo
                    WHERE preco_inicio > 0
                )
                SELECT
                    ticker, nome,
                    ROUND(preco_inicio::numeric, 2)  as preco_inicio,
                    ROUND(preco_atual::numeric, 2)   as preco_atual,
                    ROUND(pico_ativo::numeric, 2)    as pico_ativo,
                    ROUND(vale_ativo::numeric, 2)    as vale_ativo,
                    data_inicio, data_fim,
                    ROUND(((preco_atual - preco_inicio) / preco_inicio * 100)::numeric, 1) as retorno_pct,
                    ROUND(((preco_atual - pico_ativo)   / pico_ativo   * 100)::numeric, 1) as dist_pico_pct,
                    ROUND(((preco_atual - vale_ativo)   / vale_ativo   * 100)::numeric, 1) as dist_vale_pct
                FROM resumo
                WHERE preco_inicio > 0 AND preco_atual > 0
                ORDER BY dist_pico_pct ASC
            """ % periodo_anos, (setor,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro detalhe setor: {e}", flush=True)
        return []
    finally:
        conn.close()

# ── RISK ENGINE ───────────────────────────────────────────────
def db_calcular_risk_scores(limit=500):
    """
    Calcula Score de Risco 0-100 para todos os ativos.
    Quanto MAIOR o score, MAIS arriscado é o ativo.
    Componentes:
      - Volatilidade 30d  (25%) — desvio padrão dos retornos diários
      - Beta               (20%) — sensibilidade ao mercado
      - Dívida/EBITDA      (20%) — risco de crédito
      - Drawdown máx 2a    (20%) — maior queda histórica
      - Liquidez inversa   (15%) — volume baixo = risco alto
    """
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(reference_date) as dt FROM ranking_snapshots")
            ref = cur.fetchone()
            if not ref or not ref['dt']: return []
            ref_date = ref['dt']

            cur.execute("""
                WITH
                -- 1. Volatilidade — desvio padrão dos retornos diários (30 dias)
                volatilidade AS (
                    SELECT ticker,
                        STDDEV(
                            (close - LAG(close) OVER (PARTITION BY ticker ORDER BY data))
                            / NULLIF(LAG(close) OVER (PARTITION BY ticker ORDER BY data), 0)
                        ) * 100 as vol_diaria
                    FROM historico_precos
                    WHERE intervalo = '1d'
                      AND data >= CURRENT_DATE - INTERVAL '30 days'
                    GROUP BY ticker
                    HAVING COUNT(*) >= 15
                ),
                -- 2. Drawdown máximo nos últimos 2 anos
                drawdown AS (
                    SELECT ticker,
                        MIN(
                            (close - MAX(close) OVER (PARTITION BY ticker ORDER BY data
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT_ROW))
                            / NULLIF(MAX(close) OVER (PARTITION BY ticker ORDER BY data
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT_ROW), 0)
                        ) * 100 as drawdown_max
                    FROM historico_precos
                    WHERE intervalo = '1d'
                      AND data >= CURRENT_DATE - INTERVAL '2 years'
                    GROUP BY ticker
                )
                SELECT
                    a.ticker,
                    c.trading_name as nome,
                    c.sector as setor,
                    -- Componentes brutos
                    COALESCE(v.vol_diaria, 3) as volatilidade,
                    COALESCE(ms.beta, 1) as beta,
                    COALESCE(fs.total_debt / NULLIF(fs.ebitda, 0), 5) as divida_ebitda,
                    COALESCE(ABS(d.drawdown_max), 30) as drawdown,
                    COALESCE(ms.volume, 0) as volume,
                    -- Score de Risco final (0-100, maior = mais arriscado)
                    ROUND(LEAST(100, GREATEST(0,
                        -- Volatilidade: 0%=0pts, 3%+=25pts (normaliza 0-25)
                        LEAST(25, COALESCE(v.vol_diaria, 3) / 3 * 25) * 0.25 / 0.25 +
                        -- Beta: 0=0pts, 2+=20pts
                        LEAST(20, GREATEST(0, COALESCE(ms.beta, 1)) / 2 * 20) +
                        -- Dívida/EBITDA: 0=0pts, 5x+=20pts
                        LEAST(20, GREATEST(0, COALESCE(fs.total_debt / NULLIF(fs.ebitda,0), 3)) / 5 * 20) +
                        -- Drawdown: 0=0pts, 60%+=20pts
                        LEAST(20, ABS(COALESCE(d.drawdown_max, 20)) / 60 * 20) +
                        -- Liquidez inversa: volume alto=0pts, volume zero=15pts
                        GREATEST(0, 15 - LEAST(15, COALESCE(ms.volume, 0) / 5000000 * 15))
                    ))::numeric, 1) as risk_score
                FROM ranking_snapshots r
                JOIN assets a ON a.asset_id = r.asset_id
                JOIN companies c ON c.company_id = a.company_id
                LEFT JOIN market_snapshots ms ON ms.asset_id = a.asset_id
                    AND ms.reference_date = r.reference_date
                LEFT JOIN financial_snapshots fs ON fs.asset_id = a.asset_id
                    AND fs.reference_date = r.reference_date
                LEFT JOIN volatilidade v ON v.ticker = a.ticker
                LEFT JOIN drawdown d ON d.ticker = a.ticker
                WHERE r.reference_date = %s
                  AND a.asset_type = 'ACAO'
                  AND a.status = 'ATIVO'
                ORDER BY risk_score ASC
                LIMIT %s
            """, (ref_date, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB] Erro risk scores: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_risk_score_ativo(ticker):
    """Retorna o score de risco de um ativo específico com detalhamento."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(reference_date) as dt FROM ranking_snapshots")
            ref = cur.fetchone()
            if not ref or not ref['dt']: return None
            ref_date = ref['dt']

            # Busca asset_id
            cur.execute("SELECT asset_id FROM assets WHERE ticker=%s LIMIT 1", (ticker,))
            asset = cur.fetchone()
            if not asset: return None
            asset_id = asset['asset_id']

            # Busca dados de mercado e financeiros
            cur.execute("""
                SELECT ms.beta, ms.volume,
                       fs.total_debt, fs.ebitda
                FROM assets a
                LEFT JOIN market_snapshots ms ON ms.asset_id = a.asset_id
                    AND ms.reference_date = %s
                LEFT JOIN financial_snapshots fs ON fs.asset_id = a.asset_id
                    AND fs.reference_date = %s
                WHERE a.asset_id = %s
                LIMIT 1
            """, (ref_date, ref_date, asset_id))
            dados = cur.fetchone()

            # Volatilidade 30 dias
            cur.execute("""
                SELECT STDDEV(
                    (close - LAG(close) OVER (ORDER BY data))
                    / NULLIF(LAG(close) OVER (ORDER BY data), 0)
                ) * 100 as vol_diaria
                FROM historico_precos
                WHERE ticker=%s AND intervalo='1d'
                  AND data >= CURRENT_DATE - INTERVAL '30 days'
            """, (ticker,))
            vol_row = cur.fetchone()

            # Drawdown máximo 2 anos
            cur.execute("""
                SELECT MIN(
                    (close - MAX(close) OVER (ORDER BY data
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT_ROW))
                    / NULLIF(MAX(close) OVER (ORDER BY data
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT_ROW), 0)
                ) * 100 as drawdown_max
                FROM historico_precos
                WHERE ticker=%s AND intervalo='1d'
                  AND data >= CURRENT_DATE - INTERVAL '2 years'
            """, (ticker,))
            dd_row = cur.fetchone()

        conn.close()

        d = dados or {}
        vol   = float(vol_row['vol_diaria'] or 3) if vol_row and vol_row['vol_diaria'] else 3.0
        beta  = float(d.get('beta') or 1)
        total_debt = float(d.get('total_debt') or 0)
        ebitda_val = float(d.get('ebitda') or 0)
        div_e = (total_debt / ebitda_val) if ebitda_val > 0 else 0
        dd    = abs(float(dd_row['drawdown_max'] or 0)) if dd_row and dd_row['drawdown_max'] else 20.0
        vol_m = float(d.get('volume') or 0)

        pts_vol   = min(25, vol / 3 * 25)
        pts_beta  = min(20, max(0, beta) / 2 * 20)
        pts_divid = min(20, max(0, div_e) / 5 * 20)
        pts_dd    = min(20, dd / 60 * 20)
        pts_liq   = max(0, 15 - min(15, vol_m / 5000000 * 15))

        risk_score = round(min(100, max(0,
            pts_vol + pts_beta + pts_divid + pts_dd + pts_liq)), 1)

        label = ('🟢 Muito Baixo' if risk_score <= 20 else
                 '🟢 Baixo'       if risk_score <= 40 else
                 '🟡 Moderado'    if risk_score <= 60 else
                 '🟠 Alto'        if risk_score <= 80 else
                 '🔴 Muito Alto')

        return {
            'risk_score': risk_score,
            'label': label,
            'detalhes': {
                'volatilidade':  {'valor': round(vol, 2),   'pts': round(pts_vol, 1),   'max': 25, 'unidade': '%/dia'},
                'beta':          {'valor': round(beta, 2),  'pts': round(pts_beta, 1),  'max': 20, 'unidade': 'x'},
                'divida_ebitda': {'valor': round(div_e, 2), 'pts': round(pts_divid, 1), 'max': 20, 'unidade': 'x'},
                'drawdown':      {'valor': round(dd, 1),    'pts': round(pts_dd, 1),    'max': 20, 'unidade': '%'},
                'liquidez':      {'valor': round(vol_m/1e6, 1), 'pts': round(pts_liq, 1), 'max': 15, 'unidade': 'M/dia'},
            }
        }
    except Exception as e:
        print(f"[DB] Erro risk ativo: {e}", flush=True)
        return None
    finally:
        try: conn.close()
        except: pass

# ── COMUNIDADE DE CARTEIRAS ───────────────────────────────────

def db_init_carteiras_comunidade(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS carteiras_publicas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                descricao TEXT,
                composicao JSONB NOT NULL,
                capital_total NUMERIC,
                retorno_12m NUMERIC,
                n_ativos INTEGER,
                publica BOOLEAN DEFAULT TRUE,
                clones INTEGER DEFAULT 0,
                ranking_score NUMERIC DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS carteiras_avaliacoes (
                id SERIAL PRIMARY KEY,
                carteira_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                estrelas INTEGER NOT NULL CHECK (estrelas BETWEEN 1 AND 5),
                created_at TEXT,
                UNIQUE(carteira_id, usuario_id)
            );
            CREATE TABLE IF NOT EXISTS carteiras_comentarios (
                id SERIAL PRIMARY KEY,
                carteira_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                comentario TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS carteiras_favoritos (
                carteira_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                created_at TEXT,
                PRIMARY KEY (carteira_id, usuario_id)
            );
            CREATE TABLE IF NOT EXISTS carteiras_seguidores (
                autor_id INTEGER NOT NULL,
                seguidor_id INTEGER NOT NULL,
                created_at TEXT,
                PRIMARY KEY (autor_id, seguidor_id)
            );
            CREATE INDEX IF NOT EXISTS idx_carteiras_publicas_usuario
                ON carteiras_publicas(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_carteiras_publicas_ranking
                ON carteiras_publicas(publica, ranking_score DESC);
        """)
    conn.commit()

def db_publicar_carteira(uid, nome, descricao, composicao, capital_total, retorno_12m):
    """Publica uma carteira na comunidade — sempre cria nova entrada independente."""
    import json
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteiras_publicas
                    (usuario_id, nome, descricao, composicao,
                     capital_total, retorno_12m, n_ativos,
                     publica, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
                RETURNING id
            """, (uid, nome, descricao, json.dumps(composicao),
                  capital_total, retorno_12m,
                  len(composicao), now, now))
            cid = cur.fetchone()[0]
        conn.commit(); conn.close()
        return cid
    except Exception as e:
        print(f"[DB] Erro publicar carteira: {e}", flush=True)
        return None

def db_listar_carteiras_publicas(uid_atual=None, ordem='ranking', limit=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            order_map = {
                'ranking':   'cp.ranking_score DESC',
                'avaliacao': 'media_estrelas DESC',
                'seguidores':'total_seguidores DESC',
                'clones':    'cp.clones DESC',
                'recentes':  'cp.updated_at DESC',
            }
            order_sql = order_map.get(ordem, 'cp.ranking_score DESC')
            cur.execute(f"""
                SELECT cp.*,
                    u.nome as autor,
                    COALESCE(AVG(ca.estrelas),0)::NUMERIC(3,1) as media_estrelas,
                    COUNT(DISTINCT ca.id) as total_avaliacoes,
                    COUNT(DISTINCT cc.id) as total_comentarios,
                    COUNT(DISTINCT cf.usuario_id) as total_favoritos,
                    COUNT(DISTINCT cs.seguidor_id) as total_seguidores
                FROM carteiras_publicas cp
                LEFT JOIN usuarios u ON u.id = cp.usuario_id
                LEFT JOIN carteiras_avaliacoes ca ON ca.carteira_id = cp.id
                LEFT JOIN carteiras_comentarios cc ON cc.carteira_id = cp.id
                LEFT JOIN carteiras_favoritos cf ON cf.carteira_id = cp.id
                LEFT JOIN carteiras_seguidores cs ON cs.autor_id = cp.usuario_id
                WHERE cp.publica = TRUE
                GROUP BY cp.id, u.nome
                ORDER BY {order_sql}
                LIMIT %s
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            # Marca minha carteira e favoritos do usuário atual
            if uid_atual:
                favs = set()
                seguindo = set()
                cur.execute("SELECT carteira_id FROM carteiras_favoritos WHERE usuario_id=%s", (uid_atual,))
                for r in cur.fetchall(): favs.add(r[0])
                cur.execute("SELECT autor_id FROM carteiras_seguidores WHERE seguidor_id=%s", (uid_atual,))
                for r in cur.fetchall(): seguindo.add(r[0])
                for row in rows:
                    row['minha']    = (row.get('usuario_id') == uid_atual)
                    row['favoritei']= (row['id'] in favs)
                    row['sigo']     = (row.get('usuario_id') in seguindo)
            return rows
    except Exception as e:
        print(f"[DB] Erro listar carteiras: {e}", flush=True)
        return []
    finally:
        conn.close()

def db_detalhe_carteira_publica(cid, uid_atual=None):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT cp.*, u.nome as autor,
                    COALESCE(AVG(ca.estrelas),0)::NUMERIC(3,1) as media_estrelas,
                    COUNT(DISTINCT ca.id) as total_avaliacoes,
                    COUNT(DISTINCT cc.id) as total_comentarios,
                    COUNT(DISTINCT cf.usuario_id) as total_favoritos,
                    COUNT(DISTINCT cs.seguidor_id) as total_seguidores
                FROM carteiras_publicas cp
                LEFT JOIN usuarios u ON u.id = cp.usuario_id
                LEFT JOIN carteiras_avaliacoes ca ON ca.carteira_id = cp.id
                LEFT JOIN carteiras_comentarios cc ON cc.carteira_id = cp.id
                LEFT JOIN carteiras_favoritos cf ON cf.carteira_id = cp.id
                LEFT JOIN carteiras_seguidores cs ON cs.autor_id = cp.usuario_id
                WHERE cp.id = %s AND cp.publica = TRUE
                GROUP BY cp.id, u.nome
            """, (cid,))
            cart = cur.fetchone()
            if not cart: return None
            cart = dict(cart)

            # Comentários
            cur.execute("""
                SELECT cc.comentario, cc.created_at, u.nome as autor
                FROM carteiras_comentarios cc
                JOIN usuarios u ON u.id = cc.usuario_id
                WHERE cc.carteira_id = %s
                ORDER BY cc.created_at DESC LIMIT 50
            """, (cid,))
            cart['comentarios'] = [dict(r) for r in cur.fetchall()]

            # Avaliação do usuário atual
            if uid_atual:
                cur.execute("""
                    SELECT estrelas FROM carteiras_avaliacoes
                    WHERE carteira_id=%s AND usuario_id=%s
                """, (cid, uid_atual))
                av = cur.fetchone()
                cart['minha_avaliacao'] = av[0] if av else None
                cur.execute("SELECT 1 FROM carteiras_favoritos WHERE carteira_id=%s AND usuario_id=%s", (cid, uid_atual))
                cart['favoritei'] = bool(cur.fetchone())
                cur.execute("SELECT 1 FROM carteiras_seguidores WHERE autor_id=%s AND seguidor_id=%s", (cart['usuario_id'], uid_atual))
                cart['sigo'] = bool(cur.fetchone())
                cart['minha'] = (cart['usuario_id'] == uid_atual)
        conn.close()
        return cart
    except Exception as e:
        print(f"[DB] Erro detalhe carteira: {e}", flush=True)
        return None
    finally:
        conn.close()

def db_calcular_ranking_carteira(cid):
    """Ranking score da carteira baseado em avaliações, seguidores e clones."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(AVG(ca.estrelas),0) as media_estrelas,
                    COUNT(DISTINCT ca.id) as total_aval,
                    COUNT(DISTINCT cs.seguidor_id) as seguidores,
                    COUNT(DISTINCT cc.id) as comentarios,
                    cp.clones
                FROM carteiras_publicas cp
                LEFT JOIN carteiras_avaliacoes ca ON ca.carteira_id = cp.id
                LEFT JOIN carteiras_seguidores cs ON cs.autor_id = cp.usuario_id
                LEFT JOIN carteiras_comentarios cc ON cc.carteira_id = cp.id
                WHERE cp.id = %s
                GROUP BY cp.id, cp.clones
            """, (cid,))
            r = cur.fetchone()
            if not r: return 0
            score = (
                float(r['media_estrelas']) / 5 * 40 +
                min(25, float(r['seguidores']) / 10 * 25) +
                min(20, float(r['clones']) / 10 * 20) +
                min(15, float(r['comentarios']) / 5 * 15)
            )
            score = round(score, 1)
            cur.execute("UPDATE carteiras_publicas SET ranking_score=%s WHERE id=%s", (score, cid))
        conn.commit(); conn.close()
        return score
    except Exception as e:
        print(f"[DB] Erro ranking carteira: {e}", flush=True)
        return 0

# ── WHATSAPP / TWILIO ─────────────────────────────────────────

def db_init_whatsapp(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_usuarios (
                usuario_id  INTEGER PRIMARY KEY,
                numero      TEXT NOT NULL,
                ativo       BOOLEAN DEFAULT FALSE,
                opt_in      BOOLEAN DEFAULT FALSE,
                criado_em   TEXT,
                updated_at  TEXT
            );
        """)
    conn.commit()

def db_salvar_whatsapp(uid, numero, ativo=True):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO whatsapp_usuarios (usuario_id, numero, ativo, opt_in, criado_em, updated_at)
                VALUES (%s, %s, %s, FALSE, %s, %s)
                ON CONFLICT (usuario_id) DO UPDATE SET
                    numero=EXCLUDED.numero, ativo=EXCLUDED.ativo, updated_at=EXCLUDED.updated_at
            """, (uid, numero, ativo, now, now))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro salvar whatsapp: {e}", flush=True)
        return False

def db_confirmar_whatsapp_optin(uid):
    """Marca opt-in como confirmado após usuário enviar msg para o Twilio."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE whatsapp_usuarios SET opt_in=TRUE, updated_at=%s
                WHERE usuario_id=%s
            """, (now, uid))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[DB] Erro confirmar opt-in: {e}", flush=True)
        return False

def db_buscar_whatsapp(uid):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM whatsapp_usuarios WHERE usuario_id=%s", (uid,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except: return None

def db_listar_whatsapp_ativos():
    """Retorna todos os usuários com WhatsApp ativo e opt-in confirmado."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT w.usuario_id, w.numero, u.nome
                FROM whatsapp_usuarios w
                JOIN usuarios u ON u.id = w.usuario_id
                WHERE w.ativo=TRUE AND w.opt_in=TRUE
            """)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] Erro listar whatsapp: {e}", flush=True)
        return []

def db_buscar_whatsapp_usuario(uid):
    """Retorna número e status do usuário específico."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM whatsapp_usuarios WHERE usuario_id=%s", (uid,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except: return None
