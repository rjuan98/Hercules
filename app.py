from __future__ import annotations

import base64
import calendar
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from database import get_db, init_db

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:  # optional
    OAuth = None

try:
    import requests as http_requests
except ImportError:  # optional (necessário para a leitura de notas com IA)
    http_requests = None

# Leitura de notas com IA: liga sozinha quando a chave existir no ambiente
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

BASE_DIR = Path(__file__).resolve().parent
# Em produção (Render, PythonAnywhere etc.) aponte UPLOAD_DIR para o disco persistente
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or BASE_DIR / "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def _load_secret_key() -> str:
    """Chave fixa por instalação: sem ela, cada reinício do servidor derruba as sessões."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Fica logado por 90 dias — logar toda vez é exaustivo
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)

# Atrás de um proxy HTTPS (Render/Railway/PythonAnywhere), respeita os headers X-Forwarded-*
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if os.environ.get("SECURE_COOKIES") == "1" or os.environ.get("RENDER"):
    app.config["SESSION_COOKIE_SECURE"] = True

if os.environ.get("FLASK_ENV") == "development" or os.environ.get("DEBUG") == "1":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

oauth = None
if OAuth is not None and os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    oauth = OAuth(app)
    oauth.register(
        "google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

PROFILE_CHOICES = [
    ("pf", "Pessoa física"),
    ("mei", "MEI"),
    ("lojista", "Pequeno lojista"),
    ("hibrido", "Híbrido (PF + negócio)"),
]

HOME_FOCUS_CHOICES = [
    ("saldo", "Saldo atual"),
    ("spend_today", "Quanto posso gastar hoje"),
    ("everything_ok", "Tudo em dia"),
    ("month_end", "Quanto sobra no fim do mês"),
    ("where_money_goes", "Onde estou gastando"),
    ("goal", "Meta principal"),
]

NOTIFICATION_CHOICES = [
    ("silencioso", "Silencioso"),
    ("equilibrado", "Equilibrado"),
    ("detalhista", "Detalhista"),
]

TRANSACTION_TYPES = [
    ("saida", "Saída"),
    ("entrada", "Entrada"),
]

TRANSACTION_CATEGORIES = [
    "Alimentação",
    "Transporte",
    "Saúde",
    "Educação",
    "Moradia",
    "Lazer",
    "Assinaturas",
    "Mercado",
    "Varejo",
    "Serviços",
    "Reserva",
    "Outros",
]

INCOME_CATEGORIES = [
    "Salário",
    "Freelance / bico",
    "Vendas",
    "Reembolso",
    "Rendimentos",
    "Transferência recebida",
    "Presente",
    "Outros",
]

NOTE_CATEGORIES = [
    "Saúde",
    "Educação",
    "Moradia",
    "Transporte",
    "Alimentação",
    "Lazer",
    "Serviços",
    "Outros",
]

PLAN_LABELS = {
    "free": "Gratuito",
    "plus": "Plus",
}

# Teto anual de faturamento do MEI (Lei Complementar). Ultrapassar exige
# atenção (desenquadramento); o termômetro do Painel MEI usa este valor.
MEI_LIMITE_ANUAL = 81000.0

MONTH_NAMES = {
    "01": "Janeiro",
    "02": "Fevereiro",
    "03": "Março",
    "04": "Abril",
    "05": "Maio",
    "06": "Junho",
    "07": "Julho",
    "08": "Agosto",
    "09": "Setembro",
    "10": "Outubro",
    "11": "Novembro",
    "12": "Dezembro",
}


# ------------------------
# Helpers
# ------------------------

def money(value: Any) -> str:
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def format_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value)
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return text
    return dt.strftime("%d/%m/%Y")


def month_label(month_string: str) -> str:
    if not month_string or "-" not in month_string:
        return month_string or ""
    year, month = month_string.split("-", 1)
    return f"{MONTH_NAMES.get(month, month)} de {year}"


def normalize_profile(value: str | None) -> str:
    value = (value or "pf").strip().lower()
    if value in {"pessoal", "pf", "personal", "fisica", "física"}:
        return "pf"
    if value in {"mei"}:
        return "mei"
    if value in {"lojista", "business", "negocio", "negócio"}:
        return "lojista"
    if value in {"hibrido", "híbrido", "both", "pf+mei"}:
        return "hibrido"
    return "pf"


def normalize_focus(value: str | None) -> str:
    value = (value or "saldo").strip()
    valid = {k for k, _ in HOME_FOCUS_CHOICES}
    return value if value in valid else "saldo"


def normalize_notification_mode(value: str | None) -> str:
    value = (value or "equilibrado").strip().lower()
    valid = {k for k, _ in NOTIFICATION_CHOICES}
    return value if value in valid else "equilibrado"


def allowed_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in {"pdf", "png", "jpg", "jpeg", "webp"}


def sanitize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


# Sentinela para "não perguntar mais sobre esse padrão"
IGNORE_RULE = "__manter__"

# ------------------------
# Interpretador de capturas (notificações de banco e frases livres)
# ------------------------
# Valor com R$ explícito tem prioridade (evita confundir com número do cartão)
_MONEY_RS = re.compile(r"R\$\s*([\d\.]+,\d{2}|[\d\.]+(?:,\d{1,2})?)")
_MONEY_BARE = re.compile(r"(?<![\d,\.])(\d+(?:[.,]\d{1,2})?)(?![\d])")

_ENTRADA_HINTS = (
    "recebeu", "recebido", "recebida", "recebi", "pix recebido", "caiu na conta",
    "depósito", "deposito", "salário", "salario", "te pagou", "crédito de", "credito de",
    "transferência recebida", "transferencia recebida", "ganhei", "entrou",
)
_SAIDA_HINTS = (
    "compra", "comprei", "pagamento", "pagou", "paguei", "gastei", "débito", "debito",
    "pix enviado", "enviou um pix", "você enviou", "voce enviou", "saque", "boleto",
    "transferência enviada", "transferencia enviada", "fatura", "aprovada em", "aprovada no",
)
_MERCHANT_CUTOFFS = (
    # "cart" pega cartão/cartao mesmo com problema de acento
    " para o cart", " com o cart", " no cart", " no seu cart", " cart%",
    " cartão", " cartao", " final ", " às ", " as ", " hoje", " agora",
    " em ", ",", ".", ";", " - ", " no valor", " valor de",
)
# "no crédito" (forma de pagamento) é diferente de "crédito de" (dinheiro entrando) —
# por isso é uma checagem separada, não reaproveita _ENTRADA_HINTS.
_CREDIT_CARD_HINTS = (
    "no crédito", "no credito", "cartão de crédito", "cartao de credito", "fatura",
)


def parse_capture_text(user_id: int, text: str) -> dict[str, Any]:
    """Extrai valor, tipo e estabelecimento de um texto de notificação ou frase livre.
    Devolve {'ok': bool, 'valor', 'tipo', 'estabelecimento', 'descricao'}."""
    raw = sanitize_text(text)
    low = raw.lower()
    result: dict[str, Any] = {
        "ok": False, "valor": 0.0, "tipo": None, "estabelecimento": None, "descricao": raw[:120],
        "no_credito": any(h in low for h in _CREDIT_CARD_HINTS),
    }
    if not raw:
        return result

    m = _MONEY_RS.search(raw) or _MONEY_BARE.search(raw)
    if not m:
        return result
    result["valor"] = parse_money(m.group(1))
    if result["valor"] <= 0:
        return result

    if any(h in low for h in _ENTRADA_HINTS):
        result["tipo"] = "entrada"
    elif any(h in low for h in _SAIDA_HINTS):
        result["tipo"] = "saida"

    # Estabelecimento: o que vem depois de "em/no/na/para/pra/de" após o valor.
    # Remove antes a forma de pagamento ("no crédito"/"no débito"), senão ela
    # é capturada como se fosse o nome do estabelecimento.
    after_value = raw[m.end():]
    after_value = re.sub(r"\bno\s+cr[ée]dito\b", "", after_value, flags=re.IGNORECASE)
    after_value = re.sub(r"\bno\s+d[ée]bito\b", "", after_value, flags=re.IGNORECASE)
    merchant_match = re.search(r"\b(?:em|no|na|pra|para|com|de|do|da)\s+(.{2,60})", after_value, re.IGNORECASE)
    if merchant_match:
        merchant = merchant_match.group(1)
        low_merchant = merchant.lower()
        cut = len(merchant)
        for stop in _MERCHANT_CUTOFFS:
            idx = low_merchant.find(stop)
            if idx > 1:
                cut = min(cut, idx)
        merchant = sanitize_text(merchant[:cut])
        if merchant:
            result["estabelecimento"] = merchant[:60]

    # Frase livre sem dica de direção ("12 quentinha") assume saída
    if result["tipo"] is None and result["estabelecimento"]:
        result["tipo"] = "saida"

    result["ok"] = bool(result["valor"] > 0 and result["tipo"])
    return result


def register_capture(user_id: int, text: str, origem: str = "notificacao") -> dict[str, Any]:
    """Interpreta e lança a captura. Alta confiança vira transação; dúvida vira pendente."""
    # Diagnóstico do app (notificação sem título/texto reconhecível): nunca
    # tenta extrair valor daqui — o dump pode conter números soltos (IDs,
    # timestamps) que pareceriam um valor válido e virariam um lançamento falso.
    if text.strip().startswith("[DIAGNOSTICO"):
        with get_db() as db:
            db.execute(
                "INSERT INTO capturas (user_id, origem, conteudo, status, dados_extraidos) VALUES (?, ?, ?, 'pendente', ?)",
                (user_id, origem, sanitize_text(text)[:500], json.dumps({"diagnostico": True})),
            )
        return {"status": "pendente"}

    parsed = parse_capture_text(user_id, text)
    today_iso = date.today().isoformat()

    if parsed["ok"] and parsed["estabelecimento"]:
        alvo = parsed["estabelecimento"]
        with get_db() as db:
            # Dedup: mesma pessoa, mesmo valor e lugar nos últimos 3 minutos
            dup = db.execute(
                """SELECT id FROM transacoes
                   WHERE user_id = ? AND valor = ? AND LOWER(COALESCE(estabelecimento,'')) = LOWER(?)
                     AND datetime(created_at) >= datetime('now', '-3 minutes')""",
                (user_id, parsed["valor"], alvo),
            ).fetchone()
            if dup:
                return {"status": "duplicada", "id": dup["id"]}
            # Categoriza só pelo estabelecimento — a frase inteira engana
            # (ex.: "GAStei" casa com a palavra-chave "gás" de Moradia)
            categoria = categorize(user_id, alvo)
            if parsed["tipo"] == "entrada" and categoria in TRANSACTION_CATEGORIES:
                categoria = "Outros"
            no_credito = 1 if (parsed["tipo"] == "saida" and parsed.get("no_credito")) else 0
            cur = db.execute(
                """INSERT INTO transacoes
                   (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte, confidence, needs_review, no_credito)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 90, 0, ?)""",
                (user_id, parsed["tipo"], parsed["valor"], alvo, alvo, categoria, today_iso, origem, no_credito),
            )
        return {"status": "lancada", "id": cur.lastrowid, "categoria": categoria,
                "valor": parsed["valor"], "tipo": parsed["tipo"], "estabelecimento": alvo, "no_credito": bool(no_credito)}

    # Não entendeu o bastante: fila de pendentes para o check-in
    with get_db() as db:
        db.execute(
            "INSERT INTO capturas (user_id, origem, conteudo, status, dados_extraidos) VALUES (?, ?, ?, 'pendente', ?)",
            (user_id, origem, sanitize_text(text)[:500], json.dumps(parsed, ensure_ascii=False)),
        )
    return {"status": "pendente"}


def pending_captures(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM capturas WHERE user_id = ? AND status = 'pendente' ORDER BY datetime(created_at) DESC LIMIT 10",
            (user_id,),
        ).fetchall()


# ------------------------
# OFX: importação de extrato com reconciliação
# ------------------------
_OFX_TRN_RE = re.compile(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", re.DOTALL | re.IGNORECASE)


def _ofx_field(block: str, tag: str) -> str:
    """Campo OFX em SGML (sem fechamento) ou XML (com fechamento)."""
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return sanitize_text(m.group(1)) if m else ""


def parse_ofx(content: str) -> list[dict[str, Any]]:
    """Extrai as transações de um arquivo OFX. Bancos brasileiros usam OFX 1.x (SGML) ou 2.x (XML).
    Extrato de fatura de cartão de crédito vem num bloco <CCSTMTRS> (em vez de <STMTRS> da
    conta corrente) — marcamos todas as transações desse arquivo como 'no crédito', porque
    esse dinheiro ainda não saiu da conta, só vai sair quando a fatura for paga."""
    is_credit_card_file = bool(re.search(r"<CCSTMTRS", content, re.IGNORECASE))
    transactions = []
    for m in _OFX_TRN_RE.finditer(content):
        block = m.group(1)
        amount_raw = _ofx_field(block, "TRNAMT").replace(",", ".")
        try:
            amount = float(amount_raw)
        except ValueError:
            continue
        if amount == 0:
            continue
        dt_raw = _ofx_field(block, "DTPOSTED")[:8]
        try:
            dt = datetime.strptime(dt_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        memo = _ofx_field(block, "MEMO") or _ofx_field(block, "NAME") or "Movimentação importada"
        transactions.append({
            "valor": abs(amount),
            "tipo": "entrada" if amount > 0 else "saida",
            "data": dt,
            "descricao": memo[:120],
            "fitid": _ofx_field(block, "FITID")[:80] or None,
            "no_credito": is_credit_card_file,
        })
    return transactions


def import_ofx_transactions(user_id: int, items: list[dict[str, Any]], forcar_credito: bool = False) -> dict[str, int]:
    """Importa com reconciliação: FITID já visto = pula; valor+data já registrado
    (captura/manual) = casa e marca; anterior ao saldo inicial = pula (protege o saldo)."""
    stats = {"importadas": 0, "ja_importadas": 0, "reconciliadas": 0, "antigas": 0}
    with get_db() as db:
        saldo_row = db.execute(
            """SELECT date(COALESCE(data_transacao, created_at)) AS dia FROM transacoes
               WHERE user_id = ? AND fonte = 'ajuste' AND descricao = 'Saldo inicial'
               ORDER BY dia ASC LIMIT 1""",
            (user_id,),
        ).fetchone()
        saldo_date = saldo_row["dia"] if saldo_row else None

        for item in items:
            # O saldo inicial já resume o passado: importar dias anteriores duplicaria dinheiro
            if saldo_date and item["data"] < saldo_date:
                stats["antigas"] += 1
                continue
            if item["fitid"]:
                seen = db.execute(
                    "SELECT 1 FROM transacoes WHERE user_id = ? AND fitid = ?",
                    (user_id, item["fitid"]),
                ).fetchone()
                if seen:
                    stats["ja_importadas"] += 1
                    continue
            no_credito = 1 if (item.get("no_credito") or forcar_credito) else 0

            # Reconciliação: o Herc (ou o usuário) já registrou esse valor nesse dia?
            match = db.execute(
                """SELECT id FROM transacoes
                   WHERE user_id = ? AND tipo = ? AND ABS(valor - ?) < 0.005
                     AND date(COALESCE(data_transacao, created_at)) = date(?)
                     AND fitid IS NULL AND fonte != 'ofx'
                   LIMIT 1""",
                (user_id, item["tipo"], item["valor"], item["data"]),
            ).fetchone()
            if match:
                # O extrato sabe melhor se foi no crédito do que a captura em tempo real
                db.execute(
                    "UPDATE transacoes SET fitid = ?, no_credito = ? WHERE id = ?",
                    (item["fitid"], no_credito, match["id"]),
                )
                stats["reconciliadas"] += 1
                continue
            categoria = categorize(user_id, item["descricao"]) if item["tipo"] == "saida" else "Outros"
            if apply_rules(user_id, item["descricao"]):
                categoria = apply_rules(user_id, item["descricao"])
            db.execute(
                """INSERT INTO transacoes
                   (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte, confidence, fitid, no_credito)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ofx', 95, ?, ?)""",
                (user_id, item["tipo"], item["valor"], item["descricao"], item["descricao"],
                 categoria, item["data"], item["fitid"], no_credito),
            )
            stats["importadas"] += 1
    return stats


