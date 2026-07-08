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

def db_salvar_posicao(uid, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora):
    """Salva/atualiza posição como CONFIRMADA (fluxo manual normal)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carteira (usuario_id, ticker, nome, cor, setor_id, setor_nome, preco_medio, quantidade, data_compra, corretora, adicionado_em, status, origem)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmada','manual')
                ON CONFLICT (usuario_id, ticker) DO UPDATE SET
                nome=%s, cor=%s, setor_id=%s, setor_nome=%s, preco_medio=%s, quantidade=%s, data_compra=%s, corretora=%s, adicionado_em=%s, status='confirmada'
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
                PRIMARY KEY (ticker, data, intervalo)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_ticker_data
                ON historico_precos(ticker, data DESC);
            CREATE INDEX IF NOT EXISTS idx_hist_ticker_intervalo
                ON historico_precos(ticker, intervalo, data DESC);
        """)
    conn.commit()

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

def db_listar_backtests(uid, limit=10):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, ticker, estrategia, data_inicio, data_fim,
                       capital_inicial, capital_final, retorno_pct,
                       retorno_ibov, alpha, drawdown_max, sharpe,
                       n_operacoes, created_at
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
                publica BOOLEAN DEFAULT FALSE,
                usos INTEGER DEFAULT 0,
                retorno_medio NUMERIC,
                sharpe_medio NUMERIC,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_est_publica
                ON backtesting_estrategias(publica, usos DESC);
            CREATE INDEX IF NOT EXISTS idx_bt_est_usuario
                ON backtesting_estrategias(usuario_id);
        """)
    conn.commit()

def db_salvar_estrategia_bt(uid, nome, descricao, tipo, regras, publica=False, retorno_medio=None, sharpe_medio=None):
    import json
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtesting_estrategias
                    (usuario_id, nome, descricao, tipo, regras, publica,
                     retorno_medio, sharpe_medio, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (uid, nome, descricao, tipo, json.dumps(regras), publica,
                  retorno_medio, sharpe_medio, now, now))
            row = cur.fetchone()
        conn.commit(); conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Erro salvar estratégia BT: {e}", flush=True)
        return None

def db_listar_estrategias_bt(uid=None, publicas=False, limit=20):
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if publicas:
                cur.execute("""
                    SELECT e.*, u.nome as autor
                    FROM backtesting_estrategias e
                    JOIN usuarios u ON u.id = e.usuario_id
                    WHERE e.publica = TRUE
                    ORDER BY e.usos DESC, e.retorno_medio DESC NULLS LAST
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
