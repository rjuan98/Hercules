from __future__ import annotations

import base64
import calendar
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import traceback
import unicodedata
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
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
from markupsafe import Markup
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from urllib.parse import urlparse
from werkzeug.routing import IntegerConverter
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from backup import (cifragem_ligada, esta_cifrado, fazer_backup, garantir_backup_do_dia,
                    listar_backups, ultimo_backup)
from database import get_db, init_db

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:  # optional
    OAuth = None

try:
    import requests as http_requests
except ImportError:  # optional (necessário para a leitura de notas com IA)
    http_requests = None

try:
    from pypdf import PdfReader
except ImportError:  # optional (necessário para importar extrato em PDF)
    PdfReader = None

try:  # optional (necessário para o bloqueio por digital/rosto)
    import webauthn as _webauthn
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
except ImportError:
    _webauthn = None

# Leitura de notas com IA: liga sozinha quando a chave existir no ambiente
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Open Finance via Pluggy: liga sozinho quando as chaves existirem no ambiente.
# PLUGGY_ITEM_IDS = ids dos bancos conectados (no Meu Pluggy), separados por vírgula.
PLUGGY_API = "https://api.pluggy.ai"
PLUGGY_CLIENT_ID = os.environ.get("PLUGGY_CLIENT_ID")
PLUGGY_CLIENT_SECRET = os.environ.get("PLUGGY_CLIENT_SECRET")
PLUGGY_ITEM_IDS = [s.strip() for s in (os.environ.get("PLUGGY_ITEM_IDS") or "").split(",") if s.strip()]

BASE_DIR = Path(__file__).resolve().parent

# ------------------------
# Fuso: o app é brasileiro, o servidor não
# ------------------------
# PythonAnywhere (e quase toda hospedagem) roda em UTC. Usar a hora do servidor
# joga toda compra feita depois das 21h para o dia seguinte — e no dia 31, para o
# MÊS seguinte, bagunçando fatura, "gastei hoje" e o recado da semana.
try:
    from zoneinfo import ZoneInfo
    FUSO_BR = ZoneInfo("America/Sao_Paulo")
except Exception:            # sem tzdata no sistema: o Brasil não tem horário de verão
    FUSO_BR = timezone(timedelta(hours=-3))


def agora_br() -> datetime:
    """Agora em horário de Brasília, sem tzinfo (o banco guarda ingênuo)."""
    return datetime.now(FUSO_BR).replace(tzinfo=None)


def hoje_br() -> date:
    return agora_br().date()


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


class _IdConverter(IntegerConverter):
    """Id de URL cabe em 64 bits, que é o teto do SQLite.

    O conversor `<int:>` do Flask aceita número de qualquer tamanho. Um id de 40
    dígitos passava pela rota, chegava no banco e estourava com OverflowError —
    erro 500 em 16 rotas, e uma linha de traceback no log pra cada varredura
    automática que passasse por aqui. Um id impossível não é erro do servidor: é
    página que não existe, e agora dá 404.
    """
    regex = r"\d{1,18}"


app.url_map.converters["int"] = _IdConverter
app.secret_key = _load_secret_key()
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Fica logado por 90 dias — logar toda vez é exaustivo
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)

# Atrás de um proxy HTTPS (Render/Railway/PythonAnywhere), respeita os headers X-Forwarded-*
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Cookie só por HTTPS. Sem isso, basta uma requisição em HTTP puro pra o cookie de
# sessão vazar. O PythonAnywhere serve HTTPS e anuncia o domínio no ambiente —
# detectar evita depender de alguém lembrar de ligar a variável.
_hospedagem_https = bool(os.environ.get("RENDER") or os.environ.get("PYTHONANYWHERE_DOMAIN")
                         or os.environ.get("PYTHONANYWHERE_SITE"))
if os.environ.get("SECURE_COOKIES") == "1" or (_hospedagem_https
                                               and os.environ.get("SECURE_COOKIES") != "0"):
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



POR_PAGINA = 60      # cabe numa rolagem sem virar página gigante
TRANSACTION_TYPES = [
    ("saida", "Saída"),
    ("entrada", "Entrada"),
]

# Paleta do gráfico — definida aqui para a legenda (HTML) e a rosca (JS) baterem
CHART_COLORS = ["#C96F3B", "#2E6D8C", "#3D5A2E", "#9C6A10", "#7E4E7A", "#5C8A73", "#B23A2A"]

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


def money_html(value: Any) -> Markup:
    """Mesmo valor, embrulhado — é isso que o olhinho de ocultar apaga na tela."""
    return Markup('<span class="money">{}</span>').format(money(value))


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




def allowed_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in {"pdf", "png", "jpg", "jpeg", "webp"}


# Nenhum campo de texto do app tem uso legítimo acima disso. Sem teto, um
# testador colou uma string gigante e ela foi pro banco inteira — quebrando o
# layout de todas as telas que mostram a lista.
TEXTO_MAX = 200


def sanitize_text(value: str | None, limite: int = TEXTO_MAX) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())[:limite]


def normalize_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


# Sentinela para "não perguntar mais sobre esse padrão"
IGNORE_RULE = "__manter__"