# ------------------------
# Os 12 Trabalhos de Hércules (conquistas reais, não medalhas vazias)
# ------------------------
TRABALHOS = [
    {"key": "leao", "emoji": "🦁", "nome": "O Leão de Nemeia",
     "feito": "Domou uma categoria: um mês inteiro dentro do limite.",
     "como": "Crie um limite mensal numa categoria e feche o mês sem estourar."},
    {"key": "hidra", "emoji": "🐍", "nome": "A Hidra de Lerna",
     "feito": "Cortou 3 cabeças: ensinou 3 regras ao Herc.",
     "como": "Ensine o Herc 3 vezes (ex.: 'Dennys é Doces') na tela inicial ou em Categorias."},
    {"key": "corca", "emoji": "🦌", "nome": "A Corça de Cerineia",
     "feito": "Mais rápido que a flecha: um gasto anotado sem tocar no app.",
     "como": "Configure a captura automática nas Configurações e faça uma compra."},
    {"key": "javali", "emoji": "🐗", "nome": "O Javali de Erimanto",
     "feito": "Capturou o javali: fechou um mês no azul.",
     "como": "Termine um mês com as entradas maiores que as saídas."},
    {"key": "estabulos", "emoji": "🧹", "nome": "Os Estábulos de Augias",
     "feito": "Tudo limpo: nenhuma pendência, nenhuma conta vencida.",
     "como": "Resolva as capturas pendentes e não deixe contas vencerem."},
    {"key": "aves", "emoji": "🦅", "nome": "As Aves do Estínfale",
     "feito": "Espantou as aves: 10 gastos capturados automaticamente.",
     "como": "Deixe a captura automática trabalhar por você."},
    {"key": "touro", "emoji": "🐂", "nome": "O Touro de Creta",
     "feito": "Domou o touro: 7 dias seguidos fechando o dia.",
     "como": "Feche o dia na tela inicial por 7 dias seguidos."},
    {"key": "eguas", "emoji": "🐎", "nome": "As Éguas de Diomedes",
     "feito": "Domou as éguas selvagens: 30 dias seguidos com o Herc.",
     "como": "Mantenha a sequência de check-ins por 30 dias."},
    {"key": "cinto", "emoji": "🎗️", "nome": "O Cinto de Hipólita",
     "feito": "Conquistou o cinto: completou a primeira meta.",
     "como": "Crie uma meta e guarde até completar."},
    {"key": "gado", "emoji": "🐄", "nome": "O Gado de Gerião",
     "feito": "Trouxe o rebanho de longe: importou um extrato do banco.",
     "como": "Importe um arquivo OFX em Entradas e saídas."},
    {"key": "pomos", "emoji": "🍎", "nome": "Os Pomos das Hespérides",
     "feito": "Colheu os frutos de ouro: guardou dinheiro em 3 meses diferentes.",
     "como": "Faça aportes na sua reserva em 3 meses distintos."},
    {"key": "cerbero", "emoji": "🐕", "nome": "Cérbero",
     "feito": "Domou o guardião: 10 notas fiscais organizadas.",
     "como": "Guarde 10 notas — no fim do ano, o contador agradece."},
]


def _prev_month_bounds():
    first_this = date.today().replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), last_prev.isoformat()


def _trabalho_conquistado(user_id: int, key: str, db) -> bool:
    ini, fim = _prev_month_bounds()
    if key == "leao":
        cats = db.execute(
            "SELECT nome, limite_mensal FROM categorias WHERE user_id = ? AND limite_mensal > 0", (user_id,)
        ).fetchall()
        for cat in cats:
            gasto = db.execute(
                """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                   WHERE user_id = ? AND tipo = 'saida' AND categoria = ?
                     AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
                (user_id, cat["nome"], ini, fim),
            ).fetchone()["t"]
            if float(gasto) <= float(cat["limite_mensal"]):
                return True
        return False
    if key == "hidra":
        n = db.execute(
            "SELECT COUNT(*) AS n FROM regras_categorizacao WHERE user_id = ? AND categoria_nome != ?",
            (user_id, IGNORE_RULE),
        ).fetchone()["n"]
        return n >= 3
    if key == "corca":
        return db.execute(
            "SELECT 1 FROM transacoes WHERE user_id = ? AND fonte = 'notificacao' LIMIT 1", (user_id,)
        ).fetchone() is not None
    if key == "javali":
        row = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN tipo='entrada' THEN valor ELSE 0 END), 0) AS e,
                      COALESCE(SUM(CASE WHEN tipo='saida' THEN valor ELSE 0 END), 0) AS s,
                      COUNT(*) AS n
               FROM transacoes
               WHERE user_id = ? AND fonte != 'ajuste'
                 AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, ini, fim),
        ).fetchone()
        return row["n"] > 0 and float(row["e"]) >= float(row["s"])
    if key == "estabulos":
        total = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if total < 10:
            return False
        pend = db.execute(
            "SELECT 1 FROM capturas WHERE user_id = ? AND status = 'pendente' LIMIT 1", (user_id,)
        ).fetchone()
        vencida = db.execute(
            """SELECT 1 FROM compromissos WHERE user_id = ? AND status = 'pendente'
               AND date(vencimento) < date('now') LIMIT 1""",
            (user_id,),
        ).fetchone()
        return pend is None and vencida is None
    if key == "aves":
        n = db.execute(
            "SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ? AND fonte = 'notificacao'", (user_id,)
        ).fetchone()["n"]
        return n >= 10
    if key == "touro":
        return checkin_streak(user_id) >= 7
    if key == "eguas":
        return checkin_streak(user_id) >= 30
    if key == "cinto":
        return db.execute(
            "SELECT 1 FROM metas WHERE user_id = ? AND meta_valor > 0 AND valor_atual >= meta_valor LIMIT 1",
            (user_id,),
        ).fetchone() is not None
    if key == "gado":
        return db.execute(
            "SELECT 1 FROM transacoes WHERE user_id = ? AND fonte = 'ofx' LIMIT 1", (user_id,)
        ).fetchone() is not None
    if key == "pomos":
        n = db.execute(
            """SELECT COUNT(DISTINCT strftime('%Y-%m', COALESCE(data_transacao, created_at))) AS n
               FROM transacoes WHERE user_id = ? AND categoria = 'Reserva' AND tipo = 'saida'""",
            (user_id,),
        ).fetchone()["n"]
        return n >= 3
    if key == "cerbero":
        n = db.execute("SELECT COUNT(*) AS n FROM notas WHERE user_id = ?", (user_id,)).fetchone()["n"]
        return n >= 10
    return False


def evaluate_trabalhos(user_id: int) -> list[str]:
    """Verifica os trabalhos ainda não conquistados; concede os que foram cumpridos.
    Devolve as chaves recém-conquistadas."""
    novos = []
    with get_db() as db:
        feitos = {r["trabalho"] for r in db.execute(
            "SELECT trabalho FROM trabalhos WHERE user_id = ?", (user_id,)
        ).fetchall()}
        for t in TRABALHOS:
            if t["key"] in feitos:
                continue
            if _trabalho_conquistado(user_id, t["key"], db):
                db.execute(
                    "INSERT OR IGNORE INTO trabalhos (user_id, trabalho, conquistado_em) VALUES (?, ?, ?)",
                    (user_id, t["key"], date.today().isoformat()),
                )
                novos.append(t["key"])
    return novos


# Dicas do Herc: ensino contextual, uma frase por vez, some depois de vista
HERC_TIPS = {
    "registro_rapido": "Dica: escreve ali em cima algo como “gastei 10 no mercado” que eu entendo e anoto sozinho. Pode até falar, no botão do microfone. 🎤",
    "primeira_captura": "Viu essa movimentação aí? Eu anotei sozinho pela notificação do banco — você não precisou fazer nada. 😉",
    "primeira_nota": "Guardei sua nota! Sempre que precisar achar alguma, elas ficam todas aqui, organizadas. No fim do ano, é só exportar para o contador.",
}


def tip_seen(user_id: int, key: str) -> bool:
    with get_db() as db:
        return db.execute(
            "SELECT 1 FROM dicas_vistas WHERE user_id = ? AND dica = ?", (user_id, key)
        ).fetchone() is not None


def checkin_streak(user_id: int) -> int:
    """Dias consecutivos de check-in, contando a partir de hoje (ou ontem, se hoje ainda não fechou)."""
    with get_db() as db:
        dias = [r["dia"] for r in db.execute(
            "SELECT dia FROM checkins WHERE user_id = ? ORDER BY dia DESC LIMIT 366", (user_id,)
        ).fetchall()]
    if not dias:
        return 0
    known = set(dias)
    cursor = date.today()
    if cursor.isoformat() not in known:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.isoformat() in known:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def user_categories(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM categorias WHERE user_id = ? ORDER BY nome COLLATE NOCASE",
            (user_id,),
        ).fetchall()


def expense_category_names(user_id: int) -> list[str]:
    """Categorias fixas + as criadas pelo usuário (sem duplicar)."""
    custom = [c["nome"] for c in user_categories(user_id)]
    base = [c for c in TRANSACTION_CATEGORIES if c not in custom]
    return custom + base


def user_rules(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM regras_categorizacao WHERE user_id = ? ORDER BY datetime(created_at) DESC",
            (user_id,),
        ).fetchall()


def apply_rules(user_id: int, *texts: str | None) -> str | None:
    """Regra aprendida vence tudo: se o padrão aparece no texto, devolve a categoria."""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return None
    for rule in user_rules(user_id):
        if rule["categoria_nome"] == IGNORE_RULE:
            continue
        if rule["padrao_texto"].lower() in haystack:
            return rule["categoria_nome"]
    return None


def categorize(user_id: int, *texts: str | None) -> str:
    """Ordem de decisão: regras que o usuário ensinou > palavras-chave genéricas."""
    return apply_rules(user_id, *texts) or auto_category(" ".join(t for t in texts if t))


def pending_suggestions(user_id: int, limit: int = 2):
    """Gastos repetidos que caíram em 'Outros': o Hércules pergunta uma vez o que são."""
    with get_db() as db:
        rows = db.execute(
            """SELECT LOWER(TRIM(COALESCE(NULLIF(estabelecimento, ''), descricao))) AS padrao,
                      MAX(COALESCE(NULLIF(estabelecimento, ''), descricao)) AS display,
                      COUNT(*) AS vezes,
                      SUM(valor) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida'
                 AND COALESCE(NULLIF(categoria, ''), 'Outros') = 'Outros'
                 AND COALESCE(NULLIF(estabelecimento, ''), descricao) IS NOT NULL
                 AND date(COALESCE(data_transacao, created_at)) >= date('now', '-60 day')
               GROUP BY padrao
               HAVING COUNT(*) >= 3
               ORDER BY total DESC""",
            (user_id,),
        ).fetchall()
    known = {r["padrao_texto"].lower() for r in user_rules(user_id)}
    return [r for r in rows if r["padrao"] not in known][:limit]


def reclassify_transactions(user_id: int, pattern: str, categoria: str) -> int:
    """Aplica uma regra nova ao passado. Devolve quantas movimentações mudaram."""
    like = f"%{pattern}%"
    with get_db() as db:
        cur = db.execute(
            """UPDATE transacoes SET categoria = ?
               WHERE user_id = ? AND (descricao LIKE ? OR estabelecimento LIKE ?)
                 AND COALESCE(categoria, '') != ?""",
            (categoria, user_id, like, like, categoria),
        )
        return cur.rowcount


def category_month_spending(user_id: int) -> dict[str, float]:
    month_start, month_end = month_bounds()
    with get_db() as db:
        rows = db.execute(
            """SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS categoria, SUM(valor) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida'
                 AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)
               GROUP BY categoria""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return {r["categoria"]: float(r["total"] or 0) for r in rows}


