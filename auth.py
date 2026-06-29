"""
Módulo de autenticação — JWT + bcrypt + SendGrid
"""
import os, jwt, bcrypt, secrets, string
from datetime import datetime, timezone, timedelta
import db

SECRET_KEY = os.getenv("JWT_SECRET", "janus-b3-secret-2026-mude-em-producao")
JWT_EXPIRES_HOURS = 72
SENDGRID_KEY = os.getenv("SENDGRID_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@janus-b3.com")
APP_URL = os.getenv("APP_URL", "https://b3-investimentos.onrender.com")

LANGS = {
    'pt': {
        'verify_subject': 'Janus B3 — Confirme seu e-mail',
        'verify_body': lambda nome, code: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🚀 Bem-vindo ao Janus B3, {nome}!</h2>
  <p>Seu código de verificação é:</p>
  <div style="font-size:36px;font-weight:bold;letter-spacing:8px;color:#0066cc;padding:20px;background:#f0f4ff;border-radius:12px;text-align:center">{code}</div>
  <p style="color:#666">Este código expira em 30 minutos.</p>
  <p style="color:#999;font-size:12px">Se você não criou esta conta, ignore este e-mail.</p>
</div>""",
        'reset_subject': 'Janus B3 — Redefinição de senha',
        'reset_body': lambda nome, link: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🔐 Redefinir senha — Janus B3</h2>
  <p>Olá, {nome}! Clique no botão abaixo para redefinir sua senha:</p>
  <a href="{link}" style="display:inline-block;padding:14px 28px;background:#0066cc;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">Redefinir senha</a>
  <p style="color:#666">Este link expira em 1 hora.</p>
  <p style="color:#999;font-size:12px">Se você não solicitou, ignore este e-mail.</p>
</div>""",
    },
    'en': {
        'verify_subject': 'Janus B3 — Confirm your email',
        'verify_body': lambda nome, code: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🚀 Welcome to Janus B3, {nome}!</h2>
  <p>Your verification code is:</p>
  <div style="font-size:36px;font-weight:bold;letter-spacing:8px;color:#0066cc;padding:20px;background:#f0f4ff;border-radius:12px;text-align:center">{code}</div>
  <p style="color:#666">This code expires in 30 minutes.</p>
</div>""",
        'reset_subject': 'Janus B3 — Password Reset',
        'reset_body': lambda nome, link: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🔐 Reset Password — Janus B3</h2>
  <p>Hi {nome}! Click below to reset your password:</p>
  <a href="{link}" style="display:inline-block;padding:14px 28px;background:#0066cc;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">Reset Password</a>
  <p style="color:#666">This link expires in 1 hour.</p>
</div>""",
    },
    'es': {
        'verify_subject': 'Janus B3 — Confirma tu correo',
        'verify_body': lambda nome, code: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🚀 ¡Bienvenido a Janus B3, {nome}!</h2>
  <p>Tu código de verificación es:</p>
  <div style="font-size:36px;font-weight:bold;letter-spacing:8px;color:#0066cc;padding:20px;background:#f0f4ff;border-radius:12px;text-align:center">{code}</div>
  <p style="color:#666">Este código expira en 30 minutos.</p>
</div>""",
        'reset_subject': 'Janus B3 — Restablecer contraseña',
        'reset_body': lambda nome, link: f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
  <h2 style="color:#0066cc">🔐 Restablecer contraseña — Janus B3</h2>
  <p>¡Hola {nome}! Haz clic para restablecer tu contraseña:</p>
  <a href="{link}" style="display:inline-block;padding:14px 28px;background:#0066cc;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">Restablecer contraseña</a>
  <p style="color:#666">Este enlace expira en 1 hora.</p>
</div>""",
    }
}

def hash_senha(senha): return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()
def verificar_senha(senha, hash_): return bcrypt.checkpw(senha.encode(), hash_.encode())

def gerar_codigo(): return ''.join(secrets.choice(string.digits) for _ in range(6))

def gerar_token(): return secrets.token_urlsafe(32)

def gerar_jwt(uid, email, plano='free'):
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRES_HOURS)
    return jwt.encode({'uid': uid, 'email': email, 'plano': plano, 'exp': exp}, SECRET_KEY, algorithm='HS256')

def verificar_jwt(token):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    except: return None

def gerar_jwt_admin(email):
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    return jwt.encode({'admin': True, 'email': email, 'exp': exp}, SECRET_KEY, algorithm='HS256')

def verificar_jwt_admin(token):
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return data if data.get('admin') else None
    except: return None

def enviar_email(dest, subject, html_body):
    if not SENDGRID_KEY:
        print(f"[EMAIL] SENDGRID_API_KEY não configurada. Para: {dest}", flush=True)
        print(f"[EMAIL] Assunto: {subject}", flush=True)
        return True  # Em dev, não falha
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        msg = Mail(from_email=EMAIL_FROM, to_emails=dest, subject=subject, html_content=html_body)
        sg = SendGridAPIClient(SENDGRID_KEY)
        resp = sg.send(msg)
        print(f"[EMAIL] Enviado para {dest}: {resp.status_code}", flush=True)
        return resp.status_code in [200, 202]
    except Exception as e:
        print(f"[EMAIL] Erro: {e}", flush=True)
        return False

def enviar_verificacao(email, nome, codigo, lang='pt'):
    l = LANGS.get(lang, LANGS['pt'])
    return enviar_email(email, l['verify_subject'], l['verify_body'](nome, codigo))

def enviar_reset(email, nome, token, lang='pt'):
    link = f"{APP_URL}/reset-senha?token={token}"
    l = LANGS.get(lang, LANGS['pt'])
    return enviar_email(email, l['reset_subject'], l['reset_body'](nome, link))

def expira_em(minutos=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutos)).isoformat()

def init_admin_padrao():
    """Cria admin padrão se não existir."""
    email = os.getenv("ADMIN_EMAIL", "admin@janus-b3.com")
    senha = os.getenv("ADMIN_PASSWORD", "Janus@Admin2026!")
    db.db_criar_admin(email, hash_senha(senha))
    print(f"[AUTH] Admin verificado: {email}", flush=True)