# ------------------------
# OFX: importação de extrato com reconciliação
# ------------------------
_OFX_TRN_RE = re.compile(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", re.DOTALL | re.IGNORECASE)


def _ofx_field(block: str, tag: str) -> str:
    """Campo OFX em SGML (sem fechamento) ou XML (com fechamento)."""
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return sanitize_text(m.group(1)) if m else ""


# Como os bancos brasileiros escrevem "paguei a fatura" no extrato do cartão (vale pro OFX e pra Pluggy)
_PAGAMENTO_FATURA = ("pagamento", "pagto", "pgto", "pag fatura", "pag. fatura")


# Dinheiro que só troca de bolso. A caixinha do Nubank é um RDB: guardar aparece
# como "Aplicação RDB" e tirar como "Resgate RDB". Sem reconhecer isso, quem usa
# caixinha vê o próprio dinheiro entrando como se fosse salário e saindo como se
# fosse gasto — e ninguém guarda dinheiro sem mexer nele.
#
# A lista é curta de propósito. Marcar receita de verdade como "interno" some com
# a renda da pessoa, que é o erro pior; na dúvida, deixa passar como movimento
# normal e ela corrige na tela.
_MOVIMENTO_INTERNO = (
    "aplicacao rdb", "resgate rdb",
    "aplicacao automatica", "resgate automatico",
    "aplicacao cdb", "resgate cdb",
    "aplicacao poupanca", "resgate poupanca", "deposito poupanca",
    "aplicacao investimento", "resgate investimento",
    "transferencia entre contas", "transf entre contas",
    "caixinha", "reserva de emergencia",
)


def e_movimento_interno(descricao: str) -> bool:
    """É dinheiro seu mudando de lugar, não entrando nem saindo da sua vida."""
    return any(p in _strip_accents(descricao or "").lower() for p in _MOVIMENTO_INTERNO)


def e_pagamento_de_fatura(descricao: str) -> bool:
    """Distingue PAGAR A FATURA de ESTORNAR UMA COMPRA — no cartão, os dois vêm
    com valor negativo, mas significam coisas opostas.

    Pagar a fatura não é movimento do cartão (o dinheiro sai da conta, e isso já
    está registrado lá). Estorno é uma compra que voltou: tem que ABATER a fatura,
    senão o app cobra da pessoa uma compra que ela devolveu."""
    return any(p in _strip_accents(descricao or "").lower() for p in _PAGAMENTO_FATURA)


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
        except (ValueError, OverflowError):
            continue
        # "1e400" vira infinito e "nan" vira NaN — os dois saem de um arquivo
        # malformado sem esforço nenhum. Um infinito guardado faz o saldo virar
        # "R$ -inf" na tela e não tem como desfazer sem mexer no banco.
        if amount == 0 or valor_absurdo(amount):
            continue
        dt_raw = _ofx_field(block, "DTPOSTED")[:8]
        try:
            dt = datetime.strptime(dt_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        memo = _ofx_field(block, "MEMO") or _ofx_field(block, "NAME") or "Movimentação importada"
        # Na fatura, positivo é estorno OU pagamento — e só o estorno abate.
        # Pagar a fatura já sai da conta corrente; contar aqui abateria duas vezes.
        # (Numa conta comum, "pagamento da fatura" é gasto de verdade: não pula.)
        if is_credit_card_file and amount > 0 and e_pagamento_de_fatura(memo):
            continue
        transactions.append({
            "valor": abs(amount),
            "tipo": "entrada" if amount > 0 else "saida",
            "data": dt,
            "descricao": memo[:120],
            "fitid": _ofx_field(block, "FITID")[:80] or None,
            "no_credito": is_credit_card_file,
        })
    return transactions


# ------------------------
# PDF / texto colado: quando o banco não dá OFX (ex.: Nubank só deixa achar o PDF)
# ------------------------
_PT_MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
              "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
# Valor em reais: "1.234,56" (com milhar) ou "1234,56"/"12,50" (sem). Aceita "-" e "R$" antes.
_BRL_VALUE_RE = re.compile(r"(-\s*)?R?\$?\s*((?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})")
_LEADING_DATE_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")
_LEADING_DATE_DMON = re.compile(
    r"^(\d{1,2})\s+(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\.?(?:\s+de)?\s*(\d{4})?",
    re.IGNORECASE,
)
# Uma entrada de dinheiro (o resto é gasto): o extrato do banco usa estas palavras.
_STATEMENT_ENTRADA_HINTS = ("recebid", "estorno", "devoluç", "devoluc", "reembolso",
                            "cashback", "rendimento", "salário", "salario", "crédito em conta",
                            "credito em conta", "deposito", "depósito")
# Linhas de resumo/saldo que NÃO são transações — pular pra não somar saldo como gasto.
_STATEMENT_SKIP_PREFIX = ("saldo", "total", "subtotal", "valor a pagar", "valor total",
                          "limite", "fatura anterior", "pagamento recebido", "pagamento da fatura",
                          "encargos e", "movimentações", "movimentacoes", "resumo")


def extract_pdf_text(raw: bytes) -> str:
    """Texto de um PDF de extrato. Só funciona em PDF de texto (não em PDF escaneado/imagem)."""
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _resolve_year(day: int, month: int, year: int | None, hoje: date) -> date | None:
    if year is not None:
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    # Sem ano no extrato: usa o ano que deixa a data no passado recente (não no futuro).
    for candidate in (hoje.year, hoje.year - 1):
        try:
            d = date(candidate, month, day)
        except ValueError:
            continue
        if d <= hoje + timedelta(days=1):
            return d
    return None


def _leading_date(line: str, hoje: date) -> date | None:
    """Data só quando lidera a linha (convenção de extrato) — evita pegar '2/6' de 'Parcela 2/6'."""
    m = _LEADING_DATE_DMON.match(line)
    if m:
        month = _PT_MONTHS.get(m.group(2).lower()[:3])
        if month:
            yr = int(m.group(3)) if m.group(3) else None
            return _resolve_year(int(m.group(1)), month, yr, hoje)
    m = _LEADING_DATE_DMY.match(line)
    if m:
        yr = int(m.group(3)) if m.group(3) else None
        return _resolve_year(int(m.group(1)), int(m.group(2)), yr, hoje)
    return None


def _strip_leading_date(text: str) -> str:
    text = _LEADING_DATE_DMON.sub("", text.strip(), count=1)
    text = _LEADING_DATE_DMY.sub("", text.strip(), count=1)
    return text.strip()


def parse_bank_statement_text(text: str, forcar_credito: bool = False) -> list[dict[str, Any]]:
    """Lê o texto de um extrato (de PDF ou colado) e devolve transações no mesmo formato do OFX.
    Estratégia tolerante: a data lidera a linha (ou é um cabeçalho de dia); o valor é o número
    em reais na linha; o tipo vem do sinal '-' ou de palavras como 'recebido'. Sem certeza,
    assume GASTO (mais seguro num app que controla gastos). Cada linha ganha um id sintético
    pra reimportar o mesmo extrato não duplicar. O que não der pra situar no tempo é descartado."""
    low_all = text.lower()
    is_credit = forcar_credito or bool(
        re.search(r"cart[aã]o de cr[eé]dito", low_all)
        or ("fatura" in low_all and ("vencimento" in low_all or "transaç" in low_all or "transac" in low_all))
    )
    hoje = hoje_br()
    current_date: date | None = None
    seq_counter: dict[tuple, int] = {}
    items: list[dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        d = _leading_date(line, hoje)
        if d:
            current_date = d
        low = line.lower()
        if low.startswith(_STATEMENT_SKIP_PREFIX):
            continue
        val_matches = list(_BRL_VALUE_RE.finditer(line))
        if not val_matches:
            continue
        vm = val_matches[-1]  # o valor da transação costuma ser o último número da linha
        valor = float(vm.group(2).replace(".", "").replace(",", "."))
        if valor <= 0 or valor_absurdo(valor):
            continue
        the_date = current_date or d
        if the_date is None:
            continue  # sem data não dá pra situar no tempo com segurança
        neg = bool(vm.group(1))

        if is_credit:
            # Numa fatura, pagamento/estorno abatem a conta — não são compras, então pulamos.
            if any(k in low for k in ("pagamento", "estorno", "crédito", "credito")):
                continue
            tipo, no_cred = "saida", True
        elif neg:
            tipo, no_cred = "saida", False
        elif any(h in low for h in _STATEMENT_ENTRADA_HINTS):
            tipo, no_cred = "entrada", False
        else:
            tipo, no_cred = "saida", False

        desc = (line[:vm.start()] + " " + line[vm.end():]).strip()
        desc = _strip_leading_date(desc)
        desc = re.sub(r"\s{2,}", " ", desc).strip(" -–—\t")
        desc = sanitize_text(desc)[:120] or "Movimentação importada"

        key = (the_date.isoformat(), tipo, round(valor, 2), desc)
        seq = seq_counter.get(key, 0)
        seq_counter[key] = seq + 1
        fitid = "PDF-" + hashlib.sha1(f"{key}|{seq}".encode("utf-8")).hexdigest()[:20]
        items.append({
            "valor": valor,
            "tipo": tipo,
            "data": the_date.isoformat(),
            "descricao": desc,
            "fitid": fitid,
            "no_credito": no_cred,
        })
    return items


# "PARC 3/12", "PARCELA 3 DE 12", "3/12" — só com a palavra parcela por perto, senão
# uma data como "3/12" viraria parcelamento.
_PARCELA_RE = re.compile(
    r"(?:parc(?:ela)?\.?\s*|\b)(\d{1,2})\s*(?:/|\s+de\s+)\s*(\d{1,2})\b", re.IGNORECASE)


def detectar_parcela(texto: str) -> tuple[int | None, int | None]:
    """Lê '3/12' de uma descrição de compra parcelada. Devolve (parcela, total)."""
    t = (texto or "")
    if not re.search(r"parc", t, re.IGNORECASE):
        return None, None
    m = _PARCELA_RE.search(t)
    if not m:
        return None, None
    num, total = int(m.group(1)), int(m.group(2))
    if 1 <= num <= total <= 99 and total > 1:
        return num, total
    return None, None


def _dia_do_mes(ano: int, mes: int, dia: int) -> date:
    """Dia 31 em fevereiro vira o último dia do mês (o banco fecha no último dia)."""
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def dia_de_data_api(valor) -> int | None:
    """Extrai o dia do mês de uma data vinda da API do banco.

    Fatiar a string na mão (`str(v)[8:10]`) quebra com formato inesperado e, pior,
    às vezes ACERTA um número errado — "15/08/2026" viraria dia 26 e desalinharia
    a fatura inteira sem erro nenhum. Na dúvida, devolve None."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(valor or "").strip())
    if not m:
        return None
    dia = int(m.group(3))
    return dia if 1 <= dia <= 31 else None


def ciclo_fatura(hoje: date, dia_fechamento: int) -> tuple[date, date]:
    """Início e fim da fatura ABERTA. Fatura não segue o calendário: fecha no dia X,
    então uma compra do dia 28 pode cair na fatura do mês seguinte."""
    fecha_este_mes = _dia_do_mes(hoje.year, hoje.month, dia_fechamento)
    if hoje <= fecha_este_mes:
        mes_ant = hoje.replace(day=1) - timedelta(days=1)
        inicio = _dia_do_mes(mes_ant.year, mes_ant.month, dia_fechamento) + timedelta(days=1)
        return inicio, fecha_este_mes
    prox = (hoje.replace(day=1) + timedelta(days=32)).replace(day=1)
    return fecha_este_mes + timedelta(days=1), _dia_do_mes(prox.year, prox.month, dia_fechamento)


def _chave_compra(descricao: str) -> str:
    """Nome da compra sem o número da parcela — 'NOTEBOOK PARC 1/12' e 'PARC 2/12'
    são a MESMA compra. Sem isso, importar 3 meses de fatura contaria 3 vezes."""
    base = re.sub(r"parc(?:ela)?\.?\s*\d{1,2}\s*(?:/|\s+de\s+)\s*\d{1,2}", " ", descricao or "", flags=re.IGNORECASE)
    base = re.sub(r"\b\d{1,2}\s*/\s*\d{1,2}\b", " ", base)
    return re.sub(r"\s+", " ", base).strip().lower()


def possiveis_duplicatas(user_id: int) -> list[dict[str, Any]]:
    """Lançamento anotado na mão que parece ser a MESMA coisa que o banco trouxe.

    O caso real: o Matheus digitou "salário R$ 2.600" no dia 30, porque o dinheiro
    ainda não tinha caído. No dia 31 o banco trouxe "Transferência Recebida —
    R$ 2.660,41". Para a reconciliação do import, que exige mesmo dia e mesmo
    valor ao centavo, são duas coisas. Para a vida dele, é o salário — e o mês
    fechou com R$ 2.600 de renda que não existiu.

    Casar por aproximação SOZINHO seria pior: um dia isso apagaria um gasto de
    verdade. Então o app não decide, ele pergunta. O que aparece aqui é candidato,
    não veredito.

    O sinal é forte e específico: um lado foi anotado na mão (é sempre uma
    aproximação de algo que o banco ainda vai confirmar) e o outro veio do banco,
    perto na data e no valor.
    """
    # Uma consulta so. Antes era uma por lancamento anotado na mao (ate 40), e
    # isso roda em TODA carga da tela inicial — no PythonAnywhere, onde o
    # orcamento e de 100 segundos de CPU por dia, 40 idas ao banco por visita
    # custam de verdade.
    col_m = "date(COALESCE(NULLIF(m.data_transacao, ''), m.created_at))"
    col_b = "date(COALESCE(NULLIF(b.data_transacao, ''), b.created_at))"
    with get_db() as db:
        linhas = db.execute(
            f"""SELECT m.id AS m_id, m.valor AS m_valor, m.descricao AS m_desc,
                       {col_m} AS m_dia, m.tipo AS tipo,
                       b.id AS b_id, b.valor AS b_valor, b.descricao AS b_desc,
                       {col_b} AS b_dia,
                       MIN(ABS(b.valor - m.valor)) AS _perto
                  FROM transacoes m
                  JOIN transacoes b
                    ON b.user_id = m.user_id AND b.id != m.id AND b.tipo = m.tipo
                   AND b.fitid IS NOT NULL
                   -- Tolerancia proporcional: quem digita "2.600" pra um salario
                   -- de R$ 2.660,41 erra 2,3%, nao centavos. O piso e baixo de
                   -- proposito: com R$ 20 de folga, R$ 30 casaria com R$ 45.
                   AND ABS(b.valor - m.valor) <= MAX(5.0, m.valor * 0.05)
                   AND ABS(julianday({col_b}) - julianday({col_m})) <= 4
                 WHERE m.user_id = ? AND m.fonte = 'manual' AND m.fitid IS NULL
                   AND m.dup_ok = 0 AND m.no_credito = 0
                   AND COALESCE(NULLIF(m.categoria, ''), '') != 'Reserva'
                   AND m.valor > 0
                 GROUP BY m.id
                 ORDER BY {col_m} DESC
                 LIMIT 20""",
            (user_id,),
        ).fetchall()
    return [{
        "manual": {"id": r["m_id"], "valor": float(r["m_valor"]),
                   "descricao": r["m_desc"], "dia": r["m_dia"]},
        "banco": {"id": r["b_id"], "valor": float(r["b_valor"]),
                  "descricao": r["b_desc"], "dia": r["b_dia"]},
        "tipo": r["tipo"],
    } for r in linhas]


def calc_parcelas_futuras(user_id: int) -> dict[str, Any]:
    """Quanto das PRÓXIMAS faturas já está comprometido por parcelamento.
    Agrupa por compra e conta só a parcela mais recente de cada uma."""
    with get_db() as db:
        rows = db.execute(
            """SELECT descricao, valor, parcela_num, parcela_total FROM transacoes
               WHERE user_id = ? AND no_credito = 1
                 AND parcela_total > 1 AND parcela_num IS NOT NULL""",
            (user_id,),
        ).fetchall()
    compras: dict[tuple, dict] = {}
    for r in rows:
        chave = (_chave_compra(r["descricao"]), int(r["parcela_total"]), round(float(r["valor"]), 2))
        atual = compras.get(chave)
        if not atual or int(r["parcela_num"]) > atual["ultima"]:
            compras[chave] = {"descricao": r["descricao"], "valor": float(r["valor"]),
                              "ultima": int(r["parcela_num"]), "total": int(r["parcela_total"])}
    total, meses, itens = 0.0, 0, []
    for c in compras.values():
        restantes = c["total"] - c["ultima"]
        if restantes <= 0:
            continue
        c["restantes"] = restantes
        c["falta"] = restantes * c["valor"]
        total += c["falta"]
        meses = max(meses, restantes)
        itens.append(c)
    itens.sort(key=lambda i: i["falta"], reverse=True)
    return {"total": total, "meses": meses, "itens": itens, "tem": bool(itens)}


def _provedor_do_fitid(fitid: str | None) -> str | None:
    """De onde veio esse identificador.

    Cada fonte numera do seu jeito: a Pluggy manda PLG-<id>, o PDF vira PDF-<hash>
    e o OFX traz o FITID do banco. Saber a origem e o que permite reconhecer que
    dois ids diferentes sao a MESMA compra vista por caminhos diferentes.
    """
    if not fitid:
        return None
    if fitid.startswith("PLG-"):
        return "pluggy"
    if fitid.startswith("PDF-"):
        return "pdf"
    return "ofx"


def import_ofx_transactions(user_id: int, items: list[dict[str, Any]], forcar_credito: bool = False) -> dict[str, int]:
    """Importa com reconciliação: FITID já visto = pula; valor+data já registrado
    (captura/manual) = casa e marca; anterior ao saldo inicial = pula (protege o saldo)."""
    stats = {"importadas": 0, "ja_importadas": 0, "reconciliadas": 0, "antigas": 0}
    # Linhas ja casadas NESTE import. Sem isso, dois cafes de R$ 5 no mesmo dia
    # colapsariam num so: o segundo acharia a mesma linha que o primeiro.
    ja_casadas: set[int] = set()
    # As regras aprendidas não mudam durante o import: lê uma vez, não uma por linha.
    # Um extrato de 3 meses são centenas de lançamentos — eram centenas de conexões.
    regras = user_rules(user_id)
    with get_db() as db:
        saldo_row = db.execute(
            """SELECT date(COALESCE(NULLIF(data_transacao, ''), created_at)) AS dia FROM transacoes
               WHERE user_id = ? AND fonte = 'ajuste' AND descricao = 'Saldo inicial'
               ORDER BY dia ASC LIMIT 1""",
            (user_id,),
        ).fetchone()
        saldo_date = saldo_row["dia"] if saldo_row else None

        for item in items:
            # O saldo inicial já resume o passado: importar dias anteriores duplicaria dinheiro
            if saldo_date and item["data"] and item["data"] < saldo_date:
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
            # Última barreira, valendo pra qualquer origem — inclusive uma que
            # ainda não existe. Valor ruim aqui não vira erro: vira linha pulada,
            # porque derrubar o import inteiro por causa de uma linha perderia
            # todas as outras.
            try:
                if valor_absurdo(float(item.get("valor"))):
                    stats["antigas"] += 0
                    continue
            except (TypeError, ValueError):
                continue
            no_credito = 1 if (item.get("no_credito") or forcar_credito) else 0

            # Reconciliação: esse valor, nesse dia, já está registrado?
            #
            # Candidata é toda linha do mesmo tipo/valor/dia que veio de OUTRO
            # caminho — anotada na mão, capturada pelo Herc, ou trazida por outra
            # fonte. Era aqui que a conta dobrava: exigia `fitid IS NULL AND fonte
            # != 'ofx'`, e a linha da Pluggy tem as duas coisas (fitid PLG-… e
            # fonte 'ofx', porque entra por esta mesma função). Resultado: quem
            # conectava o banco E importava o extrato via tudo em dobro — saldo,
            # gasto do mês, ritmo diário.
            provedor_novo = _provedor_do_fitid(item["fitid"])
            candidatas = db.execute(
                """SELECT id, fitid FROM transacoes
                   WHERE user_id = ? AND tipo = ? AND ABS(valor - ?) < 0.005
                     AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) = date(?)
                   ORDER BY id""",
                (user_id, item["tipo"], item["valor"], item["data"]),
            ).fetchall()
            match = next(
                (c for c in candidatas
                 if c["id"] not in ja_casadas
                 and _provedor_do_fitid(c["fitid"]) != provedor_novo),
                None,
            )
            if match:
                ja_casadas.add(match["id"])
                # NÃO troca o fitid: o id antigo é o que a fonte antiga vai
                # procurar no próximo sync. Sobrescrever faria ela reimportar.
                db.execute(
                    "UPDATE transacoes SET fitid = COALESCE(fitid, ?), no_credito = ? WHERE id = ?",
                    (item["fitid"], no_credito, match["id"]),
                )
                stats["reconciliadas"] += 1
                continue
            # A regra ensinada vence sempre; sem regra, só saída ganha categoria automática.
            regra = apply_rules(user_id, item["descricao"], regras=regras)
            if regra:
                categoria = regra
            elif item["tipo"] == "saida":
                categoria = auto_category(item["descricao"])
            else:
                categoria = "Outros"
            p_num = item.get("parcela_num")
            p_total = item.get("parcela_total")
            if not p_total:  # extrato que só escreve "PARC 3/12" na descrição
                p_num, p_total = detectar_parcela(item["descricao"])
            db.execute(
                """INSERT INTO transacoes
                   (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao,
                    fonte, confidence, fitid, no_credito, parcela_num, parcela_total, interno)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ofx', 95, ?, ?, ?, ?, ?)""",
                (user_id, item["tipo"], item["valor"], item["descricao"], item["descricao"],
                 categoria, item["data"], item["fitid"], no_credito, p_num, p_total,
                 1 if e_movimento_interno(item["descricao"]) else 0),
            )
            ja_casadas.add(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            stats["importadas"] += 1
    return stats


# ------------------------
# Open Finance via Pluggy: o banco entrega os gastos direto, sem subir arquivo
# ------------------------
def pluggy_configured() -> bool:
    """App nível: consegue falar com a Pluggy. O item do banco é por usuário."""
    return bool(PLUGGY_CLIENT_ID and PLUGGY_CLIENT_SECRET and http_requests)


def pluggy_user_item_ids(user) -> list[str]:
    """Item(ns) do banco conectados por ESTE usuário (via widget). Cai no env var antigo se vazio."""
    item = user["pluggy_item_id"] if ("pluggy_item_id" in user.keys() and user["pluggy_item_id"]) else None
    return [item] if item else PLUGGY_ITEM_IDS


def pluggy_connect_token(api_key: str) -> str:
    """Token pra inicializar o widget Pluggy Connect (amarrado à NOSSA aplicação)."""
    resp = http_requests.post(
        f"{PLUGGY_API}/connect_token",
        json={},
        headers={"X-API-KEY": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def pluggy_auth() -> str:
    """Troca clientId/secret por um apiKey temporário (vale ~2h). Levanta exceção em erro."""
    resp = http_requests.post(
        f"{PLUGGY_API}/auth",
        json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["apiKey"]


def _pluggy_get(api_key: str, path: str, params: dict | None = None):
    resp = http_requests.get(
        f"{PLUGGY_API}{path}",
        params=params or {},
        headers={"X-API-KEY": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def pluggy_status_itens(api_key: str, item_ids: list[str]) -> dict[str, Any]:
    """Como está a coleta agora: rodando? deu erro de acesso? de quando é o dado?"""
    info = {"atualizando": False, "erro_login": False, "ultima_coleta": None}
    for item_id in item_ids:
        try:
            it = _pluggy_get(api_key, f"/items/{item_id}")
        except Exception:
            continue
        status = (it.get("status") or "").upper()
        exec_status = (it.get("executionStatus") or "").upper()
        if status == "UPDATING":
            info["atualizando"] = True
        if "LOGIN_ERROR" in exec_status or "INVALID_CREDENTIALS" in exec_status or status == "LOGIN_ERROR":
            info["erro_login"] = True
        u = it.get("lastUpdatedAt")
        if u and (not info["ultima_coleta"] or str(u) > str(info["ultima_coleta"])):
            info["ultima_coleta"] = u
    return info


def _coleta_recente(iso: str | None, horas: float) -> bool:
    if not iso:
        return False
    try:
        quando = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    return (datetime.utcnow() - quando) < timedelta(hours=horas)


def pluggy_refresh_items(api_key: str, item_ids: list[str], espera_max: int = 24,
                         pedir_coleta: bool = True, min_horas: float = 0) -> dict[str, Any]:
    """Pede ao banco uma coleta NOVA (PATCH /items/{id}) e espera terminar.

    REGRA DE OURO: nunca pedir coleta com uma já rodando. Cada PATCH REINICIA a
    coleta — pedindo de 30 em 30 minutos ela nunca chegava ao fim, e o dado só
    aparecia de madrugada, quando ninguém abria o app.
    """
    info = pluggy_status_itens(api_key, item_ids)
    if info["erro_login"]:
        return {"pronto": True, "erro_login": True,
                "ultima_coleta": info["ultima_coleta"], "pediu": False}

    pediu = False
    ja_fresco = _coleta_recente(info["ultima_coleta"], min_horas) if min_horas else False
    if pedir_coleta and not info["atualizando"] and not ja_fresco:
        for item_id in item_ids:
            try:
                http_requests.patch(
                    f"{PLUGGY_API}/items/{item_id}", json={},
                    headers={"X-API-KEY": api_key}, timeout=30,
                ).raise_for_status()
                pediu = True
            except Exception:
                pass  # segue com o que já existe: melhor sincronizar velho do que falhar

    if not pediu and not info["atualizando"]:
        # nada rodando e nada pedido: o que está lá já é o mais novo que dá
        return {"pronto": True, "erro_login": False,
                "ultima_coleta": info["ultima_coleta"], "pediu": False}

    limite = time.time() + espera_max
    pronto, erro_login, ultima = False, False, info["ultima_coleta"]
    while time.time() < limite:
        time.sleep(3)
        agora = pluggy_status_itens(api_key, item_ids)
        ultima = agora["ultima_coleta"] or ultima
        erro_login = erro_login or agora["erro_login"]
        if not agora["atualizando"]:
            pronto = True
            break
    return {"pronto": pronto, "erro_login": erro_login, "ultima_coleta": ultima, "pediu": pediu}


def pluggy_accounts(api_key: str, item_ids: list[str]) -> list[dict]:
    """Todas as contas dos itens informados."""
    contas = []
    for item_id in item_ids:
        data = _pluggy_get(api_key, "/accounts", {"itemId": item_id})
        contas.extend(data.get("results", []))
    return contas


def pluggy_investimentos(api_key: str, item_ids: list[str]) -> float:
    """Total guardado em investimentos (renda fixa, fundos etc). Vem de /investments,
    não de /accounts — o dinheiro guardado não é saldo em conta, é reserva."""
    total = 0.0
    for item_id in item_ids:
        data = _pluggy_get(api_key, "/investments", {"itemId": item_id})
        for inv in data.get("results", []):
            if (inv.get("status") or "ACTIVE").upper() == "TOTAL_WITHDRAWAL":
                continue  # já resgatado por inteiro
            total += float(inv.get("balance") or 0)
    return total


def pluggy_fetch_items(api_key: str, contas: list[dict], since: str) -> list[dict[str, Any]]:
    """Transações das contas informadas desde `since` (YYYY-MM-DD), no formato do import.
    Sinal do valor: em conta de banco, negativo = gasto; em cartão de crédito é INVERTIDO
    (positivo = compra, negativo = pagamento da fatura OU estorno de compra)."""
    items = []
    for conta in contas:
        conta_id = conta.get("id")
        tipo_conta = conta.get("type")
        # Só conta corrente/poupança e cartão viram transações. Investimento/empréstimo
        # têm lógica própria (aporte, rendimento) e não entram como gasto/entrada aqui.
        if not conta_id or tipo_conta not in ("BANK", "CREDIT"):
            continue
        is_credit = tipo_conta == "CREDIT"
        # /v2/transactions: filtro por dateFrom (yyyy-mm-dd), sem pageSize (padrão já é 500,
        # o máximo) — cobre de sobra 90 dias de uso pessoal numa página só.
        data = _pluggy_get(api_key, "/v2/transactions",
                           {"accountId": conta_id, "dateFrom": since})
        for t in data.get("results", []):
            amount = t.get("amount")
            if amount is None:
                continue
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            # Mesmo cuidado do OFX: valor que não é número de verdade envenena
            # todo somatório do app, e não dá pra confiar que a outra ponta
            # sempre manda coisa sã.
            if amount == 0 or valor_absurdo(amount):
                continue
            desc = t.get("description") or t.get("descriptionRaw") or "Movimentação"
            if is_credit:
                if amount > 0:
                    tipo = "saida"                       # compra
                elif e_pagamento_de_fatura(desc):
                    continue                             # pagou a fatura: já saiu da conta
                else:
                    tipo = "entrada"                     # estorno: abate a fatura
            else:
                tipo = "entrada" if amount > 0 else "saida"
            # Parcelamento: a Pluggy manda "3 de 12" no metadado do cartão
            meta = t.get("creditCardMetadata") or {}
            p_num, p_total = meta.get("installmentNumber"), meta.get("totalInstallments")
            if not p_total:  # alguns bancos só escrevem na descrição
                p_num, p_total = detectar_parcela(desc)
            items.append({
                "valor": abs(float(amount)),
                "tipo": tipo,
                # Vazio viraria data_transacao = "", que o COALESCE não troca pelo
                # created_at: a movimentação some das telas por mês mas continua no saldo.
                "data": ((t.get("date") or "")[:10] or None),
                "descricao": sanitize_text(desc)[:120] or "Movimentação",
                "fitid": "PLG-" + str(t.get("id"))[:70],
                "no_credito": is_credit,
                "parcela_num": p_num,
                "parcela_total": p_total,
            })
    return items


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
     "feito": "Mais rápido que a flecha: seu banco entrega os gastos sozinho.",
     "como": "Conecte seu banco pelo Open Finance nas Configurações."},
    {"key": "javali", "emoji": "🐗", "nome": "O Javali de Erimanto",
     "feito": "Capturou o javali: fechou um mês no azul.",
     "como": "Termine um mês com as entradas maiores que as saídas."},
    {"key": "estabulos", "emoji": "🧹", "nome": "Os Estábulos de Augias",
     "feito": "Tudo limpo: nenhuma pendência, nenhuma conta vencida.",
     "como": "Não deixe nenhuma conta vencer."},
    {"key": "aves", "emoji": "🦅", "nome": "As Aves do Estínfale",
     "feito": "Espantou as aves: 10 movimentações entraram sem você digitar.",
     "como": "Sincronize o banco e deixe o Herc trabalhar por você."},
    {"key": "touro", "emoji": "🐂", "nome": "O Touro de Creta",
     "feito": "Domou o touro: fechou um mês sem estourar o teto do cartão.",
     "como": "Defina um teto de gasto no cartão e termine o mês dentro dele."},
    {"key": "eguas", "emoji": "🐎", "nome": "As Éguas de Diomedes",
     "feito": "Domou as éguas selvagens: um mês de estrada com o Herc.",
     "como": "Use o Herc por 30 dias com suas movimentações em dia."},
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


def _prev_month_bounds(dia_virada: int | None = None):
    """O mês passado DA PESSOA, em ISO.

    Tem que usar a mesma régua do mês atual. Com a virada configurada, o mês
    corrente virou ciclo e este aqui continuava no calendário — os dois se
    sobrepunham num dia e a comparação "gastei mais ou menos que mês passado"
    comparava períodos diferentes.
    """
    inicio_atual, _ = month_bounds(hoje_br(), dia_virada)
    fim_anterior = inicio_atual - timedelta(days=1)
    inicio_anterior, _ = month_bounds(fim_anterior, dia_virada)
    return inicio_anterior.isoformat(), fim_anterior.isoformat()


def _trabalho_conquistado(user_id: int, key: str, db) -> bool:
    ini, fim = _prev_month_bounds(virada_do_usuario(user_id))
    if key == "leao":
        cats = db.execute(
            "SELECT nome, limite_mensal FROM categorias WHERE user_id = ? AND limite_mensal > 0", (user_id,)
        ).fetchall()
        for cat in cats:
            gasto = db.execute(
                """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                   WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                     AND categoria = ?
                     AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
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
        # Banco conectado pelo Open Finance: os gastos passam a entrar sozinhos
        return db.execute(
            "SELECT 1 FROM usuarios WHERE id = ? AND pluggy_item_id IS NOT NULL AND pluggy_item_id != ''",
            (user_id,),
        ).fetchone() is not None
    if key == "javali":
        row = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN tipo='entrada' AND no_credito=0 THEN valor ELSE 0 END), 0) AS e,
                      COALESCE(SUM(CASE WHEN tipo='saida' THEN valor ELSE 0 END), 0) AS s,
                      COUNT(*) AS n
               FROM transacoes
               WHERE user_id = ? AND fonte != 'ajuste' AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, ini, fim),
        ).fetchone()
        return row["n"] > 0 and float(row["e"]) >= float(row["s"])
    if key == "estabulos":
        total = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if total < 10:
            return False
        vencida = db.execute(
            """SELECT 1 FROM compromissos WHERE user_id = ? AND status = 'pendente'
               AND date(vencimento) < date(?) LIMIT 1""",
            (user_id, hoje_br().isoformat()),
        ).fetchone()
        return vencida is None
    if key == "aves":
        # Movimentações que entraram sem digitação (banco/extrato)
        n = db.execute(
            "SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ? AND fonte = 'ofx'", (user_id,)
        ).fetchone()["n"]
        return n >= 10
    if key == "touro":
        # Fechou o mês passado dentro do teto de gasto do cartão que definiu
        row = db.execute("SELECT cartao_orcamento FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        teto = float(row["cartao_orcamento"] or 0) if row else 0.0
        if teto <= 0:
            return False
        fatura = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 1
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, ini, fim),
        ).fetchone()["t"]
        return 0 < float(fatura) <= teto
    if key == "eguas":
        # Um mês de estrada: conta com 30+ dias e uso real (20+ movimentações)
        row = db.execute(
            "SELECT CAST(julianday('now') - julianday(created_at) AS INTEGER) AS dias FROM usuarios WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row or (row["dias"] or 0) < 30:
            return False
        n = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ?", (user_id,)).fetchone()["n"]
        return n >= 20
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
            """SELECT COUNT(DISTINCT strftime('%Y-%m', COALESCE(NULLIF(data_transacao, ''), created_at))) AS n
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
                    (user_id, t["key"], hoje_br().isoformat()),
                )
                novos.append(t["key"])
    return novos


# Dicas do Herc: ensino contextual, uma frase por vez, some depois de vista
HERC_TIPS = {
    "registro_rapido": "Dica: quando um gasto ficar em “Outros”, abra Entradas e saídas e toque em “Ensinar” — eu aprendo e arrumo todos os parecidos de uma vez. 🎯",
    "primeira_nota": "Guardei sua nota! Sempre que precisar achar alguma, elas ficam todas aqui, organizadas. No fim do ano, é só exportar para o contador.",
    # Dicas que aparecem no MOMENTO em que a coisa acontece — é assim que se aprende
    "tem_outros": "Tem gasto que eu não reconheci e deixei em “Outros”. Abra Entradas e saídas e toque em "
                  "“Ensinar” — se você me disser uma vez que HOPS é Bebidas, eu acerto pra sempre. 🎯",
    "primeiro_credito": "Reparei que teve compra no crédito. Ela <strong>não sai do seu saldo agora</strong> — "
                        "vira fatura pra pagar depois. Por isso mostro as duas coisas separadas. 💳",
    "primeira_parcela": "Achei uma compra parcelada. Olha no card “já comprometido nas próximas faturas”: "
                        "é o quanto dos próximos meses já está reservado. 📅",
    "primeira_sync": "Seus gastos agora entram sozinhos do banco. Você não precisa anotar nada — "
                     "só olhar de vez em quando. 🦁",
    # Trilha do MEI
    "mei_primeira_nota": "Guardei sua nota. No <strong>Painel MEI</strong> ela já entra no seu faturamento "
                         "do ano — e no fim do ano você baixa tudo junto pro contador, num arquivo só. 🏪",
    "mei_sem_nota": "Você é MEI: sempre que emitir uma nota, guarde em <strong>Notas</strong>. É isso que "
                    "monta seu faturamento do ano e te avisa antes de chegar no limite. 🏪",
    "mei_limite": "Atenção: seu faturamento do ano já passou de 80% do limite do MEI. Vale conversar com "
                  "seu contador antes de estourar. ⚠️",
    "salario_na_virada": "Reparei que seu dinheiro entra no fim do mês. Pelo calendário ele conta no mês "
                         "que está acabando, mas na prática é o dinheiro do mês seguinte — então um mês "
                         "parece ótimo e o outro parece um desastre. Em <strong>Configurações</strong> "
                         "você diz o dia em que recebe, e eu passo a fechar o mês junto com você. 📅",
}


def salario_perto_da_virada(user_id: int) -> bool:
    """A pessoa recebe nos últimos dias do mês e ainda não avisou o app?

    Quem recebe assim vive um mês que não é o do calendário. Enquanto não
    configura, todo mês de salário fecha lindo e o seguinte parece um desastre —
    e ela não tem como saber que isso é ajustável se ninguém contar.
    """
    if virada_do_usuario(user_id):
        return False
    col = "COALESCE(NULLIF(data_transacao, ''), created_at)"
    with get_db() as db:
        n = db.execute(
            f"""SELECT COUNT(DISTINCT strftime('%Y-%m', {col})) AS n
                  FROM transacoes
                 WHERE user_id = ? AND tipo = 'entrada' AND no_credito = 0
                   AND fonte != 'ajuste' AND valor >= 100
                   AND CAST(strftime('%d', {col}) AS INTEGER) >=
                       CAST(strftime('%d', date({col}, 'start of month', '+1 month', '-1 day'))
                            AS INTEGER) - 2""",
            (user_id,),
        ).fetchone()["n"]
    # Dois meses seguidos é padrão; um só pode ser coincidência, e sugerir mudar
    # o mês inteiro por causa de uma entrada solta seria atrapalhar.
    return (n or 0) >= 2


def tip_seen(user_id: int, key: str) -> bool:
    with get_db() as db:
        return db.execute(
            "SELECT 1 FROM dicas_vistas WHERE user_id = ? AND dica = ?", (user_id, key)
        ).fetchone() is not None



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


def _normalizar_regra(texto: str) -> str:
    """Deixa 'IFD*IFOOD', 'ifd ifood' e 'IFD-IFOOD' com a mesma cara, dos DOIS lados
    da comparação — senão a regra ensinada não casa com o texto do extrato."""
    t = _strip_accents((texto or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def apply_rules(user_id: int, *texts: str | None, regras=None) -> str | None:
    """Regra aprendida vence tudo. Compara normalizado (sem acento, caixa ou símbolo).

    `regras` evita reler o banco quando isto roda em cima de milhares de linhas —
    passe o resultado de user_rules() uma vez e reaproveite."""
    haystack = _normalizar_regra(" ".join(t for t in texts if t))
    if not haystack:
        return None
    for rule in (user_rules(user_id) if regras is None else regras):
        if rule["categoria_nome"] == IGNORE_RULE:
            continue
        padrao = _normalizar_regra(rule["padrao_texto"])
        if padrao and padrao in haystack:
            return rule["categoria_nome"]
    return None


# Lixo que o extrato gruda no nome do lugar e atrapalha o "ensinar"
_RUIDO_PADRAO = re.compile(
    r"(\d{1,2}\s*/\s*\d{1,4}"                    # 12/07, 3/12
    r"|\bparc(?:ela)?\.?\b|\bparcela\b"
    r"|\b\d{2}[/-]\d{2}(?:[/-]\d{2,4})?\b"        # datas
    r"|\bltda\b|\bme\b|\bs\.?a\.?\b|\beireli\b|\bcia\b"
    r"|\*+|\bn[o°º]?\s*\d+\b|\b\d{4,}\b)",        # códigos e números longos
    re.IGNORECASE)


def padrao_sugerido(texto: str) -> str:
    """Sugere o TRECHO QUE SE REPETE do nome do lugar. Sem isso o usuário ensina
    'HOPS BAR 12/07' — que nunca mais vai aparecer igual — e acha que não aprendeu."""
    base = _RUIDO_PADRAO.sub(" ", texto or "")
    base = re.sub(r"[^\wÀ-ÿ\s.&-]", " ", base)     # tira símbolos soltos, mantém letras
    palavras = [p for p in re.split(r"\s+", base.strip()) if len(p) > 1]
    if not palavras:
        return sanitize_text(texto)[:40]
    return " ".join(palavras[:2])[:40]


def categorize(user_id: int, *texts: str | None, regras=None) -> str:
    """Ordem de decisão: regras que o usuário ensinou > palavras-chave genéricas."""
    return (apply_rules(user_id, *texts, regras=regras)
            or auto_category(" ".join(t for t in texts if t)))


def pending_suggestions(user_id: int, limit: int = 2):
    """Gastos repetidos que caíram em 'Outros': o Hércules pergunta uma vez o que são."""
    with get_db() as db:
        rows = db.execute(
            """SELECT LOWER(TRIM(COALESCE(NULLIF(estabelecimento, ''), descricao))) AS padrao,
                      MAX(COALESCE(NULLIF(estabelecimento, ''), descricao)) AS display,
                      COUNT(*) AS vezes,
                      SUM(valor) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                 AND COALESCE(NULLIF(categoria, ''), 'Outros') = 'Outros'
                 AND COALESCE(NULLIF(estabelecimento, ''), descricao) IS NOT NULL
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date('now', '-60 day')
               GROUP BY padrao
               HAVING COUNT(*) >= 3
               ORDER BY total DESC""",
            (user_id,),
        ).fetchall()
    known = {r["padrao_texto"].lower() for r in user_rules(user_id)}
    return [r for r in rows if r["padrao"] not in known][:limit]


def reclassify_transactions(user_id: int, pattern: str, categoria: str) -> int:
    """Aplica uma regra nova ao passado. Compara normalizado (o LIKE do banco não
    ignora acento nem símbolo, então 'IFD*IFOOD' escapava)."""
    alvo = _normalizar_regra(pattern)
    if not alvo:
        return 0
    mudadas = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT id, descricao, estabelecimento, categoria FROM transacoes WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for r in rows:
            if r["categoria"] == categoria:
                continue
            texto = _normalizar_regra(f"{r['descricao'] or ''} {r['estabelecimento'] or ''}")
            if alvo in texto:
                db.execute("UPDATE transacoes SET categoria = ? WHERE id = ?", (categoria, r["id"]))
                mudadas += 1
    return mudadas


def category_month_spending(user_id: int) -> dict[str, float]:
    month_start, month_end = mes_do_usuario(user_id)
    with get_db() as db:
        rows = db.execute(
            """SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS categoria, SUM(valor) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)
               GROUP BY categoria""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return {r["categoria"]: float(r["total"] or 0) for r in rows}


def _strip_accents(s: str) -> str:
    """'Ônibus' e 'ONIBUS' viram 'onibus' — o extrato do banco vem sem padrão de acento/caixa."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Palavras/marcas que aparecem de verdade em extrato brasileiro. ORDEM = prioridade
# (primeira que casar vence). Tudo minúsculo e SEM acento (o texto é normalizado antes).
# Marca entre \b...\b vira busca por palavra inteira (evita "99" pegar valor, "claro" pegar frase).
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    # Assinaturas antes de Lazer, pra streaming cair aqui (é assinatura recorrente).
    ("Assinaturas", ["netflix", "spotify", "disney+", "disney plus", "hbo", "amazon prime", "prime video",
                     "globoplay", "youtube premium", "deezer", "paramount", "star+", "apple.com/bill",
                     "apple music", "icloud", "google one", "playstation plus", "game pass",
                     "canva", "chatgpt", "openai", "notion", "dropbox", "assinatura", "subscription"]),
    ("Saúde", ["farmacia", "drogaria", "drogasil", "droga raia", "drogaraia", "pacheco", "pague menos",
               "ultrafarma", "panvel", "nissei", "hospital", "clinica", "medic", "consultorio", "consulta medic",
               "odonto", "dentist", "psicolog", "psiquiatr", "laboratorio", "exame", "fleury", "sabin",
               "hermes pardini", "unimed", "hapvida", "amil", "otica", "oculos", "academia", "smartfit",
               "smart fit", "bodytech", "bio ritmo", "bioritmo", "selfit", "bluefit", "gympass",
               "totalpass", "vacina", "fisioterap", "posto de saude"]),
    ("Educação", ["escola", "colegio", "faculdade", "universidade", "curso", "udemy", "alura", "coursera",
                  "hotmart", "livraria", "livro", "papelaria", "apostila", "kumon", "wizard", "ccaa",
                  "fisk", "cultura inglesa", "aula", "treinamento", "ensino", "creche", "bercario",
                  "material escolar"]),
    ("Transporte", ["uber", "99app", "99 pop", "99pop", "99 tecnolog", "99*", "cabify", "indriver", "in driver",
                    "onibus", "metro", "cptm", "sptrans", "riocard", "mais.mobi", "mais mobi", "jae.com",
                    "bilhete unico", "cartao transporte",
                    r"\bbrt\b", "vlt", "supervia", "trensurb", "recarga transporte", "passagem", "rodoviaria",
                    "viacao", "buser", "clickbus", "posto", "auto posto", "ipiranga", "shell", "petrobras",
                    "br mania", "gasolina", "combustivel", "etanol", "diesel", "estacionamento", "estapar",
                    "estacione", "zona azul", "zul+", "pedagio", "sem parar", "conectcar", "veloe", "taggy",
                    "autopass", "tembici", "yellow bike", "taxi"]),
    ("Alimentação", ["quentinha", "marmita", "restaurante", "lanchonete", "lanche", "padaria", "panificadora",
                     "pizzaria", "pizza", "hamburgueria", "burger", "hamburg", "ifood", "rappi", "aiqfome",
                     "delivery", r"\bbar\b", "boteco", "botequim", "choperia", "cafeteria", "confeitaria",
                     "doceria", "sorveteria", "sorvete", "acai", "esfiha", "esfirra", "pastel", "coxinha",
                     "salgad", "sushi", "temaki", "churrascaria", "espetinho", "mcdonald", "mc donald", "burger king",
                     "subway", "habib", "giraffas", "spoleto", "outback", "madero", "coco bambu", "kfc",
                     "bobs", "dominos", "pizza hut", "starbucks", "comida", "gelato"]),
    ("Moradia", ["aluguel", "condominio", "condom", "imobiliaria", "enel", "cemig", "cpfl", "celpe", "coelba",
                 "copel", "energisa", "equatorial", "neoenergia", "energia eletrica", "conta de luz", "sabesp",
                 "cedae", "sanepar", "caesb", "cagece", "embasa", "conta de agua", "comgas", "ultragaz",
                 "liquigas", "consigaz", "supergasbras", "iptu", "internet", "vivo fibra", "oi fibra",
                 "net virtua", r"\bvivo\b", r"\bclaro\b", r"\btim\b", "faxina", "diarista", "reforma",
                 "material de construcao", "leroy merlin", "telha norte", "obramax"]),
    ("Lazer", ["cinema", "cinemark", "kinoplex", "ingresso", "sympla", "eventim", "show", "teatro", "museu",
               "parque", "hopi hari", "beto carrero", "viagem", "hotel", "pousada", "airbnb", "booking",
               "decolar", "hurb", "cvc", "123 milhas", "latam", "gol linhas", "azul viagens", "steam",
               "playstation", "xbox", "nintendo", "epic games", "boliche", "balada"]),
    # Varejo antes de Mercado: "mercado livre" é loja online, não supermercado.
    ("Varejo", ["mercado livre", "mercadolivre", "amazon", "shopee", "aliexpress", "shein", "magalu",
                "magazine luiza", "americanas", "casas bahia", "ponto frio", "submarino", "kabum", "pichau",
                "renner", "riachuelo", "marisa", "pernambucanas", "zara", "hering", "nike", "adidas",
                "centauro", "netshoes", "decathlon", "kalunga", "havan", "vivara", "pandora", "shopping",
                "loja"]),
    ("Mercado", ["supermercado", "hipermercado", "atacadao", "atacad", "assai", "carrefour", "pao de acucar",
                 "sacolao", "hortifruti", "acougue", "quitanda", "mercearia", "mercadinho", "sams club",
                 "sam's club", "makro", "tenda atacado", "bompreco", "guanabara", "prezunic", "mundial",
                 "mercado"]),
    ("Serviços", ["barbearia", "cabeleireiro", "salao de beleza", "manicure", "estetica", "lavanderia",
                  "chaveiro", "correios", "sedex", "cartorio", "advocacia", "advogad", "contador", "contabil",
                  "consultoria", "manutencao", "assistencia tecnica", "conserto", "oficina", "borracharia",
                  "grafica", "petshop", "pet shop", "veterinari", "banho e tosa", "freela", "software"]),
]


def auto_category(text: str) -> str:
    txt = _strip_accents((text or "").lower())
    if not txt:
        return "Outros"
    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw.startswith("\\b"):
                if re.search(kw, txt):
                    return category
            elif kw in txt:
                return category
    return "Outros"


def recategorize_outros(user_id: int) -> int:
    """Re-roda a categorização automática nas saídas que ficaram em 'Outros' — útil depois de
    melhorar as palavras-chave ou de ensinar regras novas. Só mexe no que virar categoria de verdade;
    respeita as regras aprendidas (categorize() aplica regra antes das palavras-chave)."""
    changed = 0
    # Uma leitura das regras para todas as linhas: antes abria uma conexão POR
    # transação, e quem tem um ano de banco sincronizado esperava vários segundos.
    regras = user_rules(user_id)
    with get_db() as db:
        rows = db.execute(
            """SELECT id, descricao, estabelecimento FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste'
                 AND COALESCE(NULLIF(categoria, ''), 'Outros') = 'Outros'""",
            (user_id,),
        ).fetchall()
        for r in rows:
            cat = categorize(user_id, r["descricao"], r["estabelecimento"], regras=regras)
            if cat and cat != "Outros":
                db.execute("UPDATE transacoes SET categoria = ? WHERE id = ?", (cat, r["id"]))
                changed += 1
    return changed


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


# A cada quantos dias o Herc lembra de importar o extrato, pra nada passar batido.
OFX_LEMBRETE_DIAS = 7


def dias_desde_ultimo_ofx(user) -> int | None:
    """None = nunca importou (sempre lembra); caso contrário, dias desde a última vez."""
    last = user["last_ofx_import"] if "last_ofx_import" in user.keys() else None
    if not last:
        return None
    try:
        return (hoje_br() - date.fromisoformat(last)).days
    except ValueError:
        return None


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


def month_bounds(dt: date | None = None, dia_virada: int | None = None):
    """Começo e fim do mês DA PESSOA.

    Sem `dia_virada`, é o mês do calendário — o de sempre. Com ele, o mês vira
    um ciclo, igual ao da fatura: quem recebe no dia 31 tem um mês que vai do 31
    ao 30, e o salário daquele dia conta como dinheiro do mês que começa, não do
    que termina. Sem isso, o mês do salário fecha lindo e o seguinte parece uma
    catástrofe — todo mês."""
    dt = dt or hoje_br()
    if not dia_virada or dia_virada <= 1:
        first = dt.replace(day=1)
        last = dt.replace(day=calendar.monthrange(dt.year, dt.month)[1])
        return first, last

    vira_neste = _dia_do_mes(dt.year, dt.month, dia_virada)
    if dt >= vira_neste:
        prox = (dt.replace(day=1) + timedelta(days=32)).replace(day=1)
        return vira_neste, _dia_do_mes(prox.year, prox.month, dia_virada) - timedelta(days=1)
    ant = dt.replace(day=1) - timedelta(days=1)
    return _dia_do_mes(ant.year, ant.month, dia_virada), vira_neste - timedelta(days=1)


def sql_mes(virada: int | None, col: str) -> str:
    """Em que mês da PESSOA cai uma data, em SQL.

    Sem virada é o mês do calendário. Com virada, quem cai no dia da virada ou
    depois já pertence ao mês seguinte — é assim que o salário do dia 31 vira
    dinheiro de agosto. O `min(...)` existe para meses curtos: virada 31 em um
    mês de 30 dias vira o dia 30, senão fevereiro nunca viraria.
    """
    if not virada or virada <= 1:
        return f"strftime('%Y-%m', {col})"
    virada = max(1, min(31, int(virada)))
    ultimo = f"CAST(strftime('%d', date({col}, 'start of month', '+1 month', '-1 day')) AS INTEGER)"
    return (f"CASE WHEN CAST(strftime('%d', {col}) AS INTEGER) >= min({virada}, {ultimo})"
            f" THEN strftime('%Y-%m', date({col}, 'start of month', '+1 month'))"
            f" ELSE strftime('%Y-%m', {col}) END")


def virada_do_usuario(user_id: int) -> int | None:
    with get_db() as db:
        r = db.execute("SELECT dia_virada FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    return (r["dia_virada"] if r and "dia_virada" in r.keys() else None) or None


def mes_do_usuario(user_id: int, dt: date | None = None):
    return month_bounds(dt, virada_do_usuario(user_id))


# Acima disso é dedo escorregando no teclado, não dinheiro. O app é de finança
# pessoal; sem teto, um valor absurdo contamina saldo, média e gráfico de todo
# mundo que olhar aquela tela.
VALOR_MAX = 1_000_000_000.0


def valor_absurdo(v: float) -> bool:
    """Bate no teto = quase certamente dedo escorregando. Melhor recusar e dizer,
    do que aceitar calado e envenenar saldo, média e gráfico da pessoa.

    NaN entra aqui explicitamente porque `nan >= X` é False — ele passava pela
    única guarda que existia e ia direto pro banco.
    """
    try:
        if v != v:                      # NaN não é igual a si mesmo
            return True
        return abs(v) >= VALOR_MAX      # infinito cai aqui
    except (TypeError, ValueError):
        return True


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
        n = float(text)
    except ValueError:
        return 0.0
    # inf/nan passam pelo float() e envenenam qualquer soma que os encontre
    if n != n or n in (float("inf"), float("-inf")):
        return 0.0
    return max(-VALOR_MAX, min(VALOR_MAX, n))


def days_left_in_month(dia_virada: int | None = None) -> int:
    """Dias que faltam até o mês virar — o da pessoa, se ela tiver um."""
    hoje = hoje_br()
    return max(1, (month_bounds(hoje, dia_virada)[1] - hoje).days + 1)


def months_until(deadline: str | None) -> int | None:
    """Meses (arredondando para cima, mínimo 1) entre hoje e o prazo ISO. None se sem prazo válido."""
    if not deadline:
        return None
    try:
        target = date.fromisoformat(deadline)
    except ValueError:
        return None
    today = hoje_br()
    if target <= today:
        return 1
    months = (target.year - today.year) * 12 + (target.month - today.month)
    if target.day > today.day:
        months += 1
    return max(1, months)



def calc_transaction_totals(user_id: int):
    today = hoje_br()
    virada = virada_do_usuario(user_id)
    month_start, month_end = month_bounds(today, virada)
    with get_db() as db:
        # Só interessa SE existe alguma — quem precisa da lista usa `recent` (LIMIT 8)
        # ou a tela de lançamentos, que é paginada. Carregar a tabela inteira aqui fazia
        # a Início ficar mais lenta a cada mês de uso, sem nada em troca.
        tem_lancamentos = db.execute(
            "SELECT 1 FROM transacoes WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone() is not None
        month_income = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'entrada' AND no_credito = 0 AND fonte != 'ajuste'
                 AND interno = 0
               AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        # Compra no crédito não sai da conta agora — fica de fora do saldo e do
        # "quanto posso gastar hoje" até a fatura ser paga de verdade.
        month_expenses = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND fonte != 'ajuste'
                 AND interno = 0
               AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        # O saldo e o UNICO lugar que conta 'ajuste', e e pra isso que ele existe:
        # quando o app nao bate com o banco, a pessoa corrige e vira um movimento
        # sintetico aqui. Em relatorio de gasto ele nao entra — nao foi gasto.
        balance = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN tipo = 'entrada' THEN valor ELSE -valor END), 0) AS total
               FROM transacoes WHERE user_id = ? AND no_credito = 0""",
            (user_id,),
        ).fetchone()["total"]
        # Fatura: se a pessoa (ou o banco) informou o dia de fechamento, conta pelo CICLO
        # real do cartão; senão, cai no mês do calendário.
        cartao = db.execute(
            "SELECT cartao_fechamento, cartao_vencimento FROM usuarios WHERE id = ?", (user_id,)
        ).fetchone()
        dia_fecha = (cartao["cartao_fechamento"] if cartao and "cartao_fechamento" in cartao.keys() else None)
        if dia_fecha:
            fat_ini, fat_fim = ciclo_fatura(hoje_br(), int(dia_fecha))
        else:
            fat_ini, fat_fim = month_start, month_end

        def _soma_credito(ini, fim):
            return float(db.execute(
                """SELECT COALESCE(SUM(CASE WHEN tipo = 'saida' THEN valor ELSE -valor END), 0) AS total
                     FROM transacoes
                    WHERE user_id = ? AND no_credito = 1
                      AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
                (user_id, ini.isoformat(), fim.isoformat()),
            ).fetchone()["total"])

        fatura_credito_mes = _soma_credito(fat_ini, fat_fim)
        # A fatura que FECHOU ainda precisa ser paga — some logo depois do fechamento
        # e a pessoa acha que sumiu dinheiro. Mostramos as duas.
        fatura_fechada = None
        if dia_fecha:
            fim_ant = fat_ini - timedelta(days=1)
            ini_ant = ciclo_fatura(fim_ant, int(dia_fecha))[0]
            valor_ant = _soma_credito(ini_ant, fim_ant)
            if valor_ant > 0:
                fatura_fechada = {"valor": valor_ant, "fechou_em": fim_ant}
        monthly_by_category = db.execute(
            """SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS categoria,
                      SUM(valor) AS total
                 FROM transacoes
                WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                  AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)
                GROUP BY categoria
               HAVING total > 0
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
            "SELECT * FROM transacoes WHERE user_id = ? ORDER BY date(COALESCE(NULLIF(data_transacao, ''), created_at)) DESC LIMIT 8",
            (user_id,),
        ).fetchall()

    due_soon_cutoff = hoje_br() + timedelta(days=7)
    overdue_commitments = [c for c in upcoming_commitments if c["vencimento"] and date.fromisoformat(c["vencimento"]) < hoje_br()]
    due_soon_commitments = [
        c for c in upcoming_commitments
        if c["vencimento"] and hoje_br() <= date.fromisoformat(c["vencimento"]) <= due_soon_cutoff
    ]
    commitments_total = sum(float(c["valor"]) for c in due_soon_commitments)
    # As dos 7 dias servem pro card "contas próximas". Mas o que sobra NO MÊS tem
    # que descontar tudo que vence no mês — senão o aluguel do dia 20 fica
    # invisível no dia 2, e o app diz que dá pra gastar mais do que dá.
    contas_do_mes = sum(
        float(c["valor"]) for c in upcoming_commitments
        if c["vencimento"] and date.fromisoformat(c["vencimento"]) <= month_end
    )
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
        user_row = db.execute("SELECT meta_mensal, cartao_orcamento, saldo_banco, saldo_investido FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        month_reserve_saved = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND fonte != 'ajuste'
                    AND interno = 0
                 AND categoria = 'Reserva'
               AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]

    # Banco conectado (Pluggy): o saldo REAL dele é a fonte da verdade — não o acumulado
    # de saldo inicial + movimentos (que conflita quando importamos histórico do banco).
    saldo_banco = user_row["saldo_banco"] if (user_row and "saldo_banco" in user_row.keys()) else None
    if saldo_banco is not None:
        balance = float(saldo_banco)

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
    remaining_month = float(balance) - contas_do_mes
    # Com reserva: o que dá para gastar sem comprometer o que precisa ser guardado
    spendable_month = remaining_month - reserve_remaining_month
    available_today = max(0.0, spendable_month / days_left_in_month(virada))
    # Abaixo disso o número deixa de ser conselho e vira piada: com R$ 0,50 na
    # conta o app dizia "pode gastar R$ 0,02 hoje". Melhor admitir que não dá
    # pra dividir do que fingir precisão.
    diaria_util = available_today >= 1.0
    available_today_no_reserve = max(0.0, remaining_month / days_left_in_month(virada))

    # Previsão: guardando o necessário por mês, quando a meta fica completa
    goal_forecast_months = None
    if goal_missing > 0 and reserve_monthly_needed > 0:
        goal_forecast_months = int(-(-goal_missing // reserve_monthly_needed))  # ceil

    # Cartão: controle ao longo do mês — quanto da fatura já bateu no teto que a pessoa
    # definiu, e quanto da renda do mês já está comprometida no crédito (a dor real).
    cartao_orcamento = float(user_row["cartao_orcamento"] or 0) if (user_row and "cartao_orcamento" in user_row.keys()) else 0.0
    fatura_atual = float(fatura_credito_mes or 0)
    fatura_pct_orcamento = (fatura_atual / cartao_orcamento * 100) if cartao_orcamento > 0 else None
    credito_pct_renda = (fatura_atual / float(month_income) * 100) if (month_income and float(month_income) > 0) else None

    stats = {
        "tem_lancamentos": tem_lancamentos,
        "month_income": float(month_income or 0),
        "month_expenses": float(month_expenses or 0),
        "balance": float(balance or 0),
        "fatura_credito_mes": float(fatura_credito_mes or 0),
        "fatura_por_ciclo": bool(dia_fecha),
        "fatura_fechada": fatura_fechada,
        "fatura_periodo": (fat_ini, fat_fim) if dia_fecha else None,
        "fatura_fecha_em": fat_fim if dia_fecha else None,
        "fatura_dias_p_fechar": (fat_fim - hoje_br()).days if dia_fecha else None,
        "fatura_vence_dia": (cartao["cartao_vencimento"]
                             if (cartao and "cartao_vencimento" in cartao.keys()) else None),
        "saldo_investido": (float(user_row["saldo_investido"])
                            if (user_row and "saldo_investido" in user_row.keys() and user_row["saldo_investido"] is not None)
                            else None),
        "cartao_orcamento": cartao_orcamento,
        "fatura_pct_orcamento": fatura_pct_orcamento,
        "credito_pct_renda": credito_pct_renda,
        "monthly_by_category": monthly_by_category,
        "upcoming_commitments": upcoming_commitments,
        "overdue_commitments": overdue_commitments,
        "due_soon_commitments": due_soon_commitments,
        "commitments_total": float(commitments_total),
        "contas_do_mes": float(contas_do_mes),
        # Quantos dias esse número está dividindo. Sem isso a tela não consegue
        # explicar a conta, e a pessoa tem que adivinhar de onde vem o valor.
        "dias_restantes": days_left_in_month(virada),
        "goals": goals,
        "notes": notes,
        "recent_transactions": recent,
        "goal_active": goal_active,
        "goal_progress": goal_progress,
        "current_goal": current_goal,
        "remaining_month": float(remaining_month),
        "spendable_month": float(spendable_month),
        "available_today": float(available_today),
        "diaria_util": diaria_util,
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

    if not descricao or valor <= 0 or valor_absurdo(valor):
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
    today = hoje_br()
    month_start, month_end = mes_do_usuario(user_id, today)
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
               WHERE user_id = ? AND tipo = 'entrada' AND no_credito = 0 AND fonte != 'ajuste'
               AND interno = 0
               AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, month_start.isoformat(), month_end.isoformat()),
        ).fetchone()["total"]
        expenses_month = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND fonte != 'ajuste'
               AND interno = 0
               AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
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
        if c["status"] == "pendente" and c["vencimento"] and date.fromisoformat(c["vencimento"]) <= hoje_br() + timedelta(days=7)
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
app.jinja_env.filters["money"] = money_html
# Dentro de atributo HTML (data-confirm, title, alt) o <span> do money_html vira
# marcação quebrada. Aqui vai o valor pelado.
app.jinja_env.filters["money_texto"] = money
app.jinja_env.globals["padrao_sugerido"] = padrao_sugerido
app.jinja_env.filters["format_date"] = format_date
app.jinja_env.filters["month_label"] = month_label
app.jinja_env.globals["csrf_token"] = generate_csrf_token
app.jinja_env.globals["profile_choices"] = PROFILE_CHOICES
app.jinja_env.globals["transaction_types"] = TRANSACTION_TYPES
app.jinja_env.globals["note_categories"] = NOTE_CATEGORIES
app.jinja_env.globals["transaction_categories"] = TRANSACTION_CATEGORIES
app.jinja_env.globals["income_categories"] = INCOME_CATEGORIES

app.jinja_env.globals["date"] = date


# A Pluggy injeta o widget de conexão; o resto vem tudo daqui de casa.
# 'unsafe-inline' em script é uma concessão real: o app tem scripts inline em 10
# telas. Mesmo assim o CSP ainda barra script vindo de domínio estranho, que é o
# vetor de XSS que importa aqui.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.pluggy.ai",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "connect-src 'self' https://api.pluggy.ai",
    "frame-src https://cdn.pluggy.ai https://connect.pluggy.ai",
    "form-action 'self'",
    "base-uri 'self'",          # impede <base> injetado redirecionar formulários
    "object-src 'none'",
    "frame-ancestors 'none'",   # ninguém embute o Hércules num iframe
])


@app.after_request
def cabecalhos_de_seguranca(resp):
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # Sem isso, o endereço da página vaza no Referer ao clicar num link externo
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=(), payment=()")
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


@app.before_request
def rotina_diaria():
    """Uma vez por dia, por usuário: garante o backup e anota que a pessoa voltou.
    Nada aqui pode derrubar a requisição — é manutenção, não é o app."""
    if request.endpoint in (None, "static"):
        return None
    try:
        garantir_backup_do_dia()
    except Exception:
        pass

    uid = session.get("user_id")
    hoje = hoje_br().isoformat()
    if uid and session.get("visto_em") != hoje:
        session["visto_em"] = hoje
        try:
            with get_db() as db:
                db.execute("UPDATE usuarios SET last_seen = ? WHERE id = ?", (hoje, uid))
        except Exception:
            pass
    return None


def voltar_para(padrao: str) -> str:
    """Para onde mandar a pessoa de volta, aceitando só endereço DESTE app.

    `request.referrer` vem do cabeçalho Referer, e quem faz a requisição escolhe
    o que põe ali. Confiar nele transformava o Hércules em trampolim: um link
    pro domínio do app cuspia a pessoa em outro site, com o domínio confiável
    aparecendo antes — que é exatamente o truque de um phishing bom.
    """
    ref = request.referrer or ""
    if ref:
        try:
            alvo = urlparse(ref)
        except ValueError:
            alvo = None
        # Sem host = caminho relativo, já é daqui. Com host, tem que ser o nosso.
        if alvo and alvo.scheme in ("", "http", "https") and (
                not alvo.netloc or alvo.netloc == request.host):
            if not ref.startswith("//"):
                return ref
    return padrao


@app.before_request
def csrf_protect():
    if request.method == "POST":
        exempt = request.endpoint in {"logout",
                                      "passkey_registrar", "passkey_entrar",
                                      "passkey_registrar_opcoes", "passkey_entrar_opcoes"}
        token = request.form.get("csrf_token", "")
        if not exempt and token != session.get("csrf_token"):
            # "Token de segurança inválido" nao diz nada pra quem nao e' programador,
            # e quem cai aqui geralmente so deixou a pagina aberta tempo demais.
            flash("Essa página ficou aberta tempo demais e expirou por segurança. "
                  "Atualize e tente de novo — nada do que você já salvou foi perdido.")
            return redirect(voltar_para(url_for("home" if "user_id" in session else "login")))


ERROS_LOG = Path(os.environ.get("ERROS_LOG") or BASE_DIR / "erros.log")
ERROS_MAX_BYTES = 512 * 1024


def registrar_erro(e) -> str:
    """Guarda o traceback e devolve um código curto. A pessoa lê o código na tela
    e me manda por mensagem — assim dá pra achar o erro dela no meio dos outros."""
    codigo = uuid.uuid4().hex[:6].upper()
    quando = agora_br().isoformat(timespec="seconds")
    try:
        # NUNCA gravar o corpo da requisição: ele carrega valor, descrição, senha.
        # O que interessa pra depurar é onde quebrou, não o que a pessoa digitou.
        cabecalho = (f"\n{'=' * 70}\n[{quando}] {codigo}  {request.method} {request.path}\n"
                     f"user_id={session.get('user_id')}\n")
        if ERROS_LOG.exists() and ERROS_LOG.stat().st_size > ERROS_MAX_BYTES:
            ERROS_LOG.replace(ERROS_LOG.with_suffix(".log.1"))
        with open(ERROS_LOG, "a", encoding="utf-8") as f:
            f.write(cabecalho + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
    except Exception:
        pass
    app.logger.exception("Erro %s em %s %s", codigo, request.method, request.path)
    return codigo


def erros_recentes(limite=15):
    """As últimas quebras, mais nova primeiro — pro painel de saúde."""
    if not ERROS_LOG.exists():
        return []
    try:
        blocos = ERROS_LOG.read_text(encoding="utf-8", errors="replace").split("=" * 70)
    except OSError:
        return []
    saida = []
    for bloco in reversed(blocos):
        linhas = [l for l in bloco.strip().splitlines() if l.strip()]
        if not linhas:
            continue
        ultima = next((l for l in reversed(linhas) if l and not l.startswith(" ")), linhas[-1])
        saida.append({"cabecalho": linhas[0], "erro": ultima[:160]})
        if len(saida) >= limite:
            break
    return saida


# Depois de quantos minutos parado o app pede a digital de novo
DESBLOQUEIO_MINUTOS = 30


def _marcar_desbloqueado():
    session["desbloqueado_em"] = agora_br().isoformat(timespec="seconds")


def _desbloqueio_valido() -> bool:
    """Janela deslizante: usando o app não incomoda; parado, tranca sozinho.
    (Não pode ser um 'true' eterno — o cookie dura 90 dias e a proteção sumiria.)"""
    marca = session.get("desbloqueado_em")
    if not marca:
        return False
    try:
        return agora_br() - datetime.fromisoformat(marca) < timedelta(minutes=DESBLOQUEIO_MINUTOS)
    except ValueError:
        return False


@app.before_request
def exigir_desbloqueio():
    """Com digital cadastrada, o app pede o dedo antes de mostrar qualquer dado —
    protege quem pega o celular já destravado."""
    if "user_id" not in session:
        return None
    livres = {"logout", "app_bloqueado", "static", "passkey_entrar", "passkey_entrar_opcoes",
              "passkey_remover", "privacidade", "ajuda"}
    if request.endpoint in livres:
        return None
    if not app_tem_bloqueio(session["user_id"]):
        return None
    if _desbloqueio_valido():
        _marcar_desbloqueado()  # renova enquanto estiver usando
        return None
    if request.method != "GET":
        return {"erro": "app bloqueado"}, 401
    return redirect(url_for("app_bloqueado"))


# ------------------------
# Bootstrap
# ------------------------
init_db()


# ------------------------
# Auth
# ------------------------
# Permissivo de proposito: e-mail aceita +, ponto, acento e domínio composto.
# A regra só barra o que claramente não é endereço — "abc", "@", "a@b" — porque
# quem erra o próprio e-mail no cadastro fica sem recuperação possível depois.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def email_invalido(email: str) -> bool:
    return not _EMAIL_RE.match(email or "")


SENHA_MINIMA = 8
# As que aparecem em toda lista de senha vazada. Não é uma lista completa —
# é pra impedir a escolha preguiçosa de quem está com pressa no cadastro.
_SENHAS_OBVIAS = {
    "12345678", "123456789", "1234567890", "123456", "senha123", "senha1234",
    "password", "password1", "qwerty123", "abc12345", "11111111", "00000000",
    "mudar123", "hercules", "brasil123", "10203040", "1q2w3e4r", "admin123",
}


def senha_fraca(senha: str, email: str = "", nome: str = "") -> str | None:
    """Devolve o motivo da recusa, ou None se a senha serve.

    O app guarda dinheiro e nota fiscal: uma senha de 6 dígitos numéricos sai
    em minutos numa lista de vazamento. Não exijo símbolo nem maiúscula — regra
    difícil faz a pessoa escrever a senha num papel colado no monitor."""
    if len(senha) < SENHA_MINIMA:
        return f"A senha precisa de pelo menos {SENHA_MINIMA} caracteres."
    baixa = senha.lower()
    if baixa in _SENHAS_OBVIAS:
        return "Essa senha é das mais usadas do mundo — escolha outra."
    if senha.isdigit():
        return "Só números é fácil de descobrir. Misture letras."
    if len(set(baixa)) <= 2:
        return "Essa senha repete o mesmo caractere. Escolha outra."
    if email and baixa == email.split("@")[0].lower():
        return "A senha não pode ser o seu e-mail."
    if nome and len(nome) >= 4 and baixa == nome.split(" ")[0].lower():
        return "A senha não pode ser o seu nome."
    return None


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
        if email_invalido(email):
            flash("Esse e-mail não parece completo. Confira antes de continuar.")
            return redirect(url_for("register"))
        problema = senha_fraca(senha, email, nome)
        if problema:
            flash(problema)
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
    return render_template("register.html", google_login_enabled=oauth is not None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = sanitize_text(request.form.get("email")).lower()
        senha = request.form.get("senha", "")

        faltam = login_bloqueado(email)
        if faltam:
            flash(f"Muitas tentativas seguidas. Por segurança, espere {faltam} minuto"
                  f"{'s' if faltam > 1 else ''} e tente de novo.")
            return redirect(url_for("login"))

        with get_db() as db:
            user = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["senha"], senha):
            registrar_tentativa(email, True)
            _start_session(user)
            return redirect(url_for("home"))
        registrar_tentativa(email, False)
        # Mensagem igual para e-mail inexistente e senha errada: dizer qual dos dois
        # falhou entrega a lista de quem tem conta aqui.
        flash("E-mail ou senha inválidos.")
        return redirect(url_for("login"))
    return render_template("login.html", google_login_enabled=oauth is not None)


# Força bruta: sem isso, quem souber o e-mail tenta senha pra sempre. São os
# números que travam um ataque sem atrapalhar quem só errou a senha duas vezes.
LOGIN_MAX_TENTATIVAS = 5          # por e-mail
LOGIN_MAX_POR_IP = 20             # por origem (protege a lista toda de usuários)
LOGIN_JANELA_MIN = 15


def _ip_do_pedido() -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def registrar_tentativa(email: str, sucesso: bool) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO tentativas_login (email, ip, quando, sucesso) VALUES (?, ?, ?, ?)",
            (email[:120], _ip_do_pedido()[:45], agora_br().isoformat(timespec="seconds"),
             1 if sucesso else 0),
        )
        # Não deixa a tabela crescer pra sempre — só as últimas 24h interessam
        db.execute("DELETE FROM tentativas_login WHERE quando < ?",
                   ((agora_br() - timedelta(hours=24)).isoformat(timespec="seconds"),))


def login_bloqueado(email: str) -> int:
    """Minutos que faltam pra poder tentar de novo (0 = liberado)."""
    desde = (agora_br() - timedelta(minutes=LOGIN_JANELA_MIN)).isoformat(timespec="seconds")
    with get_db() as db:
        por_email = db.execute(
            """SELECT COUNT(*) AS n, MAX(quando) AS ultima FROM tentativas_login
                WHERE email = ? AND sucesso = 0 AND quando >= ?""",
            (email[:120], desde),
        ).fetchone()
        por_ip = db.execute(
            """SELECT COUNT(*) AS n, MAX(quando) AS ultima FROM tentativas_login
                WHERE ip = ? AND sucesso = 0 AND quando >= ?""",
            (_ip_do_pedido()[:45], desde),
        ).fetchone()

    for linha, teto in ((por_email, LOGIN_MAX_TENTATIVAS), (por_ip, LOGIN_MAX_POR_IP)):
        if linha["n"] >= teto and linha["ultima"]:
            try:
                fim = datetime.fromisoformat(linha["ultima"]) + timedelta(minutes=LOGIN_JANELA_MIN)
            except ValueError:
                continue
            faltam = (fim - agora_br()).total_seconds() / 60
            if faltam > 0:
                return max(1, int(faltam) + 1)
    return 0


def _start_session(user) -> None:
    # Sessão nova a cada login: nada do visitante anterior sobrevive
    session.clear()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    session["user_id"] = user["id"]
    session["nome"] = user["nome"]
    session["perfil"] = user["perfil"]
    session["meta_mensal"] = user["meta_mensal"]
    session["view_mode"] = (user["view_mode"] if "view_mode" in user.keys() else "completo") or "completo"
    _marcar_desbloqueado()  # acabou de provar quem é (senha/Google): não pede a digital agora


@app.route("/login/google")
def google_login():
    if oauth is None:
        flash("Login com Google não está configurado neste servidor.")
        return redirect(url_for("login"))
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

    stats = calc_transaction_totals(user["id"])
    goal = stats["current_goal"]
    note_pending = len([n for n in stats["notes"] if (n["status"] or "").lower() != "autorizada"])
    all_clear = (
        len(stats["overdue_commitments"]) == 0
        and len(stats["due_soon_commitments"]) == 0
        and note_pending == 0
        and stats["balance"] >= 0
    )
    status_emoji = "🟢" if all_clear else "🟡"
    can_spend_today = stats["available_today"]

    # Pergunta inteligente: gastos repetidos que o Hércules ainda não entende
    suggestions = pending_suggestions(user["id"])

    # Modo simples: quanto saiu hoje e a projeção do fim do mês
    today_iso = hoje_br().isoformat()
    with get_db() as db:
        today_spent = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS total FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND fonte != 'ajuste'
                 AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) = date(?)""",
            (user["id"], today_iso),
        ).fetchone()["total"]
        tx_count = db.execute(
            "SELECT COUNT(*) AS n FROM transacoes WHERE user_id = ?", (user["id"],)
        ).fetchone()["n"]

    onboarding = tx_count == 0

    # O fechamento da semana — é o que dá motivo pra voltar. Fica acima de tudo
    # enquanto é notícia, e some pro resto da semana depois do "Vi".
    recado = None
    if not onboarding:
        recado = recado_da_semana(user["id"])
        if recado and tip_seen(user["id"], recado["chave"]):
            recado = None

    # Uma dica do Herc por vez, no momento em que a coisa acontece (ensina usando).
    # Ordem = mais relevante primeiro; some pra sempre depois do "Entendi!".
    herc_tip = None
    if not onboarding:
        with get_db() as db:
            f = db.execute(
                """SELECT
                     MAX(CASE WHEN tipo='saida' AND COALESCE(NULLIF(categoria,''),'Outros')='Outros'
                              THEN 1 ELSE 0 END) AS tem_outros,
                     MAX(CASE WHEN no_credito=1 THEN 1 ELSE 0 END) AS tem_credito,
                     MAX(CASE WHEN parcela_total > 1 THEN 1 ELSE 0 END) AS tem_parcela,
                     MAX(CASE WHEN fonte='ofx' THEN 1 ELSE 0 END) AS tem_sync
                   FROM transacoes WHERE user_id = ?""",
                (user["id"],),
            ).fetchone()
        # Trilha do MEI: o faturamento vem das NOTAS, então avisa quem ainda não guardou nenhuma
        mei_nota = mei_sem_nota = mei_limite = 0
        if is_business_profile(user_profile(user)):
            with get_db() as db:
                n_notas = db.execute(
                    "SELECT COUNT(*) AS n FROM notas WHERE user_id = ?", (user["id"],)
                ).fetchone()["n"]
            mei_nota = 1 if n_notas else 0
            mei_sem_nota = 0 if n_notas else 1
            faturado = calc_mei_faturamento(user["id"], hoje_br().year)
            mei_limite = 1 if faturado >= MEI_LIMITE_ANUAL * 0.8 else 0

        candidatas = [
            ("salario_na_virada", 1 if salario_perto_da_virada(user["id"]) else 0),
            ("mei_limite", mei_limite),
            ("primeira_parcela", f["tem_parcela"]),
            ("primeiro_credito", f["tem_credito"]),
            ("tem_outros", f["tem_outros"]),
            ("mei_primeira_nota", mei_nota),
            ("mei_sem_nota", mei_sem_nota),
            ("primeira_sync", f["tem_sync"]),
            ("registro_rapido", 1),
        ]
        for chave, vale in candidatas:
            if vale and not tip_seen(user["id"], chave):
                herc_tip = chave
                break
    # "Seguindo assim, termina o mês com X" — a frase do modo simples, que é onde
    # mais gente confia. Dividir o gasto do mês pelos dias corridos parecia certo
    # e era um desastre: no dia 2, quem pagou aluguel e guardou dinheiro no dia 1
    # virava alguém gastando R$ 723 por dia. Medido, a frase prometia terminar o
    # mês com R$ -21.021 pra quem ganha R$ 3.000.
    #
    # O jeito certo é o mesmo do simulador: ritmo é o gasto miúdo, e as contas que
    # ainda vencem entram inteiras, por fora — cada coisa contada uma vez só.
    virada_usuario = virada_do_usuario(user["id"])
    avg_daily_spend, _ = media_gasto_diario(user["id"])
    dias_a_viver = max(0, days_left_in_month(virada_usuario) - 1)
    projected_end = (stats["balance"] - contas_ate_fim_do_mes(user["id"])
                     - avg_daily_spend * dias_a_viver)
    view_mode = (user["view_mode"] if "view_mode" in user.keys() else "completo") or "completo"
    # O menu (base.html) lê da sessão e o conteúdo lê do banco. Duas fontes pra mesma
    # verdade dá menu completo com tela simples — realinha aqui, que o banco manda.
    session["view_mode"] = view_mode

    dias_ofx = dias_desde_ultimo_ofx(user)
    lembrar_ofx = (not onboarding) and (dias_ofx is None or dias_ofx >= OFX_LEMBRETE_DIAS)

    # "Sobrou? Guarda": na reta final do mês, se sobrou dinheiro e há uma reserva
    # incompleta, o Herc oferece jogar a sobra na reserva num toque.
    sobra_guardar = None
    if hoje_br().day >= 20:
        with get_db() as db:
            reserva_goal = db.execute(
                """SELECT meta_valor, valor_atual FROM metas
                   WHERE user_id = ? AND ativo = 1 AND LOWER(nome) LIKE '%reserva%'
                   ORDER BY created_at DESC LIMIT 1""",
                (user["id"],),
            ).fetchone()
        if reserva_goal:
            faltante = max(0.0, float(reserva_goal["meta_valor"]) - float(reserva_goal["valor_atual"]))
            sobra_mes = max(0.0, float(stats["month_income"]) - float(stats["month_expenses"]))
            if sobra_mes > 0 and faltante > 0:
                sobra_guardar = min(sobra_mes, faltante)

    session["meta_mensal"] = user["meta_mensal"]
    return render_template(
        "home.html",
        duplicatas=possiveis_duplicatas(user["id"]),
        recado=recado,
        suggestions=suggestions,
        suggestion_categories=expense_category_names(user["id"]),
        today_spent=float(today_spent or 0),
        projected_end=float(projected_end),
        view_mode=view_mode,
        onboarding=onboarding,
        herc_tip=herc_tip,
        herc_tip_text=HERC_TIPS.get(herc_tip),
        lembrar_ofx=lembrar_ofx,
        dias_ofx=dias_ofx,
        user=user,
        stats=stats,
        status_emoji=status_emoji,
        all_clear=all_clear,
        can_spend_today=can_spend_today,
        goal=goal,
        pluggy_ativo=pluggy_configured(),
        pluggy_tem_banco=bool(pluggy_user_item_ids(user)),
        sobra_guardar=sobra_guardar,
        dividas_resumo=calc_dividas(user["id"]),
        parcelas=calc_parcelas_futuras(user["id"]),
    )


def _valor_redondo(v: float) -> int:
    """Número fácil de lembrar: 87 vira 85, 137 vira 130, 640 vira 600."""
    if v < 50:
        return max(10, int(v // 5) * 5)
    if v < 200:
        return int(v // 10) * 10
    return int(v // 50) * 50


def media_gasto_diario(user_id: int, dias: int = 60) -> tuple[float, int]:
    """O ritmo do DIA A DIA — o que a pessoa gasta sem pensar, por dia.

    Três coisas ficam de fora, e cada uma por um motivo:

    * **Reserva** — guardar dinheiro numa meta vira uma saída no histórico, mas
      não é consumo. Contar isso faria o app dizer que quem poupa gasta mais.
    * **Crédito** — a compra no cartão sai da conta quando a fatura é paga, e o
      pagamento já aparece como saída. Contar os dois é contar duas vezes.
    * **Dias fora da curva** — aluguel, fatura, uma compra grande. Isso não é
      ritmo, é evento; e o que está registrado como conta a pagar já entra
      separado na simulação. Sem tirar, o aluguel do mês passado é contado de
      novo, diluído em todos os dias.

    Medido com dois meses de dados realistas: sem esses cortes, R$ 27/dia de
    gasto miúdo viravam R$ 92/dia.
    """
    desde = hoje_br() - timedelta(days=dias)
    with get_db() as db:
        linhas = db.execute(
            """SELECT date(COALESCE(NULLIF(data_transacao, ''), created_at)) AS dia,
                      SUM(valor) AS total
                 FROM transacoes
                WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0
                  AND fonte != 'ajuste' AND interno = 0
                  AND COALESCE(NULLIF(categoria, ''), '') != 'Reserva'
                  AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date(?)
                GROUP BY dia ORDER BY dia""",
            (user_id, desde.isoformat()),
        ).fetchall()
    if not linhas:
        return 0.0, 0

    com_gasto = len(linhas)
    try:
        primeiro = date.fromisoformat(linhas[0]["dia"])
    except (TypeError, ValueError):
        primeiro = desde
    # Período REAL de histórico, não a janela inteira: quem usa há 20 dias
    # dividido por 60 pareceria gastar um terço do que gasta.
    corridos = max(1, (hoje_br() - max(primeiro, desde)).days + 1)

    totais = sorted(float(r["total"] or 0) for r in linhas)
    if com_gasto >= 4:
        # Fora da curva é o dia que destoa DA PESSOA, não uma fatia fixa. Cortar
        # sempre os 10% maiores puniria quem gasta igual todo dia; comparar com a
        # mediana só corta quem realmente destoa — o dia do aluguel, o da fatura.
        mediana = totais[com_gasto // 2]
        limite = max(3 * mediana, mediana + 100.0)
        # Trava de segurança: se muita coisa passar do limite, não é evento, é o
        # jeito da pessoa gastar. Aí o corte pararia de medir e passaria a mentir.
        tipicos = [t for t in totais if t <= limite]
        if len(tipicos) < com_gasto * 0.8:
            tipicos = totais
    else:
        tipicos = totais
    return sum(tipicos) / corridos, com_gasto


def detalhe_do_ritmo(user_id: int, dias: int = 60) -> dict[str, Any]:
    """De onde sai o "seu dia a dia" — dia por dia, pra pessoa poder conferir.

    Existe porque um número sozinho não se defende. O Matheus olhou R$ 129 por
    dia e disse "que isso"; eu chutei três explicações sem ver os dados dele e
    errei. Com a lista na tela, ele não precisa de mim pra saber o que é.
    """
    desde = hoje_br() - timedelta(days=dias)
    col = "date(COALESCE(NULLIF(data_transacao, ''), created_at))"
    with get_db() as db:
        linhas = db.execute(
            f"""SELECT {col} AS dia, SUM(valor) AS total, COUNT(*) AS n
                  FROM transacoes
                 WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0
                   AND fonte != 'ajuste' AND interno = 0
                   AND COALESCE(NULLIF(categoria, ''), '') != 'Reserva'
                   AND {col} >= date(?)
                 GROUP BY dia ORDER BY total DESC""",
            (user_id, desde.isoformat()),
        ).fetchall()
        guardado = float(db.execute(
            f"""SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                 WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND interno = 0
                   AND fonte != 'ajuste' AND categoria = 'Reserva' AND {col} >= date(?)""",
            (user_id, desde.isoformat()),
        ).fetchone()["t"])
        no_credito = float(db.execute(
            f"""SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                 WHERE user_id = ? AND tipo = 'saida' AND no_credito = 1
                   AND fonte != 'ajuste' AND {col} >= date(?)""",
            (user_id, desde.isoformat()),
        ).fetchone()["t"])

    media, _ = media_gasto_diario(user_id, dias)
    if not linhas:
        return {"media": 0.0, "maiores": [], "guardado": guardado,
                "no_credito": no_credito, "limite": None, "cortou": False,
                "dias_com_gasto": 0}

    totais = sorted(float(r["total"] or 0) for r in linhas)
    com_gasto = len(totais)
    limite = None
    if com_gasto >= 4:
        mediana = totais[com_gasto // 2]
        limite = max(3 * mediana, mediana + 100.0)
        if len([t for t in totais if t <= limite]) < com_gasto * 0.8:
            limite = None          # a trava desligou o corte: nada foi tirado

    maiores = []
    for r in linhas[:6]:
        total = float(r["total"] or 0)
        maiores.append({"dia": r["dia"], "total": total, "itens": r["n"],
                        "contou": limite is None or total <= limite})
    return {"media": media, "maiores": maiores, "guardado": guardado,
            "no_credito": no_credito, "limite": limite,
            "cortou": any(not m["contou"] for m in maiores),
            "dias_com_gasto": com_gasto}


def contas_ate_fim_do_mes(user_id: int) -> float:
    """O que ainda vence neste mês e não foi pago."""
    _, fim = mes_do_usuario(user_id)
    with get_db() as db:
        return float(db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM compromissos
                WHERE user_id = ? AND status = 'pendente' AND tipo = 'saida'
                  AND date(vencimento) <= date(?)""",
            (user_id, fim.isoformat()),
        ).fetchone()["t"])


def simular_gasto(user_id: int, valor: float) -> dict[str, Any]:
    """'Se eu gastar isso hoje, eu atravesso o mês?'

    A resposta útil não é o saldo depois — é o veredito. Calculadora qualquer um
    tem; o que o Hércules sabe e a calculadora não são as contas que ainda vencem
    e o ritmo de gasto da própria pessoa."""
    stats = calc_transaction_totals(user_id)
    saldo = float(stats["balance"])
    contas = contas_ate_fim_do_mes(user_id)
    media, dias_com_dado = media_gasto_diario(user_id)
    dias = days_left_in_month(virada_do_usuario(user_id))

    # O gasto de hoje já entrou na média; contar o dia de hoje de novo puniria
    # duas vezes. Por isso dias - 1.
    dias_futuros = max(0, dias - 1)
    reservado = contas + media * dias_futuros
    sobra = saldo - valor - reservado
    folga_minima = max(media * 3, 50.0)      # três dias de respiro, ou R$ 50

    # Se nem sem a compra o mes fecha, o problema nao e' a compra. Dizer "nao cabe"
    # pra R$ 150 seria verdade sem ser util — e a pessoa precisa saber a real.
    ja_no_vermelho = saldo - reservado < 0
    if ja_no_vermelho:
        veredito = "ja_apertado"
    elif sobra < 0:
        veredito = "nao_cabe"
    elif sobra < folga_minima:
        veredito = "aperta"
    else:
        veredito = "cabe"

    teto = max(0.0, saldo - reservado)                 # gastar isso zera o mês
    teto_folgado = max(0.0, teto - folga_minima)       # gastar isso ainda deixa respiro

    return {
        "valor": valor, "saldo": saldo, "contas": contas, "media": media,
        "dias": dias, "dias_futuros": dias_futuros, "reservado": reservado,
        "sobra": sobra, "veredito": veredito,
        "teto": teto, "teto_folgado": teto_folgado,
        "ja_no_vermelho": ja_no_vermelho,
        "falta_sem_a_compra": max(0.0, reservado - saldo),
        "tem_historico": dias_com_dado >= 5,
        "dias_com_dado": dias_com_dado,
        "saldo_depois": saldo - valor,
        # A pessoa tem que conseguir conferir o ritmo sem depender de ninguém.
        "ritmo": detalhe_do_ritmo(user_id),
    }


def sugerir_guardar_mensal(user_id: int, meses: int = 6) -> dict[str, Any] | None:
    """Quanto a pessoa CONSEGUE guardar sem precisar mexer depois.

    Usa o PIOR mês (não a média) de propósito: meta que se cumpre constrói
    confiança; meta que se quebra confirma o "eu não consigo guardar". Melhor
    guardar pouco e manter do que guardar muito e sacar na metade do mês.
    """
    hoje = hoje_br()
    desde = (hoje - timedelta(days=31 * (meses + 1))).isoformat()
    with get_db() as db:
        rows = db.execute(
            f"""SELECT {sql_mes(virada_do_usuario(user_id), "COALESCE(NULLIF(data_transacao, ''), created_at)")} AS mes,
                      SUM(CASE WHEN tipo = 'entrada' AND no_credito = 0 THEN valor ELSE 0 END) AS entrou,
                      SUM(CASE WHEN tipo = 'saida' AND no_credito = 0 THEN valor ELSE 0 END) AS saiu
               FROM transacoes
               WHERE user_id = ? AND fonte != 'ajuste' AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date(?)
               GROUP BY mes ORDER BY mes DESC""",
            (user_id, desde),
        ).fetchall()

    mes_atual = hoje.strftime("%Y-%m")  # mês corrente ainda não fechou: não conta
    fechados = [r for r in rows if r["mes"] != mes_atual and float(r["entrou"] or 0) > 0]
    if len(fechados) < 2:
        return None  # sem histórico suficiente pra prometer qualquer coisa

    sobras = [float(r["entrou"] or 0) - float(r["saiu"] or 0) for r in fechados]
    positivas = [s for s in sobras if s > 0]

    if not positivas:
        # Nenhum mês sobrou: começar simbólico, só pra criar o hábito
        return {"valor": 20, "meses": len(fechados), "apertado": True,
                "pior": min(sobras), "melhor": max(sobras)}

    # 80% do pior mês que sobrou: cabe até nos meses ruins
    valor = _valor_redondo(min(positivas) * 0.8)
    return {"valor": max(10, valor), "meses": len(fechados), "apertado": False,
            "pior": min(sobras), "melhor": max(sobras)}


def detectar_assinaturas(user_id: int, meses: int = 5) -> dict[str, Any]:
    """Acha o que se repete todo mês (Netflix, academia, seguro...). Critério: mesmo
    lugar, valor parecido, em 3+ meses diferentes. É o gasto que passa despercebido
    justamente por ser silencioso."""
    desde = (hoje_br() - timedelta(days=31 * meses)).isoformat()
    with get_db() as db:
        rows = db.execute(
            """SELECT descricao, estabelecimento, valor, categoria,
                      strftime('%Y-%m', COALESCE(NULLIF(data_transacao, ''), created_at)) AS mes
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste'
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date(?)""",
            (user_id, desde),
        ).fetchall()

    grupos: dict[str, dict] = {}
    for r in rows:
        nome = r["estabelecimento"] or r["descricao"] or ""
        chave = _normalizar_regra(padrao_sugerido(nome))
        if not chave:
            continue
        g = grupos.setdefault(chave, {"nome": padrao_sugerido(nome), "valores": [],
                                      "meses": set(), "categoria": r["categoria"]})
        g["valores"].append(float(r["valor"]))
        g["meses"].add(r["mes"])

    achadas = []
    for g in grupos.values():
        if len(g["meses"]) < 3:
            continue
        vmin, vmax = min(g["valores"]), max(g["valores"])
        if vmin <= 0 or vmax / vmin > 1.25:   # valor tem que ser estável (reajuste pequeno ok)
            continue
        tipico = sorted(g["valores"])[len(g["valores"]) // 2]
        achadas.append({"nome": g["nome"], "valor": tipico, "meses": len(g["meses"]),
                        "categoria": g["categoria"] or "Outros"})
    achadas.sort(key=lambda a: a["valor"], reverse=True)
    total = sum(a["valor"] for a in achadas)
    return {"itens": achadas, "total_mes": total, "total_ano": total * 12, "tem": bool(achadas)}


def insight_semanal(user_id: int) -> dict[str, Any] | None:
    """Insight concreto da semana, comparando com a semana anterior do PRÓPRIO usuário
    (não com média de mercado, que não diz nada pra quem está começando)."""
    hoje = hoje_br()
    ini_atual, fim_atual = (hoje - timedelta(days=6)).isoformat(), hoje.isoformat()
    ini_ant, fim_ant = (hoje - timedelta(days=13)).isoformat(), (hoje - timedelta(days=7)).isoformat()
    with get_db() as db:
        def gasto(ini, fim):
            return float(db.execute(
                """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                   WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste'
                     AND no_credito = 0 AND interno = 0
                     AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
                (user_id, ini, fim),
            ).fetchone()["t"])
        atual = gasto(ini_atual, fim_atual)
        anterior = gasto(ini_ant, fim_ant)
        # O que foi no cartão fica SEPARADO, não somado. Juntar os dois inflava a
        # semana do pagamento — que é justamente quando mais se usa cartão — e
        # depois contava de novo quando a fatura era paga. O Matheus viu R$ 1.700
        # numa semana e sabia que não era dele.
        credito = float(db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
                 WHERE user_id = ? AND tipo = 'saida' AND no_credito = 1
                   AND fonte != 'ajuste'
                   AND date(COALESCE(NULLIF(data_transacao, ''), created_at))
                       BETWEEN date(?) AND date(?)""",
            (user_id, ini_atual, fim_atual),
        ).fetchone()["t"])
        # Esta some com o crédito porque mora dentro do card "saiu da conta". Nas
        # telas de "pra onde o dinheiro foi" (Meses, recado) o cartão continua
        # contando — lá a pergunta é outra.
        top = db.execute(
            """SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS cat, SUM(valor) AS t
               FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND no_credito = 0
                 AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)
               GROUP BY cat ORDER BY t DESC LIMIT 1""",
            (user_id, ini_atual, fim_atual),
        ).fetchone()
    if atual <= 0 and credito <= 0:
        return None
    return {
        "atual": atual,
        "credito": credito,
        "anterior": anterior,
        "delta": atual - anterior,
        "tem_comparacao": anterior > 0,
        "media_dia": atual / 7,
        "top_cat": top["cat"] if top else None,
        "top_valor": float(top["t"] or 0) if top else 0.0,
    }


def texto_recado(recado: dict[str, Any], nome: str = "") -> str:
    """O fechamento da semana em texto puro, pronto pra colar no WhatsApp.

    Sem HTML e sem link: mensagem que chega com cara de recado de gente, não de
    notificação de sistema. O negrito usa *asterisco*, que é o do WhatsApp."""
    ini, fim = recado["inicio"].strftime("%d/%m"), recado["fim"].strftime("%d/%m")
    primeiro = (nome or "").strip().split(" ")[0]
    linhas = [f"🦁 *Seu recado da semana* ({ini} a {fim})", ""]

    abre = f"{primeiro}, você" if primeiro else "Você"
    if recado["primeira_semana"]:
        linhas.append(f"{abre} gastou *{money(recado['gasto'])}*. Foi sua primeira semana "
                      "registrada — na próxima eu já comparo com essa.")
    elif recado["delta"] is not None and recado["delta"] < -1:
        linhas.append(f"{abre} gastou *{money(recado['gasto'])}*, "
                      f"*{money(-recado['delta'])} a menos* que na semana anterior. 👏")
    elif recado["delta"] is not None and recado["delta"] > 1:
        linhas.append(f"{abre} gastou *{money(recado['gasto'])}*, "
                      f"*{money(recado['delta'])} a mais* que na semana anterior.")
    else:
        linhas.append(f"{abre} gastou *{money(recado['gasto'])}* — praticamente o mesmo "
                      "da semana anterior.")

    if recado["mudanca"]:
        m = recado["mudanca"]
        verbo = "subiu" if m["delta"] > 0 else "caiu"
        linhas += ["", f"O que mais mudou: *{m['categoria']}* {verbo} "
                       f"{money(abs(m['delta']))}, fechando em {money(m['total'])}."]

    if recado["sobrou"] > 0:
        linhas += ["", f"💚 Sobrou *{money(recado['sobrou'])}* na semana. Guardar agora, "
                       "enquanto está na conta, é bem mais fácil do que no fim do mês."]

    return "\n".join(linhas)


MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def nome_do_mes(chave: str) -> str:
    """'2026-07' -> 'julho de 2026'."""
    try:
        ano, mes = chave.split("-")
        return f"{MESES_PT[int(mes) - 1]} de {ano}"
    except (ValueError, IndexError):
        return chave


def historico_mensal(user_id: int, meses: int = 12) -> list[dict[str, Any]]:
    """Entrou, saiu e sobrou de cada mês — o histórico que dá pra comparar.

    Crédito fica de fora do 'saiu' pelo mesmo motivo do resto do app: a compra
    parcelada não sai da conta no mês em que foi feita."""
    hoje = hoje_br()
    virada = virada_do_usuario(user_id)
    with get_db() as db:
        rows = db.execute(
            f"""SELECT {sql_mes(virada, "COALESCE(NULLIF(data_transacao, ''), created_at)")} AS mes,
                      COALESCE(SUM(CASE WHEN tipo='entrada' AND no_credito=0 THEN valor END), 0) AS entrou,
                      COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=0 THEN valor END), 0) AS saiu,
                      COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=1 THEN valor END), 0) AS credito,
                      COUNT(*) AS n
                 FROM transacoes
                WHERE user_id = ? AND fonte != 'ajuste' AND interno = 0
                GROUP BY mes ORDER BY mes DESC LIMIT ?""",
            (user_id, meses),
        ).fetchall()

    # Qual mês é "agora" também depende da virada: no dia 31, com virada 31,
    # o mês em curso já é o seguinte.
    atual = month_bounds(hoje, virada)[1].strftime("%Y-%m")
    saida = []
    for r in rows:
        entrou, saiu = float(r["entrou"]), float(r["saiu"])
        saida.append({
            "mes": r["mes"], "nome": nome_do_mes(r["mes"]),
            "entrou": entrou, "saiu": saiu, "credito": float(r["credito"]),
            "sobrou": entrou - saiu, "movimentos": r["n"],
            # O mês corrente ainda não acabou: comparar ele com meses inteiros
            # engana, então a tela avisa em vez de fingir que é comparável.
            "em_curso": r["mes"] == atual,
        })
    return saida


def movimentos_do_mes(user_id: int, mes: str, tipo: str, limite: int = 60) -> list[dict[str, Any]]:
    """As linhas que formam o "entrou" ou o "saiu" daquele mês, maior primeiro.

    Existe porque um total sozinho não se defende. O Matheus viu "entrou
    R$ 6.045" num mês em que recebeu R$ 2.660 e nem ele nem eu conseguíamos
    dizer de onde vinha a diferença — eu chutei três causas sem ver os dados.
    Com a lista aberta, a resposta para de depender de palpite.

    Mesma régua do total: crédito e ajuste ficam de fora, porque a pergunta aqui
    é o que entrou e saiu DA CONTA.
    """
    virada = virada_do_usuario(user_id)
    col = "COALESCE(NULLIF(data_transacao, ''), created_at)"
    with get_db() as db:
        linhas = db.execute(
            f"""SELECT valor, descricao, estabelecimento, categoria, fonte, fitid,
                       date({col}) AS dia
                  FROM transacoes
                 WHERE user_id = ? AND tipo = ? AND no_credito = 0 AND fonte != 'ajuste'
                   AND interno = 0
                   AND {sql_mes(virada, col)} = ?
                 ORDER BY valor DESC LIMIT ?""",
            (user_id, tipo, mes, limite),
        ).fetchall()
    return [dict(r) for r in linhas]


def comparar_categorias(user_id: int, mes: str, mes_anterior: str) -> list[dict[str, Any]]:
    """Por categoria, um mês contra o outro — ordenado pela maior mudança."""
    virada = virada_do_usuario(user_id)
    with get_db() as db:
        def gastos(m):
            return {r["cat"]: float(r["t"] or 0) for r in db.execute(
                f"""SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS cat, SUM(valor) AS t
                     FROM transacoes
                    WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                      AND {sql_mes(virada, "COALESCE(NULLIF(data_transacao, ''), created_at)")} = ?
                    GROUP BY cat""", (user_id, m)).fetchall()}
        agora, antes = gastos(mes), gastos(mes_anterior)

    linhas = [{"categoria": c, "agora": agora.get(c, 0.0), "antes": antes.get(c, 0.0),
               "delta": agora.get(c, 0.0) - antes.get(c, 0.0)}
              for c in set(agora) | set(antes)]
    linhas.sort(key=lambda l: abs(l["delta"]), reverse=True)
    return linhas


def mes_anterior_a(chave: str) -> str:
    ano, mes = (int(x) for x in chave.split("-"))
    return f"{ano - 1}-12" if mes == 1 else f"{ano}-{mes - 1:02d}"


def semana_fechada(hoje: date | None = None) -> tuple[date, date]:
    """A última semana que REALMENTE acabou (segunda a domingo).

    A semana corrente não serve: fechar o balanço na quarta-feira compara meia
    semana com uma inteira e sempre parece que a pessoa gastou menos."""
    hoje = hoje or hoje_br()
    domingo = hoje - timedelta(days=hoje.weekday() + 1)
    return domingo - timedelta(days=6), domingo


def chave_recado(fim: date) -> str:
    ano, semana, _ = fim.isocalendar()
    return f"recado:{ano}-W{semana:02d}"


# Mudança menor que isso é ruído: apontar "gastou R$ 4 a mais em padaria"
# ensina a pessoa a ignorar o recado.
RECADO_MUDANCA_MINIMA = 25.0


def recado_da_semana(user_id: int, hoje: date | None = None) -> dict[str, Any] | None:
    """O fechamento da semana: o que aconteceu, o que mudou e o que dá pra fazer.

    É o motivo de voltar. Por isso tem que trazer notícia — não repetir o saldo,
    que a pessoa já vê na tela toda vez que abre."""
    hoje = hoje or hoje_br()
    ini, fim = semana_fechada(hoje)
    ini_ant, fim_ant = ini - timedelta(days=7), fim - timedelta(days=7)

    with get_db() as db:
        def por_categoria(a, b):
            return {r["cat"]: float(r["t"] or 0) for r in db.execute(
                """SELECT COALESCE(NULLIF(categoria, ''), 'Outros') AS cat, SUM(valor) AS t
                     FROM transacoes
                    WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                      AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)
                    GROUP BY cat""", (user_id, a.isoformat(), b.isoformat())).fetchall()}

        def totais(a, b):
            r = db.execute(
                """SELECT COALESCE(SUM(CASE WHEN tipo='entrada' AND no_credito=0 THEN valor END), 0) AS entrou,
                          COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=0 THEN valor END), 0) AS saiu,
                          COUNT(*) AS n
                     FROM transacoes
                    WHERE user_id = ? AND fonte != 'ajuste' AND interno = 0
                      AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
                (user_id, a.isoformat(), b.isoformat())).fetchone()
            return float(r["entrou"]), float(r["saiu"]), r["n"]

        entrou, saiu, movimentos = totais(ini, fim)
        _, saiu_ant, movimentos_ant = totais(ini_ant, fim_ant)

    # Semana sem nada não vira recado — mandar "você gastou R$ 0" é ruído
    if movimentos == 0:
        return None

    cats, cats_ant = por_categoria(ini, fim), por_categoria(ini_ant, fim_ant)
    mudanca = None
    if movimentos_ant:
        deltas = {c: cats.get(c, 0) - cats_ant.get(c, 0) for c in set(cats) | set(cats_ant)}
        cat, valor = max(deltas.items(), key=lambda kv: abs(kv[1]), default=(None, 0))
        if cat and abs(valor) >= RECADO_MUDANCA_MINIMA:
            mudanca = {"categoria": cat, "delta": valor, "total": cats.get(cat, 0)}

    return {
        "inicio": ini, "fim": fim,
        "chave": chave_recado(fim),
        "gasto": saiu, "entrou": entrou,
        "gasto_anterior": saiu_ant,
        "delta": saiu - saiu_ant if movimentos_ant else None,
        "sobrou": max(0.0, entrou - saiu),
        "movimentos": movimentos,
        "mudanca": mudanca,
        "primeira_semana": movimentos_ant == 0,
    }


# Prévia do IR — valores de REFERÊNCIA (mudam todo ano; confirmar no ano vigente).
IR_LIMITE_DECLARACAO = 30000.0   # renda tributável anual a partir da qual costuma ser obrigatório declarar
IR_LIMITE_EDUCACAO = 3561.50     # dedução de educação por pessoa/ano
IR_ALIQUOTA_TETO = 0.275         # maior alíquota da tabela (pra estimar o TETO de economia)
IR_RENDA_CATS = ("Salário", "Freelance / bico", "Vendas", "Rendimentos")


def calc_ir_preview(user_id: int, year: int) -> dict[str, Any]:
    """Estimativa (não é cálculo oficial): renda tributável do ano + deduções de
    saúde/educação já lançadas, e o teto de economia que elas podem gerar."""
    ini, fim = f"{year}-01-01", f"{year}-12-31"
    marks = ",".join(["?"] * len(IR_RENDA_CATS))
    with get_db() as db:
        renda = db.execute(
            f"""SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
               WHERE user_id = ? AND tipo = 'entrada' AND fonte != 'ajuste' AND interno = 0
                 AND categoria IN ({marks})
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, *IR_RENDA_CATS, ini, fim),
        ).fetchone()["t"]
        # Dedução de IR NÃO exclui crédito, e isso é de propósito: consulta paga no
        # cartão é dedutível igual. Aqui a pergunta não é "saiu da conta", é "foi
        # gasto no ano". O pagamento da fatura não cai em Saúde, então não conta
        # duas vezes.
        saude = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                 AND categoria = 'Saúde'
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, ini, fim),
        ).fetchone()["t"]
        educ = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND fonte != 'ajuste' AND interno = 0
                 AND categoria = 'Educação'
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user_id, ini, fim),
        ).fetchone()["t"]
    renda = float(renda or 0)
    saude = float(saude or 0)
    educacao = float(educ or 0)
    educacao_dedutivel = min(educacao, IR_LIMITE_EDUCACAO)
    deducao_total = saude + educacao_dedutivel
    return {
        "year": year,
        "renda": renda,
        "saude": saude,
        "educacao": educacao,
        "educacao_dedutivel": educacao_dedutivel,
        "educacao_limite": IR_LIMITE_EDUCACAO,
        "deducao_total": deducao_total,
        "economia_teto": deducao_total * IR_ALIQUOTA_TETO,
        "precisa_declarar": renda >= IR_LIMITE_DECLARACAO,
        "limite_declaracao": IR_LIMITE_DECLARACAO,
        "tem_dados": renda > 0 or deducao_total > 0,
    }


@app.route("/ir")
@login_required
def previa_ir():
    user = current_user()
    ano = hoje_br().year
    ir = calc_ir_preview(user["id"], ano)
    # MEI fatura por NOTA, não por transação — sem isso a tela diria "sua renda é R$ 0"
    # pra quem faturou o ano inteiro. Melhor mostrar separado e mandar pro Painel MEI.
    ehmei = is_business_profile(user_profile(user))
    ir["faturamento_mei"] = calc_mei_faturamento(user["id"], ano) if ehmei else 0.0
    return render_template("ir.html", user=user, ir=ir, ehmei=ehmei)


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    profile = user_profile(user)
    stats = calc_transaction_totals(user["id"])
    labels = [row["categoria"] for row in stats["monthly_by_category"]]
    values = [row["total"] or 0 for row in stats["monthly_by_category"]]

    # Retrato do mês: a narrativa curta que abre o Resumo
    by_cat = stats["monthly_by_category"]
    maior_cat = max(by_cat, key=lambda r: r["total"] or 0) if by_cat else None
    prev_start, prev_end = _prev_month_bounds(virada_do_usuario(user["id"]))
    with get_db() as db:
        prev_expenses = db.execute(
            """SELECT COALESCE(SUM(valor), 0) AS t FROM transacoes
               WHERE user_id = ? AND tipo = 'saida' AND no_credito = 0 AND fonte != 'ajuste'
                 AND interno = 0
                 AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) BETWEEN date(?) AND date(?)""",
            (user["id"], prev_start, prev_end),
        ).fetchone()["t"]
    exp = float(stats["month_expenses"])
    # Quem recebe no fim do mês vê "entradas: R$ 0" no dia 1º e acha que sumiu.
    # Buscamos a última entrada pra poder avisar que ela caiu no mês passado.
    ultima_entrada = None
    if float(stats["month_income"]) <= 0:
        with get_db() as db:
            r = db.execute(
                """SELECT valor, COALESCE(NULLIF(data_transacao, ''), created_at) AS quando
                   FROM transacoes
                   WHERE user_id = ? AND tipo = 'entrada' AND no_credito = 0 AND fonte != 'ajuste'
                     AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date(?)
                   ORDER BY date(quando) DESC LIMIT 1""",
                (user["id"], (hoje_br() - timedelta(days=10)).isoformat()),
            ).fetchone()
            if r:
                ultima_entrada = {"valor": float(r["valor"]), "quando": str(r["quando"])[:10]}
    retrato = {
        "ultima_entrada": ultima_entrada,
        "tem_dados": exp > 0 or float(stats["month_income"]) > 0,
        "income": float(stats["month_income"]),
        "expenses": exp,
        "saldo_mes": float(stats["month_income"]) - exp,
        "maior_cat": maior_cat,
        "maior_cat_pct": (float(maior_cat["total"] or 0) / exp * 100) if (maior_cat and exp > 0) else 0,
        "prev_expenses": float(prev_expenses or 0),
        "delta": exp - float(prev_expenses or 0),
        "fatura": float(stats["fatura_credito_mes"]),
    }
    return render_template(
        "dashboard.html",
        user=user,
        profile=profile,
        stats=stats,
        labels=labels,
        values=values,
        retrato=retrato,
        chart_colors=CHART_COLORS,
        parcelas=calc_parcelas_futuras(user["id"]),
        insight=insight_semanal(user["id"]),
        assinaturas=detectar_assinaturas(user["id"]),
        month=month_label(hoje_br().strftime("%Y-%m")),
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
        month=month_label(hoje_br().strftime("%Y-%m")),
    )


@app.route("/mei")
@login_required
def painel_mei():
    user = current_user()
    profile = user_profile(user)
    if not is_business_profile(profile):
        flash("O Painel MEI é para quem tem perfil MEI, lojista ou híbrido.")
        return redirect(url_for("home"))

    today = hoje_br()
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
    if valor <= 0 or valor_absurdo(valor):
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
            today = hoje_br()
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
    year = request.args.get("year", str(hoje_br().year))
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
            if email_invalido(email):
                flash("Esse e-mail não parece completo. Confira antes de salvar.")
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
            problema = senha_fraca(nova_senha, user["email"], user["nome"])
            if problema:
                flash(problema)
                return redirect(url_for("settings"))
            if nova_senha != confirmar:
                flash("A confirmação não bate com a nova senha.")
                return redirect(url_for("settings"))
            with get_db() as db:
                db.execute("UPDATE usuarios SET senha = ? WHERE id = ?", (generate_password_hash(nova_senha), user["id"]))
            flash("Senha alterada com sucesso.")
            return redirect(url_for("settings"))

        perfil = normalize_profile(request.form.get("perfil"))
        meta_mensal = parse_money(request.form.get("meta_mensal"))
        cartao_orcamento = parse_money(request.form.get("cartao_orcamento"))

        def _dia_ou_none(campo):
            try:
                d = int(request.form.get(campo) or 0)
            except ValueError:
                return None
            return d if 1 <= d <= 31 else None
        cartao_fechamento = _dia_ou_none("cartao_fechamento")
        cartao_vencimento = _dia_ou_none("cartao_vencimento")
        dia_virada = _dia_ou_none("dia_virada")
        view_mode = request.form.get("view_mode", "completo")
        if view_mode not in {"simples", "completo"}:
            view_mode = "completo"
        with get_db() as db:
            db.execute(
                """UPDATE usuarios SET perfil = ?, meta_mensal = ?, cartao_orcamento = ?,
                   cartao_fechamento = ?, cartao_vencimento = ?, view_mode = ?,
                   dia_virada = ? WHERE id = ?""",
                (perfil, meta_mensal, cartao_orcamento, cartao_fechamento,
                 cartao_vencimento, view_mode, dia_virada, user["id"]),
            )
        session["perfil"] = perfil
        session["meta_mensal"] = meta_mensal
        session["view_mode"] = view_mode
        flash("Preferências atualizadas.")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        user=user,
        pluggy_ativo=pluggy_configured(),
        pluggy_tem_id=bool(PLUGGY_CLIENT_ID),
        pluggy_tem_secret=bool(PLUGGY_CLIENT_SECRET),
        pluggy_tem_banco=bool(pluggy_user_item_ids(user)),
        webauthn_ok=webauthn_disponivel(),
        passkey_ativa=app_tem_bloqueio(user["id"]),
    )


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
        if not nome or meta_valor <= 0 or valor_absurdo(meta_valor):
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
    # Reserva de emergência guiada: 3–6× o custo de vida mensal (gastos + fatura do cartão)
    custo_mensal = float(stats["month_expenses"]) + float(stats["fatura_credito_mes"])
    tem_reserva = any("reserva" in (g["row"]["nome"] or "").lower() for g in goals_view)
    reserva = {
        "mostrar": custo_mensal > 0 and not tem_reserva,
        "custo": custo_mensal,
        "r3": custo_mensal * 3,
        "r6": custo_mensal * 6,
    }
    novo = request.args.get("novo", type=int)
    return render_template("metas.html", user=user, goals=goals_view, stats=stats, novo=novo,
                           reserva=reserva, guardar=sugerir_guardar_mensal(user["id"]))


def calc_dividas(user_id: int) -> dict[str, Any]:
    """Resumo de dívidas: o que você deve e o que te devem (só o que falta pagar)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM dividas WHERE user_id = ? ORDER BY quitada ASC, COALESCE(vencimento, '9999') ASC, id DESC",
            (user_id,),
        ).fetchall()
    devo = sum(max(0.0, float(r["valor_total"]) - float(r["valor_pago"])) for r in rows
               if r["tipo"] == "devo" and not r["quitada"])
    me_devem = sum(max(0.0, float(r["valor_total"]) - float(r["valor_pago"])) for r in rows
                   if r["tipo"] == "me_devem" and not r["quitada"])
    return {"rows": rows, "devo": devo, "me_devem": me_devem, "tem": bool(rows)}


@app.route("/dividas", methods=["GET", "POST"])
@login_required
def dividas():
    user = current_user()
    if request.method == "POST":
        tipo = request.form.get("tipo")
        if tipo not in {"devo", "me_devem"}:
            tipo = "devo"
        descricao = sanitize_text(request.form.get("descricao"))[:120]
        pessoa = sanitize_text(request.form.get("pessoa"))[:80]
        valor_total = parse_money(request.form.get("valor_total"))
        valor_pago = parse_money(request.form.get("valor_pago"))
        vencimento = request.form.get("vencimento") or None
        if vencimento:
            try:
                date.fromisoformat(vencimento)
            except ValueError:
                vencimento = None
        if not descricao or valor_total <= 0 or valor_absurdo(valor_total):
            flash("Escreva o que é e um valor válido.")
            return redirect(url_for("dividas"))
        with get_db() as db:
            cur = db.execute(
                """INSERT INTO dividas (user_id, tipo, descricao, pessoa, valor_total, valor_pago, vencimento)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], tipo, descricao, pessoa, valor_total, min(valor_pago, valor_total), vencimento),
            )
            novo = cur.lastrowid
        flash("Dívida anotada." if tipo == "devo" else "Anotado — essa é pra você receber.")
        return redirect(url_for("dividas", novo=novo))
    return render_template("dividas.html", user=user, d=calc_dividas(user["id"]),
                           novo=request.args.get("novo", type=int), hoje=hoje_br().isoformat())


@app.route("/dividas/<int:divida_id>/pagar", methods=["POST"])
@login_required
def pagar_divida(divida_id):
    """Registra um abatimento (ou quita de uma vez quando o valor cobre o que falta)."""
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    with get_db() as db:
        d = db.execute("SELECT * FROM dividas WHERE id = ? AND user_id = ?", (divida_id, user["id"])).fetchone()
        if not d:
            flash("Dívida não encontrada.")
            return redirect(url_for("dividas"))
        falta = max(0.0, float(d["valor_total"]) - float(d["valor_pago"]))
        if valor <= 0 or valor_absurdo(valor):
            valor = falta  # sem valor informado = quitar o que falta
        novo_pago = min(float(d["valor_total"]), float(d["valor_pago"]) + valor)
        quitada = 1 if novo_pago >= float(d["valor_total"]) - 0.005 else 0
        db.execute("UPDATE dividas SET valor_pago = ?, quitada = ? WHERE id = ?", (novo_pago, quitada, divida_id))
    flash("Quitada! 🎉" if quitada else f"Abatido {money(valor)}.")
    return redirect(url_for("dividas"))


@app.route("/dividas/<int:divida_id>/delete", methods=["POST"])
@login_required
def delete_divida(divida_id):
    user = current_user()
    with get_db() as db:
        db.execute("DELETE FROM dividas WHERE id = ? AND user_id = ?", (divida_id, user["id"]))
    flash("Removida.")
    return redirect(url_for("dividas"))


@app.route("/reserva/usar-saldo-real", methods=["POST"])
@login_required
def reserva_usar_saldo_real():
    """Corrige a reserva pelo valor REAL investido no banco (resgates/aportes que o Herc
    não vê como transação de conta). O banco manda mais que o número digitado à mão."""
    user = current_user()
    real = user["saldo_investido"] if "saldo_investido" in user.keys() else None
    if real is None:
        flash("Ainda não sei quanto você tem investido. Sincronize o banco primeiro.")
        return redirect(url_for("metas"))
    with get_db() as db:
        goal = db.execute(
            """SELECT id FROM metas WHERE user_id = ? AND ativo = 1 AND LOWER(nome) LIKE '%reserva%'
               ORDER BY created_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if not goal:
            flash("Você ainda não tem uma reserva.")
            return redirect(url_for("metas"))
        db.execute("UPDATE metas SET valor_atual = ? WHERE id = ?", (float(real), goal["id"]))
    flash(f"Reserva atualizada pelo banco: {money(real)}.")
    return redirect(url_for("metas"))


@app.route("/metas/quanto-guardar", methods=["POST"])
@login_required
def definir_guardar_mensal():
    """Aceita a sugestão de quanto dá pra guardar por mês."""
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    if valor <= 0 or valor_absurdo(valor):
        flash("Valor inválido.")
        return redirect(url_for("metas"))
    with get_db() as db:
        db.execute("UPDATE usuarios SET meta_mensal = ? WHERE id = ?", (valor, user["id"]))
    session["meta_mensal"] = valor
    flash(f"Combinado: {money(valor)} por mês. Vou contar com isso no seu \"pode gastar hoje\".")
    return redirect(url_for("metas"))


@app.route("/reserva/guardar", methods=["POST"])
@login_required
def guardar_sobra():
    """Joga a sobra do mês na reserva de emergência (incrementa a meta, sem passar do alvo)."""
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    if valor <= 0 or valor_absurdo(valor):
        flash("Nada a guardar agora.")
        return redirect(url_for("home"))
    with get_db() as db:
        goal = db.execute(
            """SELECT id, meta_valor, valor_atual FROM metas
               WHERE user_id = ? AND ativo = 1 AND LOWER(nome) LIKE '%reserva%'
               ORDER BY created_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if not goal:
            flash("Você ainda não tem uma reserva. Crie uma em Metas.")
            return redirect(url_for("metas"))
        novo = min(float(goal["meta_valor"]), float(goal["valor_atual"]) + valor)
        guardado = novo - float(goal["valor_atual"])
        db.execute("UPDATE metas SET valor_atual = ? WHERE id = ?", (novo, goal["id"]))
    flash(f"{money(guardado)} guardados na reserva! 🎉")
    return redirect(url_for("home"))


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
    if meta_valor <= 0 or valor_absurdo(meta_valor):
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
    if valor <= 0 or valor_absurdo(valor):
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
            (user["id"], valor, f"Guardado na meta: {goal['nome']}", "Reserva", hoje_br().isoformat()),
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
    return redirect(voltar_para(url_for("home")))


@app.route("/meses")
@login_required
def meses():
    user = current_user()
    historico = historico_mensal(user["id"])
    fechados = [m for m in historico if not m["em_curso"]]

    escolhido = sanitize_text(request.args.get("mes"))
    if not re.fullmatch(r"\d{4}-\d{2}", escolhido or ""):
        escolhido = (fechados[0]["mes"] if fechados else
                     historico[0]["mes"] if historico else hoje_br().strftime("%Y-%m"))

    anterior = mes_anterior_a(escolhido)
    return render_template(
        "meses.html", user=user,
        historico=historico,
        teto=max([m["saiu"] for m in historico] + [m["entrou"] for m in historico] + [1.0]),
        mes=escolhido, mes_nome=nome_do_mes(escolhido),
        anterior=anterior, anterior_nome=nome_do_mes(anterior),
        linhas=comparar_categorias(user["id"], escolhido, anterior),
        detalhe=next((m for m in historico if m["mes"] == escolhido), None),
        detalhe_ant=next((m for m in historico if m["mes"] == anterior), None),
        entradas=movimentos_do_mes(user["id"], escolhido, "entrada"),
        saidas=movimentos_do_mes(user["id"], escolhido, "saida"),
    )


@app.route("/conferir")
@login_required
def conferir():
    """Todos os números abertos, pra comparar com o extrato do banco.

    Existe porque eu (Claude) passei uma sessão inteira deduzindo a causa de
    números que não batiam, sem nunca ver um dado real do Matheus — e errei
    quase todas. Aqui ele fotografa a tela e a conversa deixa de depender de
    palpite. Serve pra qualquer pessoa que olhe um número e pense "isso não é
    meu".
    """
    user = current_user()
    uid = user["id"]
    hoje = hoje_br()
    desde = hoje - timedelta(days=60)
    col = "date(COALESCE(NULLIF(data_transacao, ''), created_at))"

    with get_db() as db:
        marcacoes = db.execute(
            """SELECT fonte, no_credito, interno, tipo, COUNT(*) n,
                      COALESCE(SUM(valor), 0) t
                 FROM transacoes WHERE user_id = ?
                GROUP BY fonte, no_credito, interno, tipo
                ORDER BY t DESC""", (uid,)).fetchall()

        meses = db.execute(
            f"""SELECT {sql_mes(virada_do_usuario(uid), "COALESCE(NULLIF(data_transacao, ''), created_at)")} AS mes,
                   COALESCE(SUM(CASE WHEN tipo='entrada' AND no_credito=0 AND fonte!='ajuste'
                                      AND interno=0 THEN valor END),0) entrou,
                   COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=0 AND fonte!='ajuste'
                                      AND interno=0 THEN valor END),0) saiu,
                   COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=1 THEN valor END),0) cartao
                 FROM transacoes WHERE user_id = ?
                GROUP BY mes ORDER BY mes DESC LIMIT 6""", (uid,)).fetchall()

        janela = db.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN no_credito=0 AND fonte!='ajuste' AND interno=0
                       AND COALESCE(NULLIF(categoria,''),'')!='Reserva' THEN valor END),0) conta,
                  COALESCE(SUM(CASE WHEN no_credito=1 THEN valor END),0) cartao,
                  COALESCE(SUM(CASE WHEN fonte='ajuste' THEN valor END),0) ajuste,
                  COALESCE(SUM(CASE WHEN interno=1 THEN valor END),0) trocou_bolso,
                  COALESCE(SUM(CASE WHEN COALESCE(NULLIF(categoria,''),'')='Reserva'
                       AND no_credito=0 THEN valor END),0) guardado,
                  MIN({col}) primeiro
                FROM transacoes
               WHERE user_id=? AND tipo='saida' AND {col} >= date(?)""",
            (uid, desde.isoformat())).fetchone()

        dias = db.execute(
            f"""SELECT {col} dia, SUM(valor) t, COUNT(*) n,
                       GROUP_CONCAT(descricao, ' · ') o_que
                  FROM transacoes
                 WHERE user_id=? AND tipo='saida' AND no_credito=0 AND fonte!='ajuste'
                   AND interno=0 AND COALESCE(NULLIF(categoria,''),'')!='Reserva'
                   AND {col} >= date(?)
                 GROUP BY dia ORDER BY t DESC""",
            (uid, desde.isoformat())).fetchall()

    primeiro = None
    corridos = 1
    if janela["primeiro"]:
        try:
            primeiro = date.fromisoformat(janela["primeiro"])
            corridos = max(1, (hoje - max(primeiro, desde)).days + 1)
        except (TypeError, ValueError):
            primeiro = None
    media, _ = media_gasto_diario(uid)
    bruto = float(janela["conta"] or 0) / corridos

    return render_template(
        "conferir.html", user=user,
        marcacoes=marcacoes, meses=meses, janela=janela,
        dias=[dict(d) for d in dias], total_dias=len(dias),
        desde=desde, hoje=hoje, primeiro=primeiro, corridos=corridos,
        media=media, bruto=bruto,
        duplicatas=possiveis_duplicatas(uid),
    )