def auto_category(text: str) -> str:
    txt = (text or "").lower()
    rules = {
        "Saúde": ["farmácia", "hospital", "médic", "medic", "consulta", "dent", "psic", "laboratório"],
        "Educação": ["escola", "curso", "faculdade", "colégio", "livro", "aula", "treinamento"],
        "Moradia": ["aluguel", "condom", "luz", "água", "agua", "internet", "gás", "gas"],
        "Transporte": ["uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "passagem", "combust", "estacionamento"],
        "Alimentação": ["ifood", "ifood", "restaurante", "lanche", "salgado", "mercado", "padaria", "almoço", "almoco"],
        "Lazer": ["cinema", "show", "viagem", "hotel", "streaming", "spotify", "netflix", "jogo"],
        "Serviços": ["consultoria", "freela", "manutenção", "manutencao", "design", "site", "software"],
        "Assinaturas": ["assinatura", "mensalidade", "recorrente", "subscription"],
    }
    for category, keywords in rules.items():
        if any(word in txt for word in keywords):
            return category
    return "Outros"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def generate_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def current_user():
    if "user_id" not in session:
        return None
    with get_db() as db:
        return db.execute("SELECT * FROM usuarios WHERE id = ?", (session["user_id"],)).fetchone()


def user_profile(user) -> str:
    return normalize_profile(user["perfil"] if user else session.get("perfil", "pf"))


def is_business_profile(profile: str) -> bool:
    return profile in {"mei", "lojista", "hibrido"}


def is_personal_profile(profile: str) -> bool:
    return profile in {"pf", "hibrido"}


# A cada quantos dias o Herc lembra de importar o extrato — cobre o que a
# captura automática deixar passar, sem depender dela funcionar perfeitamente.
OFX_LEMBRETE_DIAS = 7


def dias_desde_ultimo_ofx(user) -> int | None:
    """None = nunca importou (sempre lembra); caso contrário, dias desde a última vez."""
    last = user["last_ofx_import"] if "last_ofx_import" in user.keys() else None
    if not last:
        return None
    try:
        return (date.today() - date.fromisoformat(last)).days
    except ValueError:
        return None


def get_or_create_capture_token(user) -> str:
    """Token do app companion: nasce no primeiro pedido, independente de
    como a pessoa logou (e-mail/senha ou Google)."""
    token = user["capture_token"] if "capture_token" in user.keys() else None
    if not token:
        token = secrets.token_urlsafe(24)
        with get_db() as db:
            db.execute("UPDATE usuarios SET capture_token = ? WHERE id = ?", (token, user["id"]))
    return token


def create_auto_login_code(user_id: int) -> str:
    """Código de uso único (2 minutos) para o WebView do app virar uma sessão
    de verdade após o login do Google acontecer numa aba de navegador
    separada (Custom Tab), que não compartilha cookies com o WebView."""
    code = secrets.token_urlsafe(24)
    expires_at = (datetime.utcnow() + timedelta(minutes=2)).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO app_auto_login (user_id, code, expires_at) VALUES (?, ?, ?)",
            (user_id, code, expires_at),
        )
    return code


def redeem_auto_login_code(code: str):
    """Troca o código pela linha do usuário, uma única vez. Devolve None se
    inválido, já usado ou expirado."""
    if not code:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM app_auto_login WHERE code = ? AND used = 0",
            (code,),
        ).fetchone()
        if not row:
            return None
        try:
            expired = datetime.utcnow() > datetime.fromisoformat(row["expires_at"])
        except ValueError:
            expired = True
        if expired:
            return None
        db.execute("UPDATE app_auto_login SET used = 1 WHERE id = ?", (row["id"],))
        user = db.execute("SELECT * FROM usuarios WHERE id = ?", (row["user_id"],)).fetchone()
    return user


def save_uploaded_file(file_storage) -> str | None | bool:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    if not allowed_file(file_storage.filename):
        return False
    safe_name = secure_filename(file_storage.filename)
    ext = Path(safe_name).suffix.lower()
    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / name
    file_storage.save(path)
    return name


def remove_uploaded_file(filename: str | None) -> None:
    if not filename:
        return
    try:
        path = UPLOAD_DIR / filename
        if path.exists():
            path.unlink()
    except OSError:
        pass


def month_bounds(dt: date | None = None):
    dt = dt or date.today()
    first = dt.replace(day=1)
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    last = dt.replace(day=last_day)
    return first, last


