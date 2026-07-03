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
    """Retorna TODAS as posições (confirmadas e pendentes). O front separa por 'status'."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM carteira WHERE usuario_id=%s ORDER BY status, ticker", (uid,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('preco_medio'): r['preco_medio'] = float(r['preco_medio'])
                if r.get('quantidade'): r['quantidade'] = float(r['quantidade'])
        conn.close()
        return rows
    except: return []

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