@app.route("/duplicata", methods=["POST"])
@login_required
def resolver_duplicata():
    """A pessoa respondeu se os dois lançamentos são a mesma coisa."""
    user = current_user()
    try:
        # Limite de 64 bits: acima disso o SQLite estoura em vez de não achar.
        id_manual = int(request.form.get("id_manual") or 0)
        if not 0 < id_manual < 2**63:
            id_manual = 0
    except (ValueError, TypeError):
        id_manual = 0
    resposta = request.form.get("resposta")
    with get_db() as db:
        dono = db.execute("SELECT id FROM transacoes WHERE id = ? AND user_id = ?",
                          (id_manual, user["id"])).fetchone()
        if not dono:
            flash("Não achei esse lançamento.")
            return redirect(voltar_para(url_for("home")))
        if resposta == "juntar":
            # Some o da mão: o do banco tem o valor e a data de verdade.
            db.execute("DELETE FROM transacoes WHERE id = ? AND user_id = ?",
                       (id_manual, user["id"]))
            flash("Juntei os dois. O valor que fica é o que o banco registrou.")
        else:
            db.execute("UPDATE transacoes SET dup_ok = 1 WHERE id = ? AND user_id = ?",
                       (id_manual, user["id"]))
            flash("Certo, são coisas diferentes. Não pergunto de novo.")
    return redirect(voltar_para(url_for("home")))