def parse_money(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    # "1.234,56" (pt-BR) → vírgula é o decimal; "99.90" (input type=number) → ponto é o decimal
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def days_left_in_month() -> int:
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return max(1, last_day - today.day + 1)


def months_until(deadline: str | None) -> int | None:
    """Meses (arredondando para cima, mínimo 1) entre hoje e o prazo ISO. None se sem prazo válido."""
    if not deadline:
        return None
    try:
        target = date.fromisoformat(deadline)
    except ValueError:
        return None
    today = date.today()
    if target <= today:
        return 1
    months = (target.year - today.year) * 12 + (target.month - today.month)
    if target.day > today.day:
        months += 1
    return max(1, months)


def _count_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def compute_recommended_focus(stats: dict[str, Any]) -> str:
    overdue = _count_value(stats.get("overdue_commitments"))
    due_soon = _count_value(stats.get("due_soon_commitments"))

    if overdue > 0 or due_soon > 0:
        return "everything_ok"
    if stats.get("remaining_month", 0) < 0:
        return "everything_ok"
    if stats.get("month_expenses", 0) > stats.get("month_income", 0):
        return "where_money_goes"
    if stats.get("goal_progress", 0) < 50 and stats.get("goal_active", False):
        return "goal"
    return "spend_today"


def calc_transaction_totals(user_id: int):
    today = date.today()
    month_start, month_end = month_bounds(today)
    with get_db() as db:
        transactions = db.execute(
            "SELECT * FROM transacoes WHERE user_id = ? ORDER BY date(COALESCE(data_transacao, created_at)) DESC",
            (user_id,),
        ).fetchall()
        month_income = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'entrada'
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        # Compra no crédito não sai da conta agora — fica de fora do saldo e do
        # "quanto posso gastar hoje" até a fatura ser paga de verdade.
        month_expenses = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        balance = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN tipo = 'entrada' THEN valor ELSE -valor END), 0) AS total
               FROM transacoes WHERE user_id = ? AND no_credito = 0""",
            (user_id,),
        ).fetchone()["total"]
        fatura_credito_mes = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 1
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        monthly_by_category = db.execute(
            """SELECT COALESCE(categoria, 'Outros') AS categoria,
                      SUM(CASE WHEN tipo = 'saida' THEN valor ELSE 0 END) AS total
               FROM transacoes
               WHERE user_id = ? AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)
               GROUP BY categoria
               ORDER BY total DESC""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
        upcoming_commitments = db.execute(
            """SELECT * FROM compromissos
               WHERE user_id = ? AND status = 'pendente'
               ORDER BY date(vencimento) ASC""",
            (user_id,),
        ).fetchall()
        goals = db.execute(
            "SELECT * FROM metas WHERE user_id = ? ORDER BY ativo DESC, created_at DESC",
            (user_id,),
        ).fetchall()
        notes = db.execute(
            "SELECT * FROM notas WHERE user_id = ? ORDER BY datetime(data_upload) DESC",
            (user_id,),
        ).fetchall()
        recent = db.execute(
            "SELECT * FROM transacoes WHERE user_id = ? ORDER BY date(COALESCE(data_transacao, created_at)) DESC LIMIT 8",
            (user_id,),
        ).fetchall()

    due_soon_cutoff = date.today() + timedelta(days=7)
    overdue_commitments = [c for c in upcoming_commitments if c["vencimento"] and date.fromisoformat(c["vencimento"]) < date.today()]
    due_soon_commitments = [
        c for c in upcoming_commitments
        if c["vencimento"] and date.today() <= date.fromisoformat(c["vencimento"]) <= due_soon_cutoff
    ]
    commitments_total = sum(float(c["valor"]) for c in due_soon_commitments)
    goal_active = False
    goal_progress = 0.0
    current_goal = None
    if goals:
        current_goal = goals[0]
        goal_active = bool(current_goal["ativo"])
        if float(current_goal["meta_valor"]) > 0:
            goal_progress = min(100.0, (float(current_goal["valor_atual"]) / float(current_goal["meta_valor"])) * 100.0)

    # --- Reserva: quanto separar por mês para cumprir a meta no prazo ---
    with get_db() as db:
        user_row = db.execute("SELECT meta_mensal FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        month_reserve_saved = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND categoria = 'Reserva'
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]

    goal_missing = 0.0
    goal_months_left = None
    reserve_monthly_needed = 0.0
    goal_deadline = None
    if current_goal is not None and goal_active:
        goal_missing = max(0.0, float(current_goal["meta_valor"]) - float(current_goal["valor_atual"]))
        try:
            goal_deadline = current_goal["prazo"]
        except (KeyError, IndexError):
            goal_deadline = None
        goal_months_left = months_until(goal_deadline)
        if goal_missing > 0:
            if goal_months_left:
                reserve_monthly_needed = goal_missing / goal_months_left
            elif user_row and float(user_row["meta_mensal"] or 0) > 0:
                reserve_monthly_needed = float(user_row["meta_mensal"])
    elif user_row and float(user_row["meta_mensal"] or 0) > 0:
        reserve_monthly_needed = float(user_row["meta_mensal"])

    reserve_saved_month = float(month_reserve_saved or 0)
    reserve_remaining_month = max(0.0, reserve_monthly_needed - reserve_saved_month)

    # Sem reserva: quanto sobra e quanto dá para gastar por dia
    remaining_month = float(balance) - commitments_total
    # Com reserva: o que dá para gastar sem comprometer o que precisa ser guardado
    spendable_month = remaining_month - reserve_remaining_month
    available_today = max(0.0, spendable_month / days_left_in_month())
    available_today_no_reserve = max(0.0, remaining_month / days_left_in_month())

    # Previsão: guardando o necessário por mês, quando a meta fica completa
    goal_forecast_months = None
    if goal_missing > 0 and reserve_monthly_needed > 0:
        goal_forecast_months = int(-(-goal_missing // reserve_monthly_needed))  # ceil

    stats = {
        "transactions": transactions,
        "month_income": float(month_income or 0),
        "month_expenses": float(month_expenses or 0),
        "balance": float(balance or 0),
        "fatura_credito_mes": float(fatura_credito_mes or 0),
        "monthly_by_category": monthly_by_category,
        "upcoming_commitments": upcoming_commitments,
        "overdue_commitments": overdue_commitments,
        "due_soon_commitments": due_soon_commitments,
        "commitments_total": float(commitments_total),
        "goals": goals,
        "notes": notes,
        "recent_transactions": recent,
        "goal_active": goal_active,
        "goal_progress": goal_progress,
        "current_goal": current_goal,
        "remaining_month": float(remaining_month),
        "spendable_month": float(spendable_month),
        "available_today": float(available_today),
        "available_today_no_reserve": float(available_today_no_reserve),
        "reserve_monthly_needed": float(reserve_monthly_needed),
        "reserve_saved_month": float(reserve_saved_month),
        "reserve_remaining_month": float(reserve_remaining_month),
        "goal_missing": float(goal_missing),
        "goal_months_left": goal_months_left,
        "goal_deadline": goal_deadline,
        "goal_forecast_months": goal_forecast_months,
        "next_income": None,
    }
    return stats


def sync_note_transaction(user_id: int, note_id: int, descricao: str, valor: float, categoria: str, tipo: str, data_emissao: str | None):
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM transacoes WHERE nota_id = ? AND user_id = ?",
            (note_id, user_id),
        ).fetchone()
        payload = (
            user_id,
            note_id,
            tipo,
            valor,
            descricao,
            descricao,
            categoria,
            data_emissao,
            "nota",
            100,
            0,
            json.dumps({"linked": True, "note_id": note_id}),
        )
        if existing:
            db.execute(
                """UPDATE transacoes SET tipo = ?, valor = ?, descricao = ?, estabelecimento = ?, categoria = ?,
                   data_transacao = ?, fonte = ?, confidence = ?, needs_review = ?, extra_json = ?
                   WHERE id = ? AND user_id = ?""",
                (tipo, valor, descricao, descricao, categoria, data_emissao, "nota", 100, 0, json.dumps({"linked": True, "note_id": note_id}), existing["id"], user_id),
            )
        else:
            db.execute(
                """INSERT INTO transacoes
                   (user_id, nota_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte, confidence, needs_review, extra_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                payload,
            )


def create_note_and_link_transaction(user_id: int, form, files, existing_note=None):
    descricao = sanitize_text(form.get("descricao"))
    valor = parse_money(form.get("valor"))
    data_emissao = form.get("data_emissao") or None
    categoria = sanitize_text(form.get("categoria")) or "Outros"
    if categoria not in NOTE_CATEGORIES:
        categoria = auto_category(descricao)
    cliente = sanitize_text(form.get("cliente")) or None
    cnpj_emitente = normalize_digits(form.get("cnpj_emitente")) or None
    numero_nota = sanitize_text(form.get("numero_nota")) or None
    status = sanitize_text(form.get("status")) or "Autorizada"
    tipo = sanitize_text(form.get("tipo")) or "saida"
    if tipo not in {"entrada", "saida"}:
        tipo = "saida"
    arquivo = files.get("arquivo")
    arquivo_name = existing_note["arquivo"] if existing_note else None

    if not descricao or valor <= 0:
        return None, "Descrição e valor são obrigatórios."
    if data_emissao:
        try:
            datetime.strptime(data_emissao, "%Y-%m-%d")
        except ValueError:
            return None, "Data de emissão inválida."
    if cnpj_emitente and len(cnpj_emitente) != 14:
        return None, "CNPJ inválido. Use 14 dígitos."
    if arquivo and arquivo.filename:
        saved = save_uploaded_file(arquivo)
        if saved is False:
            return None, "Formato de arquivo inválido."
        if saved:
            if arquivo_name and arquivo_name != saved:
                remove_uploaded_file(arquivo_name)
            arquivo_name = saved

    note_data = {
        "descricao": descricao,
        "valor": valor,
        "data_emissao": data_emissao,
        "categoria": categoria,
        "cliente": cliente,
        "cnpj_emitente": cnpj_emitente,
        "numero_nota": numero_nota,
        "status": status,
        "tipo": tipo,
        "arquivo": arquivo_name,
    }

    with get_db() as db:
        if existing_note:
            db.execute(
                """UPDATE notas SET descricao = ?, valor = ?, arquivo = ?, data_emissao = ?, categoria = ?,
                   cliente = ?, cnpj_emitente = ?, numero_nota = ?, status = ?, tipo = ?
                   WHERE id = ? AND user_id = ?""",
                (
                    descricao,
                    valor,
                    arquivo_name,
                    data_emissao,
                    categoria,
                    cliente,
                    cnpj_emitente,
                    numero_nota,
                    status,
                    tipo,
                    existing_note["id"],
                    user_id,
                ),
            )
            note_id = existing_note["id"]
        else:
            cur = db.execute(
                """INSERT INTO notas
                   (user_id, descricao, valor, arquivo, data_emissao, categoria, cliente, cnpj_emitente, numero_nota, status, tipo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, descricao, valor, arquivo_name, data_emissao, categoria, cliente, cnpj_emitente, numero_nota, status, tipo),
            )
            note_id = cur.lastrowid

    sync_note_transaction(user_id, note_id, descricao, valor, categoria, tipo, data_emissao)
    return note_data, None


def note_for_user(note_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM notas WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()


def transaction_for_user(tx_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM transacoes WHERE id = ? AND user_id = ?", (tx_id, user_id)).fetchone()


def goal_for_user(goal_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM metas WHERE id = ? AND user_id = ?", (goal_id, user_id)).fetchone()


def commitment_for_user(commitment_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM compromissos WHERE id = ? AND user_id = ?", (commitment_id, user_id)).fetchone()


def client_for_user(client_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM clientes WHERE id = ? AND user_id = ?", (client_id, user_id)).fetchone()


def service_for_user(service_id: int, user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM servicos WHERE id = ? AND user_id = ?", (service_id, user_id)).fetchone()


# ------------------------
# Painel MEI: faturamento, limite anual, DAS e DASN-SIMEI
# ------------------------
def calc_mei_faturamento(user_id: int, year: int) -> float:
    """Faturamento MEI = soma das notas emitidas (tipo entrada) no ano.
    Usa notas, não todas as 'entradas' de transações — um PIX de presente
    da mãe não é faturamento, uma nota fiscal emitida é."""
    with get_db() as db:
        row = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total FROM notas
               WHERE user_id = ? AND tipo = 'entrada'
                 AND strftime('%Y', COALESCE(data_emissao, data_upload)) = ?""",
            (user_id, str(year)),
        ).fetchone()
    return float(row["total"] or 0)


def mei_das_status(user_id: int, year: int) -> dict[str, Any]:
    with get_db() as db:
        pagos = db.execute(
            """SELECT COUNT(*) AS n FROM compromissos
               WHERE user_id = ? AND descricao = 'DAS-MEI' AND status = 'pago'
                 AND strftime('%Y', vencimento) = ?""",
            (user_id, str(year)),
        ).fetchone()["n"]
        proximo = db.execute(
            """SELECT * FROM compromissos
               WHERE user_id = ? AND descricao = 'DAS-MEI' AND status = 'pendente'
               ORDER BY date(vencimento) ASC LIMIT 1""",
            (user_id,),
        ).fetchone()
    return {"pagos": pagos, "proximo": proximo}


def calculate_business_summary(user_id: int):
    today = date.today()
    month_start, month_end = month_bounds(today)
    with get_db() as db:
        notes_in = db.execute(
            """SELECT * FROM notas
               WHERE user_id = ? AND tipo = 'entrada'
               ORDER BY datetime(data_upload) DESC""",
            (user_id,),
        ).fetchall()
        revenue_month = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'entrada'
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        expenses_month = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida'
               AND date(COALESCE(data_transacao, created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        status_counts = db.execute(
            "SELECT status, COUNT(*) AS count FROM notas WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
        top_clients = db.execute(
            """SELECT COALESCE(cliente, 'Não informado') AS cliente,
                      COUNT(*) AS count,
                      COALESCE(SUM(valor), 0) AS total
               FROM notas
               WHERE user_id = ? AND tipo = 'entrada'
               GROUP BY cliente
               ORDER BY total DESC
               LIMIT 6""",
            (user_id,),
        ).fetchall()
        clients = db.execute(
            "SELECT * FROM clientes WHERE user_id = ? ORDER BY datetime(created_at) DESC",
            (user_id,),
        ).fetchall()
        services = db.execute(
            "SELECT * FROM servicos WHERE user_id = ? ORDER BY datetime(created_at) DESC",
            (user_id,),
        ).fetchall()
        commitments = db.execute(
            "SELECT * FROM compromissos WHERE user_id = ? ORDER BY date(vencimento) ASC",
            (user_id,),
        ).fetchall()

    lucro = float(revenue_month or 0) - float(expenses_month or 0)
    pending_notes = len([n for n in notes_in if (n["status"] or "").lower() != "autorizada"])
    due_soon = [
        c for c in commitments
        if c["status"] == "pendente" and c["vencimento"] and date.fromisoformat(c["vencimento"]) <= date.today() + timedelta(days=7)
    ]
    return {
        "notes_in": notes_in,
        "revenue_month": float(revenue_month or 0),
        "expenses_month": float(expenses_month or 0),
        "lucro": lucro,
        "status_counts": status_counts,
        "top_clients": top_clients,
        "clients": clients,
        "services": services,
        "commitments": commitments,
        "pending_notes": pending_notes,
        "due_soon": due_soon,
    }


# ------------------------
# Jinja
# ------------------------
app.jinja_env.filters["money"] = money
app.jinja_env.filters["format_date"] = format_date
app.jinja_env.filters["month_label"] = month_label
app.jinja_env.globals["csrf_token"] = generate_csrf_token
app.jinja_env.globals["profile_choices"] = PROFILE_CHOICES
app.jinja_env.globals["focus_choices"] = HOME_FOCUS_CHOICES
app.jinja_env.globals["notification_choices"] = NOTIFICATION_CHOICES
app.jinja_env.globals["transaction_types"] = TRANSACTION_TYPES
app.jinja_env.globals["note_categories"] = NOTE_CATEGORIES
app.jinja_env.globals["transaction_categories"] = TRANSACTION_CATEGORIES
app.jinja_env.globals["income_categories"] = INCOME_CATEGORIES
app.jinja_env.globals["plan_labels"] = PLAN_LABELS

app.jinja_env.globals["date"] = date


@app.before_request
def csrf_protect():
    if request.method == "POST":
        exempt = request.endpoint in {"logout", "api_captura", "api_token"}
        token = request.form.get("csrf_token", "")
        if not exempt and token != session.get("csrf_token"):
            flash("Token de segurança inválido. Atualize a página e tente novamente.")
            return redirect(request.referrer or url_for("home" if "user_id" in session else "login"))


# ------------------------
# Bootstrap
# ------------------------
init_db()


# ------------------------
# Auth
# ------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        nome = sanitize_text(request.form.get("nome"))
        email = sanitize_text(request.form.get("email")).lower()
        senha = request.form.get("senha", "")
        perfil = normalize_profile(request.form.get("perfil"))
        view_mode = request.form.get("view_mode", "completo")
        if view_mode not in {"simples", "completo"}:
            view_mode = "completo"
        if not nome or not email or not senha:
            flash("Preencha todos os campos.")
            return redirect(url_for("register"))
        if len(senha) < 6:
            flash("A senha precisa ter pelo menos 6 caracteres.")
            return redirect(url_for("register"))
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO usuarios (nome, email, senha, perfil, view_mode) VALUES (?, ?, ?, ?, ?)",
                    (nome, email, generate_password_hash(senha), perfil, view_mode),
                )
            flash("Conta criada com sucesso. Faça login.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Esse e-mail já está cadastrado.")
            return redirect(url_for("register"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = sanitize_text(request.form.get("email")).lower()
        senha = request.form.get("senha", "")
        with get_db() as db:
            user = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["senha"], senha):
            _start_session(user)
            return redirect(url_for("home"))
        flash("E-mail ou senha inválidos.")
        return redirect(url_for("login"))
    return render_template("login.html", google_login_enabled=oauth is not None)


def _start_session(user) -> None:
    session.permanent = True
    session["user_id"] = user["id"]
    session["nome"] = user["nome"]
    session["perfil"] = user["perfil"]
    session["home_focus"] = user["home_focus"]
    session["notification_mode"] = user["notification_mode"]
    session["meta_mensal"] = user["meta_mensal"]
    session["view_mode"] = (user["view_mode"] if "view_mode" in user.keys() else "completo") or "completo"


@app.route("/login/google")
def google_login():
    if oauth is None:
        flash("Login com Google não está configurado neste servidor.")
        return redirect(url_for("login"))
    # Marca que este login começou dentro do app Android, para o callback
    # devolver o controle pro app em vez de continuar no navegador.
    if request.args.get("from_app"):
        session["login_origin_app"] = True
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if oauth is None:
        return redirect(url_for("login"))
    try:
        token = oauth.google.authorize_access_token()
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").lower().strip()
        nome = sanitize_text(info.get("name")) or email.split("@")[0]
    except Exception:
        flash("Não deu certo entrar com o Google. Tente de novo ou use e-mail e senha.")
        return redirect(url_for("login"))

    if not email:
        flash("O Google não informou seu e-mail. Use e-mail e senha.")
        return redirect(url_for("login"))

    with get_db() as db:
        user = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if not user:
            # Conta nova via Google: senha aleatória (dá para definir uma depois nas configurações)
            db.execute(
                "INSERT INTO usuarios (nome, email, senha, perfil) VALUES (?, ?, ?, 'pf')",
                (nome, email, generate_password_hash(secrets.token_hex(16))),
            )
            user = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
    _start_session(user)

    if session.pop("login_origin_app", False):
        # Veio do app: devolve o controle pro app Android (App Link) com o
        # token de captura e um código de uma vez para autenticar o WebView.
        capture_token = get_or_create_capture_token(user)
        auto_code = create_auto_login_code(user["id"])
        return redirect(url_for("app_entrou", token=capture_token, code=auto_code))
    return redirect(url_for("home"))


@app.route("/app/entrou")
def app_entrou():
    """Alvo do App Link do app Android: o Chrome (Custom Tab) intercepta esta
    navegação e entrega para o app. Se alguém chegar aqui direto num
    navegador normal (app link não capturado), mostra uma página simples."""
    return render_template("app_entrou.html")


@app.route("/entrar-automatico")
def entrar_automatico():
    """O WebView do app chama esta rota com o código de uso único recebido
    via App Link, para virar uma sessão de verdade dentro do WebView."""
    code = request.args.get("code", "")
    user = redeem_auto_login_code(code)
    if not user:
        flash("Esse link de entrada expirou. Entre novamente.")
        return redirect(url_for("login"))
    _start_session(user)
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------
# Home / dashboard
# ------------------------
@app.route("/")
@login_required
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    profile = user_profile(user)
    stats = calc_transaction_totals(user["id"])
    focus = normalize_focus(user["home_focus"])
    recommended = compute_recommended_focus(stats)
    business = calculate_business_summary(user["id"]) if is_business_profile(profile) else None

    goal = stats["current_goal"]
    current_month = month_label(date.today().strftime("%Y-%m"))
    note_pending = len([n for n in stats["notes"] if (n["status"] or "").lower() != "autorizada"])
    all_clear = (
        len(stats["overdue_commitments"]) == 0
        and len(stats["due_soon_commitments"]) == 0
        and note_pending == 0
        and stats["balance"] >= 0
    )
    status_phrase = "Você está com tudo em dia" if all_clear else "Temos algumas coisas para resolver"
    status_emoji = "🟢" if all_clear else "🟡"
    can_spend_today = stats["available_today"]
    next_commitment = stats["upcoming_commitments"][0] if stats["upcoming_commitments"] else None

    # Pergunta inteligente: gastos repetidos que o Hércules ainda não entende
    suggestions = pending_suggestions(user["id"])

    # Modo simples: 3 frases (tudo em dia / gastou hoje / projeção do fim do mês)
    today_iso = date.today().isoformat()
    with get_db() as db:
        today_spent = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND date(COALESCE(data_transacao, created_at)) = date(?)""",
            (user["id"], today_iso),
        ).fetchone()["total"]
        today_txs = db.execute(
            """SELECT * FROM transacoes
               WHERE user_id = ? AND date(COALESCE(data_transacao, created_at)) = date(?)
                 AND fonte != 'ajuste'
               ORDER BY id DESC LIMIT 8""",
            (user["id"], today_iso),
        ).fetchall()
        checkin_done = db.execute(
            "SELECT 1 FROM checkins WHERE user_id = ? AND dia = ?",
            (user["id"], today_iso),
        ).fetchone() is not None
        tx_count = db.execute(
            "SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ?", (user["id"],)
        ).fetchone()["n"]

    pendentes = []
    for cap in pending_captures(user["id"]):
        try:
            dados = json.loads(cap["dados_extraidos"] or "{}")
        except (TypeError, ValueError):
            dados = {}
        pendentes.append({
            "id": cap["id"],
            "conteudo": cap["conteudo"],
            "valor": dados.get("valor") or "",
            "tipo": dados.get("tipo") or "saida",
        })
    streak = checkin_streak(user["id"])
    onboarding = tx_count == 0

    # Uma dica do Herc por vez — a mais relevante primeiro
    herc_tip = None
    if not onboarding:
        with get_db() as db:
            tem_captura = db.execute(
                "SELECT 1 FROM transacoes WHERE user_id = ? AND fonte = 'notificacao' LIMIT 1",
                (user["id"],),
            ).fetchone() is not None
        if tem_captura and not tip_seen(user["id"], "primeira_captura"):
            herc_tip = "primeira_captura"
        elif not tip_seen(user["id"], "registro_rapido"):
            herc_tip = "registro_rapido"
    # Texto compartilhado do WhatsApp (share_target do PWA) pré-preenche o registro rápido
    shared_text = sanitize_text(request.args.get("texto") or request.args.get("title"))[:200]
    avg_daily_spend = stats["month_expenses"] / max(1, date.today().day)
    projected_end = stats["balance"] - (avg_daily_spend * (days_left_in_month() - 1))
    view_mode = (user["view_mode"] if "view_mode" in user.keys() else "completo") or "completo"

    dias_ofx = dias_desde_ultimo_ofx(user)
    lembrar_ofx = (not onboarding) and (dias_ofx is None or dias_ofx >= OFX_LEMBRETE_DIAS)

    session["last_balance"] = money(stats["balance"])
    session["meta_mensal"] = user["meta_mensal"]
    focus_labels = dict(HOME_FOCUS_CHOICES)
    return render_template(
        "home.html",
        suggestions=suggestions,
        suggestion_categories=expense_category_names(user["id"]),
        today_spent=float(today_spent or 0),
        projected_end=float(projected_end),
        view_mode=view_mode,
        today_txs=today_txs,
        pendentes=pendentes,
        checkin_done=checkin_done,
        streak=streak,
        onboarding=onboarding,
        shared_text=shared_text,
        herc_tip=herc_tip,
        herc_tip_text=HERC_TIPS.get(herc_tip),
        lembrar_ofx=lembrar_ofx,
        dias_ofx=dias_ofx,
        user=user,
        profile=profile,
        focus=focus,
        focus_label=focus_labels,
        recommended_focus=recommended,
        stats=stats,
        business=business,
        status_phrase=status_phrase,
        status_emoji=status_emoji,
        all_clear=all_clear,
        can_spend_today=can_spend_today,
        next_commitment=next_commitment,
        current_month=current_month,
        goal=goal,
        note_pending=note_pending,
    )


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    profile = user_profile(user)
    stats = calc_transaction_totals(user["id"])
    labels = [row["categoria"] for row in stats["monthly_by_category"]]
    values = [row["total"] or 0 for row in stats["monthly_by_category"]]
    return render_template(
        "dashboard.html",
        user=user,
        profile=profile,
        stats=stats,
        labels=labels,
        values=values,
        month=month_label(date.today().strftime("%Y-%m")),
    )


@app.route("/business-dashboard")
@login_required
def business_dashboard():
    user = current_user()
    profile = user_profile(user)
    if not is_business_profile(profile):
        flash("Este painel é voltado para MEI/lojista.")
        return redirect(url_for("home"))
    business = calculate_business_summary(user["id"])
    return render_template(
        "business_dashboard.html",
        user=user,
        profile=profile,
        business=business,
        month=month_label(date.today().strftime("%Y-%m")),
    )


@app.route("/mei")
@login_required
def painel_mei():
    user = current_user()
    profile = user_profile(user)
    if not is_business_profile(profile):
        flash("O Painel MEI é para quem tem perfil MEI, lojista ou híbrido.")
        return redirect(url_for("home"))

    today = date.today()
    faturamento_atual = calc_mei_faturamento(user["id"], today.year)
    faturamento_anterior = calc_mei_faturamento(user["id"], today.year - 1)
    pct = min(100.0, (faturamento_atual / MEI_LIMITE_ANUAL) * 100.0) if MEI_LIMITE_ANUAL else 0.0
    projecao = (faturamento_atual / today.month) * 12 if today.month else faturamento_atual
    das = mei_das_status(user["id"], today.year)
    das_valor = float(user["das_valor"] or 0) if "das_valor" in user.keys() else 0.0

    return render_template(
        "mei.html",
        user=user,
        faturamento_atual=faturamento_atual,
        faturamento_anterior=faturamento_anterior,
        limite=MEI_LIMITE_ANUAL,
        pct=pct,
        projecao=projecao,
        das=das,
        das_valor=das_valor,
        ano_atual=today.year,
        ano_anterior=today.year - 1,
        is_january=(today.month == 1),
    )


@app.route("/mei/das/ativar", methods=["POST"])
@login_required
def ativar_das():
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    dia = request.form.get("dia", type=int) or 20
    dia = max(1, min(28, dia))
    if valor <= 0:
        flash("Informe o valor do seu DAS-MEI (está no carnê ou no app do Simples Nacional).")
        return redirect(url_for("painel_mei"))

    with get_db() as db:
        db.execute("UPDATE usuarios SET das_valor = ? WHERE id = ?", (valor, user["id"]))
        existing = db.execute(
            "SELECT id FROM compromissos WHERE user_id = ? AND descricao = 'DAS-MEI' AND status = 'pendente'",
            (user["id"],),
        ).fetchone()
        if existing:
            db.execute("UPDATE compromissos SET valor = ? WHERE id = ?", (valor, existing["id"]))
            flash("Valor do DAS-MEI atualizado.")
        else:
            today = date.today()
            last_day = calendar.monthrange(today.year, today.month)[1]
            venc_dia = min(dia, last_day)
            proximo = date(today.year, today.month, venc_dia)
            if proximo < today:
                ano = today.year + (1 if today.month == 12 else 0)
                mes = 1 if today.month == 12 else today.month + 1
                venc_dia = min(dia, calendar.monthrange(ano, mes)[1])
                proximo = date(ano, mes, venc_dia)
            db.execute(
                """INSERT INTO compromissos (user_id, descricao, valor, vencimento, tipo, status, recorrente, frequencia)
                   VALUES (?, 'DAS-MEI', ?, ?, 'saida', 'pendente', 1, 'mensal')""",
                (user["id"], valor, proximo.isoformat()),
            )
            flash("DAS-MEI ativado! O Hércules vai lembrar você todo mês, e a próxima parcela nasce sozinha quando você marcar a atual como paga.")
    return redirect(url_for("painel_mei"))


@app.route("/mei/dossie")
@login_required
def dossie_mei():
    user = current_user()
    year = request.args.get("year", str(date.today().year))
    with get_db() as db:
        notes = db.execute(
            """SELECT * FROM notas WHERE user_id = ?
               AND strftime('%Y', COALESCE(data_emissao, data_upload)) = ?
               ORDER BY COALESCE(data_emissao, data_upload) ASC""",
            (user["id"], year),
        ).fetchall()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["descricao", "valor", "tipo", "categoria", "cliente", "cnpj_emitente",
                          "numero_nota", "status", "data_emissao", "data_upload"])
        for note in notes:
            writer.writerow([
                note["descricao"],
                f"{float(note['valor']):.2f}",
                note["tipo"],
                note["categoria"],
                note["cliente"] or "",
                note["cnpj_emitente"] or "",
                note["numero_nota"] or "",
                note["status"] or "",
                note["data_emissao"] or "",
                note["data_upload"] or "",
            ])
        zf.writestr(f"notas_{year}.csv", csv_buffer.getvalue())

        for note in notes:
            if note["arquivo"]:
                file_path = UPLOAD_DIR / note["arquivo"]
                if file_path.exists():
                    zf.write(file_path, arcname=f"anexos/{note['id']}_{note['arquivo']}")

    buffer.seek(0)
    filename = f"dossie_mei_{year}.zip"
    return Response(
        buffer.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ------------------------
# Settings / preferences
# ------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    if request.method == "POST":
        form_kind = request.form.get("form_kind", "preferences")

        if form_kind == "account":
            nome = sanitize_text(request.form.get("nome"))
            email = sanitize_text(request.form.get("email")).lower()
            if not nome or not email:
                flash("Preencha nome e e-mail.")
                return redirect(url_for("settings"))
            try:
                with get_db() as db:
                    db.execute("UPDATE usuarios SET nome = ?, email = ? WHERE id = ?", (nome, email, user["id"]))
                session["nome"] = nome
                flash("Dados pessoais atualizados.")
            except sqlite3.IntegrityError:
                flash("Esse e-mail já está em uso por outra conta.")
            return redirect(url_for("settings"))

        if form_kind == "password":
            senha_atual = request.form.get("senha_atual", "")
            nova_senha = request.form.get("nova_senha", "")
            confirmar = request.form.get("confirmar_senha", "")
            if not check_password_hash(user["senha"], senha_atual):
                flash("Senha atual incorreta.")
                return redirect(url_for("settings"))
            if len(nova_senha) < 6:
                flash("A nova senha precisa ter pelo menos 6 caracteres.")
                return redirect(url_for("settings"))
            if nova_senha != confirmar:
                flash("A confirmação não bate com a nova senha.")
                return redirect(url_for("settings"))
            with get_db() as db:
                db.execute("UPDATE usuarios SET senha = ? WHERE id = ?", (generate_password_hash(nova_senha), user["id"]))
            flash("Senha alterada com sucesso.")
            return redirect(url_for("settings"))

        perfil = normalize_profile(request.form.get("perfil"))
        home_focus = normalize_focus(request.form.get("home_focus"))
        notification_mode = normalize_notification_mode(request.form.get("notification_mode"))
        meta_mensal = parse_money(request.form.get("meta_mensal"))
        view_mode = request.form.get("view_mode", "completo")
        if view_mode not in {"simples", "completo"}:
            view_mode = "completo"
        with get_db() as db:
            db.execute(
                """UPDATE usuarios SET perfil = ?, home_focus = ?, notification_mode = ?, meta_mensal = ?, view_mode = ?
                   WHERE id = ?""",
                (perfil, home_focus, notification_mode, meta_mensal, view_mode, user["id"]),
            )
        session["perfil"] = perfil
        session["home_focus"] = home_focus
        session["notification_mode"] = notification_mode
        session["meta_mensal"] = meta_mensal
        session["view_mode"] = view_mode
        flash("Preferências atualizadas.")
        return redirect(url_for("settings"))

    goals_count = 0
    commitments_count = 0
    with get_db() as db:
        goals_count = db.execute("SELECT COUNT(*) AS count FROM metas WHERE user_id = ?", (user["id"],)).fetchone()["count"]
        commitments_count = db.execute("SELECT COUNT(*) AS count FROM compromissos WHERE user_id = ?", (user["id"],)).fetchone()["count"]
    capture_token = get_or_create_capture_token(user)

    return render_template(
        "settings.html",
        user=user,
        goals_count=goals_count,
        commitments_count=commitments_count,
        plan_label=PLAN_LABELS["free"],
        capture_token=capture_token,
        capture_url=url_for("api_captura", _external=True),
    )


@app.route("/settings/regenerar-token", methods=["POST"])
@login_required
def regenerar_token():
    user = current_user()
    novo = secrets.token_urlsafe(24)
    with get_db() as db:
        db.execute("UPDATE usuarios SET capture_token = ? WHERE id = ?", (novo, user["id"]))
    flash("Token novo gerado. Atualize o MacroDroid com ele.")
    return redirect(url_for("settings"))


# ------------------------
# Goals
# ------------------------
@app.route("/metas", methods=["GET", "POST"])
@login_required
def metas():
    user = current_user()
    if request.method == "POST":
        nome = sanitize_text(request.form.get("nome"))
        meta_valor = parse_money(request.form.get("meta_valor"))
        valor_atual = parse_money(request.form.get("valor_atual"))
        prazo = request.form.get("prazo") or None
        if prazo:
            try:
                date.fromisoformat(prazo)
            except ValueError:
                prazo = None
        if not nome or meta_valor <= 0:
            flash("Informe um nome e um valor de meta válidos.")
            return redirect(url_for("metas"))
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO metas (user_id, nome, meta_valor, valor_atual, ativo, prazo) VALUES (?, ?, ?, ?, 1, ?)",
                (user["id"], nome, meta_valor, valor_atual, prazo),
            )
            new_id = cursor.lastrowid
        flash("Meta criada.")
        return redirect(url_for("metas", novo=new_id))

    with get_db() as db:
        goals = db.execute("SELECT * FROM metas WHERE user_id = ? ORDER BY ativo DESC, created_at DESC", (user["id"],)).fetchall()
    stats = calc_transaction_totals(user["id"])
    goals_view = []
    for g in goals:
        missing = max(0.0, float(g["meta_valor"]) - float(g["valor_atual"]))
        m_left = months_until(g["prazo"]) if g["prazo"] else None
        monthly = (missing / m_left) if (m_left and missing > 0) else None
        goals_view.append({"row": g, "missing": missing, "months_left": m_left, "monthly": monthly})
    novo = request.args.get("novo", type=int)
    return render_template("metas.html", user=user, goals=goals_view, stats=stats, novo=novo)


@app.route("/metas/<int:goal_id>/editar", methods=["POST"])
@login_required
def editar_meta(goal_id):
    user = current_user()
    goal = goal_for_user(goal_id, user["id"])
    if not goal:
        flash("Meta não encontrada.")
        return redirect(url_for("metas"))
    nome = sanitize_text(request.form.get("nome")) or goal["nome"]
    meta_valor = parse_money(request.form.get("meta_valor"))
    valor_atual = parse_money(request.form.get("valor_atual"))
    prazo = request.form.get("prazo") or None
    if prazo:
        try:
            date.fromisoformat(prazo)
        except ValueError:
            prazo = goal["prazo"]
    if meta_valor <= 0:
        meta_valor = float(goal["meta_valor"])
    with get_db() as db:
        db.execute(
            "UPDATE metas SET nome = ?, meta_valor = ?, valor_atual = ?, prazo = ? WHERE id = ? AND user_id = ?",
            (nome, meta_valor, valor_atual, prazo, goal_id, user["id"]),
        )
    flash("Meta atualizada.")
    return redirect(url_for("metas", novo=goal_id))


@app.route("/metas/<int:goal_id>/aporte", methods=["POST"])
@login_required
def aporte_meta(goal_id):
    user = current_user()
    goal = goal_for_user(goal_id, user["id"])
    if not goal:
        flash("Meta não encontrada.")
        return redirect(url_for("metas"))
    valor = parse_money(request.form.get("valor"))
    if valor <= 0:
        flash("Informe um valor válido para guardar.")
        return redirect(url_for("metas"))
    with get_db() as db:
        db.execute(
            "UPDATE metas SET valor_atual = valor_atual + ? WHERE id = ? AND user_id = ?",
            (valor, goal_id, user["id"]),
        )
        db.execute(
            """INSERT INTO transacoes (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte)
               VALUES (?, 'saida', ?, ?, ?, 'Reserva', ?, 'manual')""",
            (user["id"], valor, f"Guardado na meta: {goal['nome']}", "Reserva", date.today().isoformat()),
        )
    flash(f"Você guardou {money(valor)} na meta {goal['nome']}.")
    return redirect(url_for("metas", novo=goal_id))


@app.route("/metas/<int:goal_id>/toggle", methods=["POST"])
@login_required
def toggle_goal(goal_id):
    user = current_user()
    goal = goal_for_user(goal_id, user["id"])
    if not goal:
        flash("Meta não encontrada.")
        return redirect(url_for("metas"))
    with get_db() as db:
        db.execute("UPDATE metas SET ativo = CASE WHEN ativo = 1 THEN 0 ELSE 1 END WHERE id = ? AND user_id = ?", (goal_id, user["id"]))
    flash("Meta atualizada.")
    return redirect(url_for("metas"))


@app.route("/metas/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    user = current_user()
    goal = goal_for_user(goal_id, user["id"])
    if not goal:
        flash("Meta não encontrada.")
        return redirect(url_for("metas"))
    with get_db() as db:
        db.execute("DELETE FROM metas WHERE id = ? AND user_id = ?", (goal_id, user["id"]))
    flash("Meta removida.")
    return redirect(url_for("metas"))


# ------------------------
# Captura automática (notificações do banco via app companion Android/MacroDroid)
# ------------------------
@app.route("/api/token", methods=["POST"])
def api_token():
    """Login do app companion via e-mail+senha (usuários que não usam Google)."""
    payload = request.get_json(silent=True) or request.form
    email = sanitize_text(payload.get("email")).lower()
    senha = payload.get("senha") or payload.get("password") or ""
    if not email or not senha:
        return {"erro": "e-mail e senha são obrigatórios"}, 400
    with get_db() as db:
        user = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
    if not user or not check_password_hash(user["senha"], senha):
        return {"erro": "e-mail ou senha inválidos"}, 401
    capture_token = get_or_create_capture_token(user)
    return {"ok": True, "token": capture_token, "nome": user["nome"]}, 200


@app.route("/api/meu-token")
@login_required
def api_meu_token():
    """Token do app companion para quem JÁ está logado no navegador/WebView
    (cobre login por Google, que não tem senha para digitar no app).
    O app Android lê o cookie de sessão do WebView e chama esta rota com ele."""
    user = current_user()
    capture_token = get_or_create_capture_token(user)
    return {"ok": True, "token": capture_token, "nome": user["nome"]}, 200


@app.route("/api/captura", methods=["POST"])
def api_captura():
    payload = request.get_json(silent=True) or request.form
    token = sanitize_text(payload.get("token"))
    texto = payload.get("texto") or payload.get("text") or ""
    if not token:
        return {"erro": "token ausente"}, 401
    with get_db() as db:
        user = db.execute("SELECT id FROM usuarios WHERE capture_token = ?", (token,)).fetchone()
    if not user:
        return {"erro": "token inválido"}, 401
    if not sanitize_text(texto):
        return {"erro": "texto vazio"}, 400
    result = register_capture(user["id"], texto, origem="notificacao")
    return result, 200


@app.route("/registro-rapido", methods=["POST"])
@login_required
def registro_rapido():
    user = current_user()
    texto = request.form.get("texto", "")
    if not sanitize_text(texto):
        flash("Me conta o que aconteceu — ex.: gastei 12 na quentinha.")
        return redirect(url_for("home"))
    result = register_capture(user["id"], texto, origem="manual")
    if result["status"] == "lancada":
        rotulo = "Entrada" if result["tipo"] == "entrada" else "Saída"
        flash(f"Anotei! {rotulo} de {money(result['valor'])} em {result['estabelecimento']} ({result['categoria']}).")
    elif result["status"] == "duplicada":
        flash("Esse eu já tinha anotado agorinha. 😉")
    else:
        flash("Não entendi direito — deixei nas pendências para você confirmar.")
    return redirect(url_for("home"))


@app.route("/checkin", methods=["POST"])
@login_required
def fechar_dia():
    user = current_user()
    today_iso = date.today().isoformat()
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO checkins (user_id, dia) VALUES (?, ?)",
            (user["id"], today_iso),
        )
    streak = checkin_streak(user["id"])
    if streak >= 2:
        flash(f"Dia fechado! 🔥 {streak} dias seguidos em dia com o Herc.")
    else:
        flash("Dia fechado! Até amanhã. 🦁")
    return redirect(url_for("home"))


@app.route("/capturas/<int:captura_id>/descartar", methods=["POST"])
@login_required
def descartar_captura(captura_id):
    user = current_user()
    with get_db() as db:
        db.execute(
            "UPDATE capturas SET status = 'descartada' WHERE id = ? AND user_id = ?",
            (captura_id, user["id"]),
        )
    flash("Captura descartada.")
    return redirect(url_for("home"))


@app.route("/trabalhos")
@login_required
def trabalhos():
    user = current_user()
    novos = evaluate_trabalhos(user["id"])
    for key in novos:
        t = next(x for x in TRABALHOS if x["key"] == key)
        flash(f"🏆 Trabalho concluído: {t['emoji']} {t['nome']}!")
    with get_db() as db:
        rows = db.execute(
            "SELECT trabalho, conquistado_em FROM trabalhos WHERE user_id = ?", (user["id"],)
        ).fetchall()
    conquistados = {r["trabalho"]: r["conquistado_em"] for r in rows}
    return render_template(
        "trabalhos.html",
        user=user,
        trabalhos=TRABALHOS,
        conquistados=conquistados,
        total=len(conquistados),
    )


@app.route("/dicas/<key>/vista", methods=["POST"])
@login_required
def marcar_dica(key):
    user = current_user()
    if key in HERC_TIPS:
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO dicas_vistas (user_id, dica) VALUES (?, ?)",
                (user["id"], key),
            )
    return redirect(request.referrer or url_for("home"))


@app.route("/saldo-inicial", methods=["POST"])
@login_required
def saldo_inicial():
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    if valor <= 0:
        flash("Me diz quanto você tem na conta hoje (pode ser aproximado).")
        return redirect(url_for("home"))
    with get_db() as db:
        db.execute(
            """INSERT INTO transacoes (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte)
               VALUES (?, 'entrada', ?, 'Saldo inicial', 'Saldo inicial', 'Outros', ?, 'ajuste')""",
            (user["id"], valor, date.today().isoformat()),
        )
    flash(f"Perfeito! Seu saldo de {money(valor)} está registrado. Agora é comigo. 🦁")
    return redirect(url_for("home"))


# ------------------------
# Categories & rules ("ensinar o Hércules")
# ------------------------
@app.route("/categorias", methods=["GET", "POST"])
@login_required
def categorias():
    user = current_user()
    if request.method == "POST":
        nome = sanitize_text(request.form.get("nome"))[:40]
        icone = sanitize_text(request.form.get("icone"))[:4] or None
        limite = max(0.0, parse_money(request.form.get("limite_mensal")))
        if not nome:
            flash("Dê um nome para a categoria.")
            return redirect(url_for("categorias"))
        with get_db() as db:
            existing = db.execute(
                "SELECT id FROM categorias WHERE user_id = ? AND nome = ? COLLATE NOCASE",
                (user["id"], nome),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE categorias SET icone = COALESCE(?, icone), limite_mensal = ? WHERE id = ?",
                    (icone, limite, existing["id"]),
                )
                flash(f"Categoria {nome} atualizada.")
            else:
                db.execute(
                    "INSERT INTO categorias (user_id, nome, icone, limite_mensal) VALUES (?, ?, ?, ?)",
                    (user["id"], nome, icone, limite),
                )
                flash(f"Categoria {nome} criada.")
        return redirect(url_for("categorias"))

    spending = category_month_spending(user["id"])
    customs = []
    for cat in user_categories(user["id"]):
        gasto = spending.get(cat["nome"], 0.0)
        limite = float(cat["limite_mensal"] or 0)
        pct = min(100.0, (gasto / limite) * 100.0) if limite > 0 else None
        customs.append({"row": cat, "gasto": gasto, "limite": limite, "pct": pct})

    fixed = [
        {"nome": nome, "gasto": spending.get(nome, 0.0)}
        for nome in TRANSACTION_CATEGORIES
        if spending.get(nome, 0.0) > 0
    ]
    rules = [r for r in user_rules(user["id"]) if r["categoria_nome"] != IGNORE_RULE]
    return render_template(
        "categorias.html",
        user=user,
        customs=customs,
        fixed=fixed,
        rules=rules,
        month=month_label(date.today().strftime("%Y-%m")),
    )


@app.route("/categorias/<int:cat_id>/delete", methods=["POST"])
@login_required
def delete_categoria(cat_id):
    user = current_user()
    with get_db() as db:
        cat = db.execute("SELECT * FROM categorias WHERE id = ? AND user_id = ?", (cat_id, user["id"])).fetchone()
        if not cat:
            flash("Categoria não encontrada.")
            return redirect(url_for("categorias"))
        db.execute("DELETE FROM categorias WHERE id = ? AND user_id = ?", (cat_id, user["id"]))
        db.execute(
            "DELETE FROM regras_categorizacao WHERE user_id = ? AND categoria_nome = ?",
            (user["id"], cat["nome"]),
        )
    flash(f"Categoria {cat['nome']} removida (as movimentações continuam lá).")
    return redirect(url_for("categorias"))


@app.route("/regras", methods=["POST"])
@login_required
def criar_regra():
    user = current_user()
    padrao = sanitize_text(request.form.get("padrao_texto"))[:80]
    acao = request.form.get("acao", "aplicar")
    categoria = (sanitize_text(request.form.get("nova_categoria")) or sanitize_text(request.form.get("categoria_nome")))[:40]
    destino = request.form.get("voltar") or url_for("home")
    # Só aceita caminhos internos (evita redirect para fora do app)
    if not destino.startswith("/") or destino.startswith("//"):
        destino = url_for("home")

    if not padrao:
        flash("Padrão vazio.")
        return redirect(destino)

    if acao == "ignorar":
        with get_db() as db:
            db.execute(
                "INSERT INTO regras_categorizacao (user_id, padrao_texto, categoria_nome, created_at) VALUES (?, ?, ?, datetime('now'))",
                (user["id"], padrao, IGNORE_RULE),
            )
        flash(f"Combinado, deixo '{padrao}' como está.")
        return redirect(destino)

    if not categoria:
        flash("Escolha ou digite uma categoria.")
        return redirect(destino)

    with get_db() as db:
        # Se a categoria é nova, nasce agora
        exists_custom = db.execute(
            "SELECT id FROM categorias WHERE user_id = ? AND nome = ? COLLATE NOCASE",
            (user["id"], categoria),
        ).fetchone()
        if not exists_custom and categoria not in TRANSACTION_CATEGORIES and categoria not in INCOME_CATEGORIES:
            db.execute(
                "INSERT INTO categorias (user_id, nome, limite_mensal) VALUES (?, ?, 0)",
                (user["id"], categoria),
            )
        db.execute(
            "INSERT INTO regras_categorizacao (user_id, padrao_texto, categoria_nome, created_at) VALUES (?, ?, ?, datetime('now'))",
            (user["id"], padrao, categoria),
        )
    changed = reclassify_transactions(user["id"], padrao, categoria)
    if changed:
        flash(f"Aprendi! '{padrao}' agora é {categoria} — {changed} movimentações reclassificadas.")
    else:
        flash(f"Aprendi! '{padrao}' agora é {categoria}.")
    return redirect(destino)


@app.route("/regras/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete_regra(rule_id):
    user = current_user()
    with get_db() as db:
        rule = db.execute(
            "SELECT * FROM regras_categorizacao WHERE id = ? AND user_id = ?", (rule_id, user["id"])
        ).fetchone()
        if not rule:
            flash("Regra não encontrada.")
            return redirect(url_for("categorias"))
        db.execute("DELETE FROM regras_categorizacao WHERE id = ? AND user_id = ?", (rule_id, user["id"]))
    flash("Regra removida. O Hércules desaprendeu essa.")
    return redirect(url_for("categorias"))


# ------------------------
# Commitments
# ------------------------
@app.route("/compromissos", methods=["GET", "POST"])
@login_required
def compromissos():
    user = current_user()
    if request.method == "POST":
        descricao = sanitize_text(request.form.get("descricao"))
        valor = parse_money(request.form.get("valor"))
        vencimento = request.form.get("vencimento")
        tipo = request.form.get("tipo", "saida")
        recorrente = 1 if request.form.get("recorrente") else 0
        frequencia = request.form.get("frequencia", "mensal")
        if not descricao or valor <= 0 or not vencimento:
            flash("Preencha descrição, valor e vencimento.")
            return redirect(url_for("compromissos"))
        try:
            date.fromisoformat(vencimento)
        except ValueError:
            flash("Data de vencimento inválida.")
            return redirect(url_for("compromissos"))
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO compromissos (user_id, descricao, valor, vencimento, tipo, status, recorrente, frequencia)
                   VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?)""",
                (user["id"], descricao, valor, vencimento, tipo if tipo in {"entrada", "saida"} else "saida", recorrente, frequencia),
            )
            new_id = cursor.lastrowid
        flash("Conta salva. Ela já aparece na sua lista.")
        return redirect(url_for("compromissos", novo=new_id))

    with get_db() as db:
        commitments = db.execute(
            "SELECT * FROM compromissos WHERE user_id = ? ORDER BY date(vencimento) ASC",
            (user["id"],),
        ).fetchall()
    return render_template("compromissos.html", user=user, commitments=commitments, novo=request.args.get("novo", type=int))


@app.route("/compromissos/<int:commitment_id>/toggle", methods=["POST"])
@login_required
def toggle_commitment(commitment_id):
    user = current_user()
    commitment = commitment_for_user(commitment_id, user["id"])
    if not commitment:
        flash("Compromisso não encontrado.")
        return redirect(url_for("compromissos"))
    new_status = "pago" if commitment["status"] == "pendente" else "pendente"
    with get_db() as db:
        db.execute("UPDATE compromissos SET status = ? WHERE id = ? AND user_id = ?", (new_status, commitment_id, user["id"]))

        # Conta recorrente paga: a próxima nasce sozinha
        if new_status == "pago" and commitment["recorrente"]:
            try:
                venc = date.fromisoformat(commitment["vencimento"])
            except (TypeError, ValueError):
                venc = date.today()
            freq = commitment["frequencia"] or "mensal"
            proximo = None
            if freq == "semanal":
                proximo = venc + timedelta(days=7)
            elif freq == "anual":
                proximo = venc.replace(year=venc.year + 1)
            elif freq == "mensal":
                ano = venc.year + (1 if venc.month == 12 else 0)
                mes = 1 if venc.month == 12 else venc.month + 1
                dia = min(venc.day, calendar.monthrange(ano, mes)[1])
                proximo = date(ano, mes, dia)
            if proximo:
                ja_existe = db.execute(
                    """SELECT 1 FROM compromissos
                       WHERE user_id = ? AND descricao = ? AND vencimento = ? AND status = 'pendente'""",
                    (user["id"], commitment["descricao"], proximo.isoformat()),
                ).fetchone()
                if not ja_existe:
                    db.execute(
                        """INSERT INTO compromissos (user_id, descricao, valor, vencimento, tipo, status, recorrente, frequencia)
                           VALUES (?, ?, ?, ?, ?, 'pendente', 1, ?)""",
                        (user["id"], commitment["descricao"], commitment["valor"], proximo.isoformat(), commitment["tipo"], freq),
                    )
                    flash(f"Conta paga! Já criei a próxima: {commitment['descricao']} em {format_date(proximo.isoformat())}.")
                    return redirect(url_for("compromissos"))
    flash("Compromisso atualizado.")
    return redirect(url_for("compromissos"))


@app.route("/compromissos/<int:commitment_id>/delete", methods=["POST"])
@login_required
def delete_commitment(commitment_id):
    user = current_user()
    commitment = commitment_for_user(commitment_id, user["id"])
    if not commitment:
        flash("Compromisso não encontrado.")
        return redirect(url_for("compromissos"))
    with get_db() as db:
        db.execute("DELETE FROM compromissos WHERE id = ? AND user_id = ?", (commitment_id, user["id"]))
    flash("Compromisso removido.")
    return redirect(url_for("compromissos"))


# ------------------------
# Clients & services
# ------------------------
@app.route("/clientes", methods=["GET", "POST"])
@login_required
def clientes():
    user = current_user()
    if request.method == "POST":
        nome = sanitize_text(request.form.get("nome"))
        documento = normalize_digits(request.form.get("documento"))
        email = sanitize_text(request.form.get("email"))
        telefone = sanitize_text(request.form.get("telefone"))
        if not nome:
            flash("Informe o nome do cliente.")
            return redirect(url_for("clientes"))
        with get_db() as db:
            db.execute(
                "INSERT INTO clientes (user_id, nome, documento, email, telefone) VALUES (?, ?, ?, ?, ?)",
                (user["id"], nome, documento or None, email or None, telefone or None),
            )
        flash("Cliente salvo.")
        return redirect(url_for("clientes"))
    with get_db() as db:
        rows = db.execute("SELECT * FROM clientes WHERE user_id = ? ORDER BY datetime(created_at) DESC", (user["id"],)).fetchall()
    return render_template("clientes.html", user=user, clients=rows)


@app.route("/clientes/<int:client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id):
    user = current_user()
    client = client_for_user(client_id, user["id"])
    if not client:
        flash("Cliente não encontrado.")
        return redirect(url_for("clientes"))
    with get_db() as db:
        db.execute("DELETE FROM clientes WHERE id = ? AND user_id = ?", (client_id, user["id"]))
    flash("Cliente removido.")
    return redirect(url_for("clientes"))


@app.route("/servicos", methods=["GET", "POST"])
@login_required
def servicos():
    user = current_user()
    if request.method == "POST":
        nome = sanitize_text(request.form.get("nome"))
        valor_padrao = parse_money(request.form.get("valor_padrao"))
        if not nome:
            flash("Informe o nome do serviço.")
            return redirect(url_for("servicos"))
        with get_db() as db:
            db.execute(
                "INSERT INTO servicos (user_id, nome, valor_padrao) VALUES (?, ?, ?)",
                (user["id"], nome, valor_padrao),
            )
        flash("Serviço salvo.")
        return redirect(url_for("servicos"))
    with get_db() as db:
        rows = db.execute("SELECT * FROM servicos WHERE user_id = ? ORDER BY datetime(created_at) DESC", (user["id"],)).fetchall()
    return render_template("servicos.html", user=user, services=rows)


@app.route("/servicos/<int:service_id>/delete", methods=["POST"])
@login_required
def delete_service(service_id):
    user = current_user()
    service = service_for_user(service_id, user["id"])
    if not service:
        flash("Serviço não encontrado.")
        return redirect(url_for("servicos"))
    with get_db() as db:
        db.execute("DELETE FROM servicos WHERE id = ? AND user_id = ?", (service_id, user["id"]))
    flash("Serviço removido.")
    return redirect(url_for("servicos"))


# ------------------------
# Notes
# ------------------------
@app.route("/notas", methods=["GET"])
@login_required
def listar_notas():
    user = current_user()
    q = sanitize_text(request.args.get("q"))
    categoria = sanitize_text(request.args.get("categoria"))
    tipo = sanitize_text(request.args.get("tipo"))
    status = sanitize_text(request.args.get("status"))
    data_inicio = request.args.get("data_inicio") or ""
    data_fim = request.args.get("data_fim") or ""

    query = "SELECT * FROM notas WHERE user_id = ?"
    params = [user["id"]]
    if q:
        query += " AND (descricao LIKE ? OR cliente LIKE ? OR cnpj_emitente LIKE ? OR categoria LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    if categoria:
        query += " AND categoria = ?"
        params.append(categoria)
    if tipo in {"entrada", "saida"}:
        query += " AND tipo = ?"
        params.append(tipo)
    if status and status != "Todas":
        query += " AND status = ?"
        params.append(status)
    if data_inicio:
        query += " AND date(COALESCE(data_emissao, data_upload)) >= date(?)"
        params.append(data_inicio)
    if data_fim:
        query += " AND date(COALESCE(data_emissao, data_upload)) <= date(?)"
        params.append(data_fim)
    query += " ORDER BY datetime(data_upload) DESC"
    with get_db() as db:
        notes = db.execute(query, params).fetchall()
    herc_tip = "primeira_nota" if notes and not tip_seen(user["id"], "primeira_nota") else None
    return render_template(
        "listar.html",
        user=user,
        notes=notes,
        herc_tip=herc_tip,
        herc_tip_text=HERC_TIPS.get(herc_tip),
        q=q,
        categoria=categoria,
        tipo=tipo,
        status=status,
        data_inicio=data_inicio,
        data_fim=data_fim,
        categories=NOTE_CATEGORIES,
        statuses=["Todas", "Autorizada", "Processando", "Rejeitada"],
    )


_AI_NOTE_PROMPT = """Você lê fotos de notas fiscais, cupons e recibos brasileiros.
Extraia os dados desta imagem e responda APENAS com um JSON válido, sem comentários, no formato:
{"descricao": "resumo curto do que é (ex.: 'Consulta médica - Dra. Ana')",
 "valor": 123.45,
 "data_emissao": "AAAA-MM-DD",
 "cliente": "nome do emitente/estabelecimento",
 "cnpj_emitente": "apenas os 14 dígitos, ou null",
 "numero_nota": "número da NF/cupom, ou null",
 "categoria": "uma destas: Saúde, Educação, Moradia, Transporte, Alimentação, Lazer, Serviços, Outros"}
Se algum campo não estiver visível, use null. Não invente valores."""


@app.route("/notas/analisar", methods=["POST"])
@login_required
def analisar_nota():
    if not ANTHROPIC_API_KEY or http_requests is None:
        return {"erro": "A leitura com IA ainda não está ativada neste servidor."}, 503
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return {"erro": "Tire ou escolha a foto da nota primeiro."}, 400
    ext = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    media_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    if ext not in media_types:
        return {"erro": "Envie uma foto (JPG, PNG ou WebP) — PDF ainda não."}, 400
    dados = arquivo.read()
    if len(dados) > 8 * 1024 * 1024:
        return {"erro": "Foto muito grande (máx. 8 MB)."}, 400

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_types[ext],
                    "data": base64.b64encode(dados).decode(),
                }},
                {"type": "text", "text": _AI_NOTE_PROMPT},
            ],
        }],
    }
    try:
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=60,
        )
    except Exception:
        return {"erro": "Não consegui falar com a IA agora. Tente de novo em instantes."}, 502
    if resp.status_code != 200:
        return {"erro": f"A IA recusou a leitura (código {resp.status_code}). Confira a chave e o saldo da API."}, 502

    try:
        texto = resp.json()["content"][0]["text"].strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
        extraido = json.loads(texto)
    except (KeyError, IndexError, ValueError):
        return {"erro": "A IA não conseguiu entender essa foto. Preencha manualmente."}, 422

    campos = {}
    for k in ("descricao", "valor", "data_emissao", "cliente", "cnpj_emitente", "numero_nota", "categoria"):
        v = extraido.get(k)
        if v is not None and v != "":
            campos[k] = v
    if campos.get("categoria") not in NOTE_CATEGORIES:
        campos.pop("categoria", None)
    return {"ok": True, "campos": campos}


@app.route("/notas/nova", methods=["GET", "POST"])
@login_required
def nova_nota():
    user = current_user()
    if request.method == "POST":
        note, error = create_note_and_link_transaction(user["id"], request.form, request.files)
        if error:
            flash(error)
            return redirect(url_for("nova_nota"))
        flash("Nota salva e vinculada à movimentação.")
        return redirect(url_for("listar_notas"))
    return render_template(
        "nova_nota.html",
        user=user,
        ai_enabled=bool(ANTHROPIC_API_KEY and http_requests),
        note=None,
        categories=NOTE_CATEGORIES,
        statuses=["Autorizada", "Processando", "Rejeitada"],
        types=TRANSACTION_TYPES,
        mode="create",
    )


@app.route("/notas/<int:note_id>/editar", methods=["GET", "POST"])
@login_required
def editar_nota(note_id):
    user = current_user()
    note = note_for_user(note_id, user["id"])
    if not note:
        flash("Nota não encontrada.")
        return redirect(url_for("listar_notas"))
    if request.method == "POST":
        updated, error = create_note_and_link_transaction(user["id"], request.form, request.files, existing_note=note)
        if error:
            flash(error)
            return redirect(url_for("editar_nota", note_id=note_id))
        flash("Nota atualizada.")
        return redirect(url_for("listar_notas"))
    return render_template(
        "nova_nota.html",
        user=user,
        ai_enabled=bool(ANTHROPIC_API_KEY and http_requests),
        note=note,
        categories=NOTE_CATEGORIES,
        statuses=["Autorizada", "Processando", "Rejeitada"],
        types=TRANSACTION_TYPES,
        mode="edit",
    )


@app.route("/notas/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_nota(note_id):
    user = current_user()
    note = note_for_user(note_id, user["id"])
    if not note:
        flash("Nota não encontrada.")
        return redirect(url_for("listar_notas"))
    with get_db() as db:
        db.execute("DELETE FROM transacoes WHERE nota_id = ? AND user_id = ?", (note_id, user["id"]))
        db.execute("DELETE FROM notas WHERE id = ? AND user_id = ?", (note_id, user["id"]))
    remove_uploaded_file(note["arquivo"])
    flash("Nota removida.")
    return redirect(url_for("listar_notas"))


# ------------------------
# Transactions
# ------------------------
@app.route("/transacoes", methods=["GET"])
@login_required
def listar_transacoes():
    user = current_user()
    q = sanitize_text(request.args.get("q"))
    tipo = sanitize_text(request.args.get("tipo"))
    categoria = sanitize_text(request.args.get("categoria"))
    data_inicio = request.args.get("data_inicio") or ""
    data_fim = request.args.get("data_fim") or ""

    query = "SELECT * FROM transacoes WHERE user_id = ?"
    params = [user["id"]]
    if q:
        query += " AND (descricao LIKE ? OR estabelecimento LIKE ? OR categoria LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if tipo in {"entrada", "saida"}:
        query += " AND tipo = ?"
        params.append(tipo)
    if categoria:
        query += " AND categoria = ?"
        params.append(categoria)
    if data_inicio:
        query += " AND date(COALESCE(data_transacao, created_at)) >= date(?)"
        params.append(data_inicio)
    if data_fim:
        query += " AND date(COALESCE(data_transacao, created_at)) <= date(?)"
        params.append(data_fim)

    query += " ORDER BY datetime(COALESCE(data_transacao, created_at)) DESC"
    with get_db() as db:
        txs = db.execute(query, params).fetchall()
    return render_template(
        "transacoes.html",
        user=user,
        txs=txs,
        q=q,
        tipo=tipo,
        categoria=categoria,
        data_inicio=data_inicio,
        data_fim=data_fim,
        categories=expense_category_names(user["id"]) + [c for c in INCOME_CATEGORIES if c not in TRANSACTION_CATEGORIES],
        types=TRANSACTION_TYPES,
        novo=request.args.get("novo", type=int),
    )


@app.route("/transacoes/nova", methods=["GET", "POST"])
@login_required
def nova_transacao():
    user = current_user()
    if request.method == "POST":
        tipo = sanitize_text(request.form.get("tipo"))
        valor = parse_money(request.form.get("valor"))
        descricao = sanitize_text(request.form.get("descricao"))
        estabelecimento = sanitize_text(request.form.get("estabelecimento")) or descricao
        categoria = sanitize_text(request.form.get("categoria")) or categorize(user["id"], estabelecimento, descricao)
        data_transacao = request.form.get("data_transacao") or date.today().isoformat()
        fonte = sanitize_text(request.form.get("fonte")) or "manual"
        confidence = int(parse_money(request.form.get("confidence")) or 100)
        needs_review = 1 if request.form.get("needs_review") else 0
        extra_json = request.form.get("extra_json") or ""
        captura_id = request.form.get("captura_id", type=int)
        no_credito = 1 if (tipo == "saida" and request.form.get("no_credito")) else 0
        if tipo not in {"entrada", "saida"}:
            flash("Escolha um tipo válido.")
            return redirect(url_for("nova_transacao"))
        if valor <= 0:
            flash("Informe um valor válido.")
            return redirect(url_for("nova_transacao"))
        try:
            date.fromisoformat(data_transacao)
        except ValueError:
            flash("Data inválida.")
            return redirect(url_for("nova_transacao"))
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO transacoes
                   (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte, confidence, needs_review, extra_json, no_credito)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user["id"],
                    tipo,
                    valor,
                    descricao,
                    estabelecimento,
                    categoria,
                    data_transacao,
                    fonte,
                    max(0, min(100, confidence)),
                    needs_review,
                    extra_json,
                    no_credito,
                ),
            )
            new_id = cursor.lastrowid
            if captura_id:
                db.execute(
                    "UPDATE capturas SET status = 'processada' WHERE id = ? AND user_id = ?",
                    (captura_id, user["id"]),
                )
        flash(("Entrada registrada." if tipo == "entrada" else "Saída registrada.") + " Está aqui na sua lista.")
        return redirect(url_for("listar_transacoes", novo=new_id))
    return render_template(
        "nova_transacao.html",
        user=user,
        categories=expense_category_names(user["id"]),
        income_categories=INCOME_CATEGORIES,
        types=TRANSACTION_TYPES,
    )


@app.route("/transacoes/<int:tx_id>/delete", methods=["POST"])
@login_required
def delete_transacao(tx_id):
    user = current_user()
    tx = transaction_for_user(tx_id, user["id"])
    if not tx:
        flash("Movimentação não encontrada.")
        return redirect(url_for("listar_transacoes"))
    with get_db() as db:
        db.execute("DELETE FROM transacoes WHERE id = ? AND user_id = ?", (tx_id, user["id"]))
    flash("Movimentação removida.")
    return redirect(url_for("listar_transacoes"))


# ------------------------
# OFX import
# ------------------------
@app.route("/importar", methods=["GET", "POST"])
@login_required
def importar_ofx():
    user = current_user()
    if request.method == "POST":
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Escolha o arquivo OFX exportado do seu banco.")
            return redirect(url_for("importar_ofx"))
        if not arquivo.filename.lower().endswith((".ofx", ".qfx")):
            flash("O arquivo precisa ser .ofx (exportado do app do banco).")
            return redirect(url_for("importar_ofx"))
        raw = arquivo.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1", errors="replace")
        items = parse_ofx(content)
        if not items:
            flash("Não encontrei movimentações nesse arquivo. Confirme que é um extrato OFX.")
            return redirect(url_for("importar_ofx"))
        forcar_credito = bool(request.form.get("fatura_cartao"))
        detectou_credito = any(item.get("no_credito") for item in items)
        stats = import_ofx_transactions(user["id"], items, forcar_credito=forcar_credito)
        with get_db() as db:
            db.execute("UPDATE usuarios SET last_ofx_import = ? WHERE id = ?", (date.today().isoformat(), user["id"]))
        partes = [f"{stats['importadas']} novas importadas"]
        if stats["reconciliadas"]:
            partes.append(f"{stats['reconciliadas']} já estavam anotadas (conferidas ✓)")
        if stats["ja_importadas"]:
            partes.append(f"{stats['ja_importadas']} repetidas puladas")
        if stats["antigas"]:
            partes.append(f"{stats['antigas']} anteriores ao seu saldo inicial, puladas")
        if detectou_credito or forcar_credito:
            partes.append("marcadas como fatura de cartão (não descontam do saldo agora)")
        flash("Extrato processado: " + " · ".join(partes) + ".")
        return redirect(url_for("listar_transacoes"))
    return render_template("importar.html", user=user)


# ------------------------
# Export
# ------------------------
@app.route("/exportar-ir")
@login_required
def exportar_ir():
    user = current_user()
    year = request.args.get("year", str(date.today().year))
    categoria = sanitize_text(request.args.get("categoria"))
    tipo = sanitize_text(request.args.get("tipo"))
    with get_db() as db:
        query = """SELECT * FROM notas
                   WHERE user_id = ? AND strftime('%Y', COALESCE(data_emissao, data_upload)) = ?"""
        params = [user["id"], year]
        if categoria:
            query += " AND categoria = ?"
            params.append(categoria)
        if tipo in {"entrada", "saida"}:
            query += " AND tipo = ?"
            params.append(tipo)
        query += " ORDER BY COALESCE(data_emissao, data_upload) ASC"
        notes = db.execute(query, params).fetchall()

    def stream():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["descricao", "valor", "tipo", "categoria", "cliente", "cnpj_emitente", "numero_nota", "status", "data_emissao", "data_upload", "arquivo"])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        for note in notes:
            writer.writerow([
                note["descricao"],
                f"{float(note['valor']):.2f}",
                note["tipo"],
                note["categoria"],
                note["cliente"] or "",
                note["cnpj_emitente"] or "",
                note["numero_nota"] or "",
                note["status"] or "",
                note["data_emissao"] or "",
                note["data_upload"] or "",
                note["arquivo"] or "",
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    filename = f"exportacao_ir_{year}.csv"
    return Response(stream(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


# ------------------------
# Files
# ------------------------
@app.route("/download/<filename>")
@login_required
def download(filename):
    user = current_user()
    with get_db() as db:
        note = db.execute("SELECT id FROM notas WHERE arquivo = ? AND user_id = ?", (filename, user["id"])).fetchone()
    if not note:
        flash("Arquivo não encontrado ou sem permissão.")
        return redirect(url_for("listar_notas"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)


# ------------------------
# PWA
# ------------------------
@app.route("/sw.js")
def service_worker():
    # Servido da raiz para o escopo do service worker cobrir o app inteiro
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


# Impressão digital (SHA-256) da assinatura de debug do app Android, extraída
# no workflow .github/workflows/android-companion.yml (passo "Print debug
# keystore SHA-256 fingerprint"). Prova ao Android que só o app real do
# Hércules pode receber o retorno do login do Google.
ANDROID_APP_FINGERPRINT = "A6:95:39:B5:38:7D:DD:78:70:7F:3D:DE:39:36:9E:1D:38:81:1F:40:54:8F:05:29:CF:7E:A5:1D:B5:9A:A3:B9"


@app.route("/.well-known/assetlinks.json")
def android_asset_links():
    payload = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.hercules.companion",
            "sha256_cert_fingerprints": [ANDROID_APP_FINGERPRINT],
        },
    }]
    return Response(json.dumps(payload), mimetype="application/json")


# ------------------------
# Alias helpers
# ------------------------
@app.route("/home")
@login_required
def home_alias():
    return redirect(url_for("home"))


# ------------------------
# Errors
# ------------------------
@app.errorhandler(413)
def too_large(_):
    flash("Arquivo muito grande. Envie até 16 MB.")
    return redirect(request.referrer or url_for("home"))


if __name__ == "__main__":
    # host 0.0.0.0 permite testar pelo celular na mesma rede (http://IP-do-PC:5000)
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