@app.route("/simular", methods=["GET", "POST"])
@login_required
def simular():
    """A pergunta que a pessoa faz na loja, não em casa depois."""
    user = current_user()
    bruto = request.form.get("valor") if request.method == "POST" else request.args.get("valor")
    valor = parse_money(bruto)
    resultado = None
    if valor > 0 and not valor_absurdo(valor):
        resultado = simular_gasto(user["id"], valor)
    elif bruto:
        flash("Informe um valor válido pra eu simular.")
    return render_template("simular.html", user=user, resultado=resultado,
                           valor_digitado=bruto or "")


@app.route("/recado")
@login_required
def recado():
    """O fechamento da semana em formato de mensagem, pra copiar e mandar.

    Enquanto o app não consegue avisar sozinho, quem manda é a pessoa."""
    user = current_user()
    rec = recado_da_semana(user["id"])
    return render_template(
        "recado.html", user=user, recado=rec,
        mensagem=texto_recado(rec, user["nome"]) if rec else "",
    )


@app.route("/recado/visto", methods=["POST"])
@login_required
def marcar_recado_visto():
    """Guarda pela SEMANA, não pra sempre: no domingo seguinte tem recado novo."""
    user = current_user()
    chave = sanitize_text(request.form.get("chave"))[:40]
    if re.fullmatch(r"recado:\d{4}-W\d{2}", chave or ""):
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO dicas_vistas (user_id, dica) VALUES (?, ?)",
                       (user["id"], chave))
    return redirect(voltar_para(url_for("home")))


@app.route("/saldo-inicial", methods=["POST"])
@login_required
def saldo_inicial():
    user = current_user()
    valor = parse_money(request.form.get("valor"))
    if valor <= 0 or valor_absurdo(valor):
        flash("Me diz quanto você tem na conta hoje (pode ser aproximado).")
        return redirect(url_for("home"))
    with get_db() as db:
        db.execute(
            """INSERT INTO transacoes (user_id, tipo, valor, descricao, estabelecimento, categoria, data_transacao, fonte)
               VALUES (?, 'entrada', ?, 'Saldo inicial', 'Saldo inicial', 'Outros', ?, 'ajuste')""",
            (user["id"], valor, hoje_br().isoformat()),
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

    # Orçamento do mês: TODAS as categorias (as suas e as padrão) num lugar só,
    # com gasto e limite. Antes só as criadas por você aceitavam limite.
    spending = category_month_spending(user["id"])
    salvas = {c["nome"].lower(): c for c in user_categories(user["id"])}
    orcamento = []
    for nome in expense_category_names(user["id"]):
        if nome == "Outros":
            continue
        row = salvas.get(nome.lower())
        limite = float(row["limite_mensal"] or 0) if row else 0.0
        gasto = spending.get(nome, 0.0)
        orcamento.append({
            "nome": nome,
            "icone": (row["icone"] if row else None) or "🏷️",
            "id": row["id"] if row else None,
            "propria": bool(row),
            "gasto": gasto,
            "limite": limite,
            "resta": (limite - gasto) if limite > 0 else None,
            "pct": (gasto / limite * 100.0) if limite > 0 else None,
        })
    # Quem tem limite primeiro; depois quem mais gastou
    orcamento.sort(key=lambda o: (o["limite"] <= 0, -o["gasto"]))
    total_orcado = sum(o["limite"] for o in orcamento)
    total_gasto_com_limite = sum(o["gasto"] for o in orcamento if o["limite"] > 0)
    customs = []
    fixed = []
    rules = [r for r in user_rules(user["id"]) if r["categoria_nome"] != IGNORE_RULE]
    return render_template(
        "categorias.html",
        user=user,
        customs=customs,
        fixed=fixed,
        rules=rules,
        orcamento=orcamento,
        total_orcado=total_orcado,
        total_gasto_com_limite=total_gasto_com_limite,
        month=month_label(hoje_br().strftime("%Y-%m")),
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


@app.route("/categorias/revisar", methods=["POST"])
@login_required
def revisar_categorias():
    """Reprocessa as saídas que ficaram em 'Outros' com as palavras-chave e regras atuais."""
    user = current_user()
    n = recategorize_outros(user["id"])
    if n:
        flash(f"Revisão pronta: {n} movimentações que estavam em 'Outros' ganharam categoria.")
    else:
        flash("Revisei tudo, mas não achei categoria automática pro que sobrou em 'Outros'. "
              "Esses você pode me ensinar na tela inicial 😉")
    return redirect(voltar_para(url_for("categorias")))


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
                "INSERT INTO regras_categorizacao (user_id, padrao_texto, categoria_nome, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], padrao, IGNORE_RULE, agora_br().isoformat(timespec="seconds")),
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
        # Ensinar de novo o mesmo lugar ATUALIZA a regra em vez de empilhar duplicatas
        antiga = None
        for r in db.execute("SELECT id, padrao_texto FROM regras_categorizacao WHERE user_id = ?",
                            (user["id"],)).fetchall():
            if _normalizar_regra(r["padrao_texto"]) == _normalizar_regra(padrao):
                antiga = r["id"]
                break
        if antiga:
            db.execute("UPDATE regras_categorizacao SET categoria_nome = ? WHERE id = ?",
                       (categoria, antiga))
        else:
            db.execute(
                "INSERT INTO regras_categorizacao (user_id, padrao_texto, categoria_nome, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], padrao, categoria, agora_br().isoformat(timespec="seconds")),
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
        if not descricao or valor <= 0 or valor_absurdo(valor) or not vencimento:
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
                venc = hoje_br()
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
        query += " AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) >= date(?)"
        params.append(data_inicio)
    if data_fim:
        query += " AND date(COALESCE(NULLIF(data_transacao, ''), created_at)) <= date(?)"
        params.append(data_fim)

    query += " ORDER BY datetime(COALESCE(NULLIF(data_transacao, ''), created_at)) DESC"

    # Sem limite, um ano de banco sincronizado vira uma página de ~10 MB: quase um
    # minuto só de download no 4G, e o celular travando pra renderizar 3 mil linhas.
    pagina = max(1, request.args.get("p", type=int) or 1)
    with get_db() as db:
        total = db.execute(
            query.replace("SELECT * FROM transacoes", "SELECT COUNT(*) AS n FROM transacoes", 1)
                 .split(" ORDER BY ")[0],
            params,
        ).fetchone()["n"]
        txs = db.execute(query + " LIMIT ? OFFSET ?",
                         params + [POR_PAGINA, (pagina - 1) * POR_PAGINA]).fetchall()
    ultima_pagina = max(1, -(-total // POR_PAGINA))
    pagina = min(pagina, ultima_pagina)
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
        expense_categories=[c for c in expense_category_names(user["id"]) if c != "Outros"],
        types=TRANSACTION_TYPES,
        novo=request.args.get("novo", type=int),
        pagina=pagina,
        ultima_pagina=ultima_pagina,
        total=total,
        por_pagina=POR_PAGINA,
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
        data_transacao = request.form.get("data_transacao") or hoje_br().isoformat()
        fonte = sanitize_text(request.form.get("fonte")) or "manual"
        confidence = int(parse_money(request.form.get("confidence")) or 100)
        needs_review = 1 if request.form.get("needs_review") else 0
        extra_json = request.form.get("extra_json") or ""
        no_credito = 1 if (tipo == "saida" and request.form.get("no_credito")) else 0
        if tipo not in {"entrada", "saida"}:
            flash("Escolha um tipo válido.")
            return redirect(url_for("nova_transacao"))
        if valor <= 0 or valor_absurdo(valor):
            flash("Informe um valor válido.")
            return redirect(url_for("nova_transacao"))
        try:
            date.fromisoformat(data_transacao)
        except ValueError:
            flash("Data inválida.")
            return redirect(url_for("nova_transacao"))

        # Rede de segurança do toque duplo (o JS já barra o segundo envio, mas ele
        # pode falhar). Preencher o MESMO formulário duas vezes em 5 segundos não é
        # humanamente possível — mas um toque a mais no 4G ruim é rotina, e
        # lançamento duplicado corrompe o saldo em silêncio.
        with get_db() as db:
            # BEGIN IMMEDIATE porque conferir antes não basta: dois toques chegam
            # juntos, os dois SELECTs rodam antes de qualquer INSERT comitar e os dois
            # se acham originais. Pegando a trava de escrita já na conferência, o
            # segundo espera e enxerga o primeiro.
            db.execute("BEGIN IMMEDIATE")
            repetido = db.execute(
                """SELECT id FROM transacoes
                    WHERE user_id = ? AND tipo = ? AND valor = ? AND descricao = ?
                      AND datetime(created_at) >= datetime('now', '-5 seconds')""",
                (user["id"], tipo, valor, descricao),
            ).fetchone()
            if repetido:
                new_id, era_repetido = repetido["id"], True
            else:
                cursor = db.execute(
                    """INSERT INTO transacoes
                       (user_id, tipo, valor, descricao, estabelecimento, categoria,
                        data_transacao, fonte, confidence, needs_review, extra_json, no_credito)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user["id"], tipo, valor, descricao, estabelecimento, categoria,
                        data_transacao, fonte, max(0, min(100, confidence)), needs_review,
                        extra_json, no_credito,
                    ),
                )
                new_id, era_repetido = cursor.lastrowid, False
        if not era_repetido:
            flash(("Entrada registrada." if tipo == "entrada" else "Saída registrada.")
                  + " Está aqui na sua lista.")
        return redirect(url_for("listar_transacoes", novo=new_id))
    return render_template(
        "nova_transacao.html",
        user=user,
        banco_conectado=bool(pluggy_user_item_ids(user)),
        categories=expense_category_names(user["id"]),
        income_categories=INCOME_CATEGORIES,
        types=TRANSACTION_TYPES,
    )


@app.route("/transacoes/<int:tx_id>/editar", methods=["GET", "POST"])
@login_required
def editar_transacao(tx_id):
    """Corrigir valor, data, descrição ou categoria — o banco às vezes traz nome ruim."""
    user = current_user()
    tx = transaction_for_user(tx_id, user["id"])
    if not tx:
        flash("Movimentação não encontrada.")
        return redirect(url_for("listar_transacoes"))
    if request.method == "POST":
        tipo = sanitize_text(request.form.get("tipo"))
        valor = parse_money(request.form.get("valor"))
        descricao = sanitize_text(request.form.get("descricao"))
        estabelecimento = sanitize_text(request.form.get("estabelecimento")) or descricao
        categoria = sanitize_text(request.form.get("categoria")) or categorize(user["id"], estabelecimento, descricao)
        data_transacao = request.form.get("data_transacao") or tx["data_transacao"]
        no_credito = 1 if (tipo == "saida" and request.form.get("no_credito")) else 0
        if tipo not in {"entrada", "saida"} or valor <= 0 or valor_absurdo(valor):
            flash("Confira o tipo e o valor.")
            return redirect(url_for("editar_transacao", tx_id=tx_id))
        try:
            date.fromisoformat(data_transacao)
        except (ValueError, TypeError):
            flash("Data inválida.")
            return redirect(url_for("editar_transacao", tx_id=tx_id))
        with get_db() as db:
            db.execute(
                """UPDATE transacoes SET tipo = ?, valor = ?, descricao = ?, estabelecimento = ?,
                   categoria = ?, data_transacao = ?, no_credito = ? WHERE id = ? AND user_id = ?""",
                (tipo, valor, descricao, estabelecimento, categoria, data_transacao,
                 no_credito, tx_id, user["id"]),
            )
        flash("Movimentação atualizada.")
        return redirect(url_for("listar_transacoes", novo=tx_id))
    return render_template(
        "nova_transacao.html",
        user=user,
        tx=tx,
        banco_conectado=bool(pluggy_user_item_ids(user)),
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
        forcar_credito = bool(request.form.get("fatura_cartao"))
        arquivo = request.files.get("arquivo")
        texto_colado = (request.form.get("texto_extrato") or "").strip()
        items = None

        if arquivo and arquivo.filename:
            nome = arquivo.filename.lower()
            raw = arquivo.read()
            if nome.endswith((".ofx", ".qfx")):
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("latin-1", errors="replace")
                items = parse_ofx(content)
            elif nome.endswith(".pdf"):
                if PdfReader is None:
                    flash("A leitura de PDF ainda não está ativa neste servidor (falta a biblioteca pypdf).")
                    return redirect(url_for("importar_ofx"))
                texto = extract_pdf_text(raw)
                if not texto.strip():
                    flash("Não consegui ler texto nesse PDF — pode ser um PDF escaneado (imagem). "
                          "Tente o arquivo OFX, ou copie o texto do extrato e cole no campo abaixo.")
                    return redirect(url_for("importar_ofx"))
                items = parse_bank_statement_text(texto, forcar_credito=forcar_credito)
            else:
                flash("Envie um arquivo .ofx, .qfx ou .pdf — ou cole o texto do extrato.")
                return redirect(url_for("importar_ofx"))
        elif texto_colado:
            items = parse_bank_statement_text(texto_colado, forcar_credito=forcar_credito)
        else:
            flash("Escolha o arquivo do extrato (OFX ou PDF) ou cole o texto do extrato.")
            return redirect(url_for("importar_ofx"))

        if not items:
            flash("Não encontrei movimentações. Confirme que é um extrato do banco "
                  "(com datas e valores) — se for PDF escaneado, tente o OFX ou cole o texto.")
            return redirect(url_for("importar_ofx"))
        detectou_credito = any(item.get("no_credito") for item in items)
        stats = import_ofx_transactions(user["id"], items, forcar_credito=forcar_credito)
        with get_db() as db:
            db.execute("UPDATE usuarios SET last_ofx_import = ? WHERE id = ?", (hoje_br().isoformat(), user["id"]))
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


def _pluggy_erro_detalhe(e: Exception) -> str:
    """Mostra o motivo real da falha (status HTTP + corpo) em vez de um genérico."""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            corpo = (resp.text or "")[:200]
        except Exception:
            corpo = ""
        return f"HTTP {resp.status_code} — {corpo}"
    return f"{type(e).__name__}: {e}"[:200]


@app.route("/pluggy/conectar")
@login_required
def pluggy_conectar():
    """Abre o widget Pluggy Connect (amarrado à NOSSA aplicação) pra conectar o banco."""
    if not pluggy_configured():
        flash("A conexão automática ainda não está ligada neste servidor.")
        return redirect(url_for("settings"))
    try:
        token = pluggy_connect_token(pluggy_auth())
    except Exception as e:
        flash("Não consegui abrir a conexão com a Pluggy: " + _pluggy_erro_detalhe(e))
        return redirect(url_for("settings"))
    return render_template("pluggy_conectar.html", connect_token=token)


@app.route("/pluggy/item", methods=["POST"])
@login_required
def pluggy_salvar_item():
    """Guarda o item que o widget criou (dono é a nossa aplicação → contas visíveis)."""
    user = current_user()
    item_id = sanitize_text(request.form.get("item_id"))[:80]
    if not item_id:
        flash("A conexão não retornou um item. Tente de novo.")
        return redirect(url_for("settings"))
    with get_db() as db:
        db.execute("UPDATE usuarios SET pluggy_item_id = ? WHERE id = ?", (item_id, user["id"]))
    flash("Banco conectado! Agora é só clicar em Sincronizar. 🎉")
    return redirect(url_for("settings"))


@app.route("/pluggy/testar", methods=["POST"])
@login_required
def pluggy_testar():
    """Confere as chaves e lista as contas conectadas, sem importar nada."""
    if not pluggy_configured():
        flash("A conexão automática ainda não está ligada neste servidor.")
        return redirect(url_for("settings"))
    user = current_user()
    item_ids = pluggy_user_item_ids(user)
    if not item_ids:
        flash("Conecte seu banco primeiro (botão “Conectar meu banco”).")
        return redirect(url_for("settings"))
    try:
        api_key = pluggy_auth()
    except Exception as e:
        flash("Autenticação falhou (confira o Client ID/Secret no WSGI): " + _pluggy_erro_detalhe(e))
        return redirect(url_for("settings"))
    try:
        contas = pluggy_accounts(api_key, item_ids)
    except Exception as e:
        flash("Autentiquei OK, mas não acessei o item: " + _pluggy_erro_detalhe(e))
        return redirect(url_for("settings"))
    if not contas:
        # Sem contas: prova cada item pra ver se as chaves enxergam o item
        detalhes = []
        for item_id in item_ids:
            try:
                it = _pluggy_get(api_key, f"/items/{item_id}")
                conn = (it.get("connector") or {}).get("name", "?")
                detalhes.append(f"item {item_id[:8]}… OK (conector {conn}, status {it.get('status', '?')})")
            except Exception as e:
                detalhes.append(f"item {item_id[:8]}… NÃO acessível com estas chaves — {_pluggy_erro_detalhe(e)}")
        flash("Autentiquei, mas o item não tem contas visíveis. " + " · ".join(detalhes))
        return redirect(url_for("settings"))
    # Diagnóstico: quantos lançamentos cada conta devolve em 90 dias e a data da última
    since90 = (hoje_br() - timedelta(days=90)).isoformat()
    linhas = []
    for c in contas:
        tipo = c.get("type")
        nome = c.get("name") or c.get("marketingName") or "conta"
        if tipo not in ("BANK", "CREDIT"):
            linhas.append(f"{nome} [{tipo}] — ignorada")
            continue
        try:
            data = _pluggy_get(api_key, "/v2/transactions", {"accountId": c.get("id"), "dateFrom": since90})
            res = data.get("results", [])
            total = data.get("total", len(res))
            ultima = (res[0].get("date") or "")[:10] if res else "nenhuma"
            rotulo = "cartão" if tipo == "CREDIT" else "conta"
            linhas.append(f"{nome} ({rotulo}): {total} lançamentos em 90d, última {ultima}")
        except Exception as e:
            linhas.append(f"{nome}: erro ao ler lançamentos ({_pluggy_erro_detalhe(e)})")
    flash("Conexão OK. " + " · ".join(linhas))
    return redirect(url_for("settings"))


@app.route("/pluggy/sincronizar", methods=["POST"])
@login_required
def pluggy_sincronizar():
    """Puxa os gastos do banco via Open Finance e importa (mesmo pipeline do OFX)."""
    user = current_user()
    # Chamada da tela inicial (fetch): responde JSON e a pessoa fica onde está
    ajax = request.headers.get("X-Requested-With") == "fetch"
    if not pluggy_configured():
        if ajax:
            return {"ok": False, "msg": "A conexão automática não está ligada neste servidor."}, 200
        flash("A conexão automática ainda não está ligada neste servidor.")
        return redirect(url_for("settings"))
    item_ids = pluggy_user_item_ids(user)
    if not item_ids:
        if ajax:
            return {"ok": False, "msg": "Conecte seu banco primeiro."}, 200
        flash("Conecte seu banco primeiro (botão “Conectar meu banco”).")
        return redirect(url_for("settings"))
    # Janela ampla e fixa (90 dias): o dedup por id da Pluggy cuida da sobreposição,
    # então não corremos o risco de a janela curta deixar gasto recente de fora.
    try:
        # esperar=True: o botão manual aguarda a coleta, porque a pessoa quer o dado agora
        stats, saldo_banco, saldo_investido, refresh, items = _sync_pluggy(
            user, pluggy_auth(), item_ids, esperar=True)
    except Exception as e:
        detalhe = _pluggy_erro_detalhe(e)
        if ajax:
            return {"ok": False, "msg": "Não consegui sincronizar: " + detalhe}, 200
        flash("Não consegui sincronizar: " + detalhe)
        return redirect(url_for("settings"))
    if refresh["erro_login"]:
        msg = ("O banco pediu autorização de novo (o acesso expirou ou a senha mudou). "
               "Toque em “Reconectar” pra renovar.")
        if ajax:
            return {"ok": False, "msg": msg}, 200
        flash(msg)
        return redirect(url_for("settings"))
    if refresh.get("ultima_coleta"):
        with get_db() as db:
            db.execute("UPDATE usuarios SET ultima_coleta_banco = ? WHERE id = ?",
                       (str(refresh["ultima_coleta"]), user["id"]))
    partes = [f"saldo do banco {money(saldo_banco)}"]
    if saldo_investido:
        partes.append(f"guardado {money(saldo_investido)}")
    if stats["importadas"]:
        partes.append(f"{stats['importadas']} movimentações novas")
    if stats["reconciliadas"]:
        partes.append(f"{stats['reconciliadas']} conferidas")
    if stats["ja_importadas"]:
        partes.append(f"{stats['ja_importadas']} repetidas puladas")
    # A data do lançamento mais recente mostra na hora se o banco entregou dado fresco
    datas = [i["data"] for i in items if i.get("data")]
    if datas:
        partes.append(f"mais recente em {format_date(max(datas))}")
    if not refresh["pronto"]:
        partes.append("o banco ainda está coletando — abra de novo em alguns minutos")
    elif not refresh.get("pediu"):
        partes.append("esse já é o dado mais recente que o banco liberou")
    msg = "Banco sincronizado: " + " · ".join(partes) + "."
    if ajax:
        return {"ok": True, "msg": msg, "novas": stats["importadas"],
                "pronto": refresh["pronto"]}, 200
    flash(msg)
    return redirect(url_for("listar_transacoes"))


# ------------------------
# Bloqueio por digital/rosto (WebAuthn) — o aparelho guarda a chave, o servidor
# só guarda a chave PÚBLICA. Nenhuma digital passa pela internet.
# ------------------------
def webauthn_disponivel() -> bool:
    return _webauthn is not None


def _rp_id() -> str:
    """Domínio da credencial (sem porta) — precisa bater com o site."""
    return (request.host or "").split(":")[0]


def _origem() -> str:
    return request.host_url.rstrip("/")


def user_passkeys(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM passkeys WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()


def app_tem_bloqueio(user_id: int) -> bool:
    return bool(user_passkeys(user_id))


@app.route("/seguranca/passkey/registrar/opcoes", methods=["POST"])
@login_required
def passkey_registrar_opcoes():
    if not webauthn_disponivel():
        return {"erro": "Bloqueio por digital não está ativo neste servidor."}, 503
    user = current_user()
    opcoes = _webauthn.generate_registration_options(
        rp_id=_rp_id(),
        rp_name="Hércules",
        user_id=str(user["id"]).encode(),
        user_name=user["email"],
        user_display_name=user["nome"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,  # exige digital/rosto/PIN
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(p["credential_id"] + "=="))
            for p in user_passkeys(user["id"])
        ],
    )
    session["webauthn_desafio"] = base64.b64encode(opcoes.challenge).decode()
    return Response(_webauthn.options_to_json(opcoes), mimetype="application/json")


@app.route("/seguranca/passkey/registrar", methods=["POST"])
@login_required
def passkey_registrar():
    if not webauthn_disponivel():
        return {"erro": "indisponível"}, 503
    user = current_user()
    desafio = session.pop("webauthn_desafio", None)
    if not desafio:
        return {"erro": "Sessão expirou. Tente de novo."}, 400
    try:
        v = _webauthn.verify_registration_response(
            credential=request.get_data(as_text=True),
            expected_challenge=base64.b64decode(desafio),
            expected_origin=_origem(),
            expected_rp_id=_rp_id(),
        )
    except Exception as e:
        return {"erro": f"Não consegui registrar: {type(e).__name__}"}, 400
    cred_id = base64.urlsafe_b64encode(v.credential_id).decode().rstrip("=")
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO passkeys (user_id, credential_id, public_key, sign_count, apelido)
               VALUES (?, ?, ?, ?, ?)""",
            (user["id"], cred_id, v.credential_public_key, v.sign_count, "Este aparelho"),
        )
    _marcar_desbloqueado()
    return {"ok": True}, 200


@app.route("/seguranca/passkey/entrar/opcoes", methods=["POST"])
@login_required
def passkey_entrar_opcoes():
    if not webauthn_disponivel():
        return {"erro": "indisponível"}, 503
    user = current_user()
    opcoes = _webauthn.generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(p["credential_id"] + "=="))
            for p in user_passkeys(user["id"])
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session["webauthn_desafio"] = base64.b64encode(opcoes.challenge).decode()
    return Response(_webauthn.options_to_json(opcoes), mimetype="application/json")


@app.route("/seguranca/passkey/entrar", methods=["POST"])
@login_required
def passkey_entrar():
    if not webauthn_disponivel():
        return {"erro": "indisponível"}, 503
    user = current_user()
    desafio = session.pop("webauthn_desafio", None)
    if not desafio:
        return {"erro": "Sessão expirou. Tente de novo."}, 400
    corpo = request.get_data(as_text=True)
    try:
        cred_id = json.loads(corpo).get("id", "")
    except ValueError:
        return {"erro": "resposta inválida"}, 400
    with get_db() as db:
        pk = db.execute(
            "SELECT * FROM passkeys WHERE user_id = ? AND credential_id = ?",
            (user["id"], cred_id),
        ).fetchone()
    if not pk:
        return {"erro": "Esse aparelho não está cadastrado."}, 400
    try:
        v = _webauthn.verify_authentication_response(
            credential=corpo,
            expected_challenge=base64.b64decode(desafio),
            expected_origin=_origem(),
            expected_rp_id=_rp_id(),
            credential_public_key=pk["public_key"],
            credential_current_sign_count=pk["sign_count"],
            require_user_verification=True,
        )
    except Exception as e:
        return {"erro": f"Não reconheci: {type(e).__name__}"}, 400
    with get_db() as db:
        db.execute("UPDATE passkeys SET sign_count = ? WHERE id = ?", (v.new_sign_count, pk["id"]))
    _marcar_desbloqueado()
    return {"ok": True}, 200


@app.route("/seguranca/passkey/remover", methods=["POST"])
@login_required
def passkey_remover():
    user = current_user()
    with get_db() as db:
        db.execute("DELETE FROM passkeys WHERE user_id = ?", (user["id"],))
    _marcar_desbloqueado()
    flash("Bloqueio por digital desativado.")
    return redirect(url_for("settings"))


@app.route("/ajuda")
def ajuda():
    """Pública de propósito: o link é mandado no WhatsApp pra quem ainda nem tem conta."""
    return render_template("ajuda.html")


@app.route("/sobre")
def sobre():
    """Aberta sem login: e' o que a pessoa lê antes de decidir criar conta."""
    return render_template("sobre.html", user=current_user())


@app.route("/termos")
def termos():
    """Aberta sem login: quem ainda não criou conta precisa poder ler o combinado."""
    return render_template("termos.html", user=current_user())


@app.route("/privacidade")
def privacidade():
    """Página pública: qualquer um pode ler ANTES de criar conta ou conectar o banco."""
    return render_template("privacidade.html")


@app.route("/conta/apagar", methods=["POST"])
@login_required
def apagar_conta():
    """Apaga a conta e tudo que é dela. Sem isso, a política de privacidade mentiria."""
    user = current_user()
    confirmacao = sanitize_text(request.form.get("confirmacao")).upper()
    if confirmacao != "APAGAR":
        flash("Pra apagar a conta, escreva APAGAR no campo de confirmação.")
        return redirect(url_for("settings"))
    with get_db() as db:
        anexos = db.execute("SELECT arquivo FROM notas WHERE user_id = ? AND arquivo IS NOT NULL",
                            (user["id"],)).fetchall()
    for a in anexos:
        remove_uploaded_file(a["arquivo"])
    with get_db() as db:
        # As tabelas filhas têm ON DELETE CASCADE, então some tudo junto
        db.execute("DELETE FROM usuarios WHERE id = ?", (user["id"],))
    session.clear()
    flash("Sua conta e todos os seus dados foram apagados. Obrigado por ter usado o Herc. 🦁")
    return redirect(url_for("login"))


@app.route("/bloqueado")
@login_required
def app_bloqueado():
    if not app_tem_bloqueio(session["user_id"]):
        return redirect(url_for("home"))
    return render_template("bloqueado.html")


# De quantos em quantos minutos o app se atualiza sozinho ao ser aberto
SYNC_AUTO_MINUTOS = 30


def _sync_pluggy(user, api_key, item_ids, esperar: bool, min_horas: float = 0):
    """Núcleo compartilhado entre o botão (espera a coleta) e o automático (não espera).
    Devolve (stats, saldo_banco, refresh) ou levanta exceção."""
    refresh = pluggy_refresh_items(api_key, item_ids, espera_max=24 if esperar else 0,
                                   min_horas=min_horas)
    contas = pluggy_accounts(api_key, item_ids)
    items = pluggy_fetch_items(api_key, contas, (hoje_br() - timedelta(days=90)).isoformat())
    saldo_banco = sum(float(c.get("balance") or 0) for c in contas if c.get("type") == "BANK")
    try:
        saldo_investido = pluggy_investimentos(api_key, item_ids)
    except Exception:
        saldo_investido = None  # banco sem investimento: não quebra o sync
    # O próprio banco diz quando a fatura fecha e vence — melhor que perguntar
    for c in contas:
        if c.get("type") != "CREDIT":
            continue
        cd = c.get("creditData") or {}
        fecha = dia_de_data_api(cd.get("balanceCloseDate"))
        vence = dia_de_data_api(cd.get("balanceDueDate"))
        if fecha or vence:
            with get_db() as db:
                if fecha:
                    db.execute("UPDATE usuarios SET cartao_fechamento = ? WHERE id = ?",
                               (fecha, user["id"]))
                if vence:
                    db.execute("UPDATE usuarios SET cartao_vencimento = ? WHERE id = ?",
                               (vence, user["id"]))
            break
    agora = agora_br().isoformat(timespec="seconds")
    hoje = hoje_br().isoformat()
    with get_db() as db:
        if saldo_investido is None:
            db.execute(
                "UPDATE usuarios SET last_ofx_import = ?, last_sync_at = ?, saldo_banco = ? WHERE id = ?",
                (hoje, agora, saldo_banco, user["id"]))
        else:
            db.execute(
                """UPDATE usuarios SET last_ofx_import = ?, last_sync_at = ?, saldo_banco = ?,
                   saldo_investido = ? WHERE id = ?""",
                (hoje, agora, saldo_banco, saldo_investido, user["id"]))
    stats = import_ofx_transactions(user["id"], items)
    return stats, saldo_banco, saldo_investido, refresh, items


@app.route("/pluggy/sync-auto", methods=["POST"])
@login_required
def pluggy_sync_auto():
    """Atualização silenciosa ao abrir o app. Não espera a coleta do banco terminar
    (senão a tela travaria); pede a coleta agora e importa o que já chegou — na próxima
    abertura, o que foi pedido agora já está lá. Só roda a cada SYNC_AUTO_MINUTOS."""
    user = current_user()
    item_ids = pluggy_user_item_ids(user)
    if not pluggy_configured() or not item_ids:
        return {"ok": False, "motivo": "sem_banco"}, 200

    ultima = user["last_sync_at"] if "last_sync_at" in user.keys() else None
    if ultima:
        try:
            if agora_br() - datetime.fromisoformat(ultima) < timedelta(minutes=SYNC_AUTO_MINUTOS):
                return {"ok": True, "pulou": True, "novas": 0}, 200
        except ValueError:
            pass
    try:
        stats, saldo, _inv, _refresh, _items = _sync_pluggy(
            user, pluggy_auth(), item_ids, esperar=False, min_horas=3)
    except Exception:
        return {"ok": False, "motivo": "falhou"}, 200  # silencioso: não atrapalha o uso
    return {"ok": True, "novas": stats["importadas"], "saldo": saldo}, 200


@app.route("/pluggy/recomecar", methods=["POST"])
@login_required
def pluggy_recomecar():
    """Zera o histórico do Herc pra o banco virar a fonte única e limpa. Mantém regras e metas."""
    user = current_user()
    with get_db() as db:
        db.execute("DELETE FROM transacoes WHERE user_id = ?", (user["id"],))
        db.execute("UPDATE usuarios SET saldo_banco = NULL, last_ofx_import = NULL WHERE id = ?", (user["id"],))
    flash("Histórico limpo. Agora clique em Sincronizar pra puxar tudo do banco, sem duplicatas.")
    return redirect(url_for("settings"))


# ------------------------
# Export
# ------------------------
# ------------------------
# Saúde do app (só pra quem mantém — desligado por padrão)
# ------------------------
ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()


def eh_admin(user) -> bool:
    """Sem ADMIN_EMAIL no ambiente ninguém é admin — nem por engano, nem por
    conta criada com o e-mail certo antes de a variável existir."""
    if not ADMIN_EMAIL or not user:
        return False
    return (user["email"] or "").strip().lower() == ADMIN_EMAIL


def _so_admin():
    """404 em vez de 403: quem não é admin não descobre nem que a tela existe."""
    from werkzeug.exceptions import NotFound
    if not eh_admin(current_user()):
        raise NotFound()


@app.context_processor
def _admin_no_menu():
    try:
        return {"eh_admin": eh_admin(current_user()) if session.get("user_id") else False}
    except Exception:
        return {"eh_admin": False}


@app.route("/saude")
@login_required
def saude_app():
    _so_admin()
    hoje = hoje_br()
    with get_db() as db:
        u = db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS ativos7,
                      SUM(CASE WHEN last_seen  = ? THEN 1 ELSE 0 END) AS hoje,
                      SUM(CASE WHEN DATE(created_at) >= ? THEN 1 ELSE 0 END) AS novos7
                 FROM usuarios""",
            ((hoje - timedelta(days=7)).isoformat(), hoje.isoformat(),
             (hoje - timedelta(days=7)).isoformat()),
        ).fetchone()
        pessoas = db.execute(
            """SELECT nome, perfil, DATE(created_at) AS entrou, last_seen,
                      (SELECT COUNT(*) FROM transacoes t WHERE t.user_id = usuarios.id) AS lancamentos
                 FROM usuarios ORDER BY COALESCE(last_seen, '') DESC, id DESC LIMIT 25"""
        ).fetchall()

    return render_template(
        "saude.html", user=current_user(),
        u=u, pessoas=pessoas, hoje=hoje,
        backups=listar_backups(), ultimo=ultimo_backup(), erros=erros_recentes(),
        cifrado=cifragem_ligada(),
    )


@app.route("/saude/backup", methods=["POST"])
@login_required
def saude_backup_agora():
    _so_admin()
    try:
        caminho = fazer_backup()
        flash(f"Backup feito: {caminho.name}")
    except Exception as e:
        flash(f"Não consegui fazer o backup: {e}")
    return redirect(url_for("saude_app"))


@app.route("/saude/backup/<nome>")
@login_required
def saude_baixar_backup(nome):
    """Tira uma cópia do servidor — é o que protege contra perder o servidor inteiro.
    O nome é conferido contra a lista real, nunca concatenado: senão viraria uma
    porta pra baixar qualquer arquivo do disco."""
    _so_admin()
    from werkzeug.exceptions import NotFound
    alvo = next((p for p in listar_backups() if p.name == nome), None)
    if alvo is None:
        raise NotFound()
    app.logger.warning("Backup %s baixado por %s", nome, session.get("user_id"))
    return send_from_directory(alvo.parent, alvo.name, as_attachment=True)


@app.route("/exportar-ir")
@login_required
def exportar_ir():
    user = current_user()
    year = request.args.get("year", str(hoje_br().year))
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
# ------------------------
# Errors
# ------------------------
@app.errorhandler(413)
def too_large(_):
    flash("Arquivo muito grande. Envie até 16 MB.")
    return redirect(voltar_para(url_for("home")))


@app.errorhandler(404)
def nao_encontrado(_):
    return render_template("erro.html", codigo=404,
                           titulo="Essa página não existe",
                           recado="Pode ser um link velho meu. Volta pro início que eu te mostro tudo."), 404


@app.errorhandler(Exception)
def erro_inesperado(e):
    """Antes disso, um erro entregava tela branca: a pessoa fechava o app e
    não contava pra ninguém — e o silêncio era lido como 'não gostou'."""
    if isinstance(e, HTTPException):
        return e
    codigo = registrar_erro(e)
    return render_template("erro.html", codigo=500, ref=codigo,
                           titulo="Deu ruim aqui do meu lado",
                           recado="Não foi culpa sua, e seus dados estão salvos. "
                                  "Já anotei o que aconteceu pra consertar."), 500


if __name__ == "__main__":
    # host 0.0.0.0 permite testar pelo celular na mesma rede (http://IP-do-PC:5000)
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
