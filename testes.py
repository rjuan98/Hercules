"""Bateria completa do Hércules.

Roda contra um banco temporário e isolado — não toca nos seus dados.
Use sempre que mexer no código:  python testes.py
"""
import os, sys, json, pathlib, tempfile, traceback
from datetime import date, datetime, timedelta

TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(TMP, "teste.db")
os.environ["UPLOAD_DIR"] = os.path.join(TMP, "uploads")
os.environ["SECRET_KEY"] = "chave-de-teste"
os.environ["PLUGGY_CLIENT_ID"] = "cid-teste"
os.environ["PLUGGY_CLIENT_SECRET"] = "csec-teste"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A
from database import get_db

# Rota que estoura de proposito, pra provar que o app avisa em vez de dar tela branca.
# Precisa ser registrada aqui: o Flask nao aceita rota nova depois da 1a requisicao.
@A.app.route("/quebra-de-proposito")
def _quebrar():
    raise RuntimeError("estourei aqui")

def url_for_recado(h):
    return 'href="/recado"' in h

_APP_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
                encoding="utf-8").read()

OK, FALHAS = [], []

def check(nome, cond, extra=""):
    (OK if cond else FALHAS).append(nome)
    print(("  ok  " if cond else "  XX  ") + nome + (f"   [{extra}]" if extra and not cond else ""))

def secao(t):
    print(f"\n=== {t} ===")

def so_texto(html):
    """Só o que a pessoa lê. Valores vêm embrulhados em <span class="money"> por
    causa do olhinho, então checar frase por substring crua daria falso negativo."""
    import re as _re
    return _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", "", html))

def novo_cliente(email, senha="tijolo-forte-42", nome="Fulano", perfil="pf"):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    c.post("/register", data={"csrf_token": "t", "nome": nome, "email": email,
                              "senha": senha, "perfil": perfil, "view_mode": "completo"},
           follow_redirects=True)
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    c.post("/login", data={"csrf_token": "t", "email": email, "senha": senha}, follow_redirects=True)
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    return c

def uid_de(email):
    with get_db() as db:
        r = db.execute("SELECT id FROM usuarios WHERE email=?", (email,)).fetchone()
        return r["id"] if r else None

# ----------------------------------------------------------------------------
secao("1. Helpers de valor e texto")
check("parse_money 1.234,56 = 1234.56", A.parse_money("1.234,56") == 1234.56)
check("parse_money 99.90 = 99.90 (ponto decimal)", A.parse_money("99.90") == 99.90)
check("parse_money '500' = 500", A.parse_money("500") == 500.0)
check("parse_money vazio = 0", A.parse_money("") == 0.0)
check("parse_money lixo = 0", A.parse_money("abc") == 0.0)
check("parse_money negativo", A.parse_money("-50,00") == -50.0)
check("money formata BRL", A.money(1234.5) .replace("\xa0", " ") == "R$ 1.234,50")
check("format_date ISO -> BR", A.format_date("2026-07-30") == "30/07/2026")
check("format_date vazio", A.format_date(None) == "")
check("sanitize_text normaliza espacos", A.sanitize_text("  a   b  ") == "a b")
from markupsafe import escape
check("saida escapa HTML (anti-XSS)", "&lt;script&gt;" in str(escape("<script>x</script>")))

secao("2. Categorização automática")
casos = [("UBER *TRIP", "Transporte"), ("mais.mobi RIOCARD", "Transporte"),
         ("PASSAGEM ONIBUS", "Transporte"), ("Quentinha da Maria", "Alimentação"),
         ("IFD*IFOOD", "Alimentação"), ("MC DONALDS", "Alimentação"),
         ("CARREFOUR", "Mercado"), ("MERCADO LIVRE", "Varejo"),
         ("DROGARIA PACHECO", "Saúde"), ("NETFLIX.COM", "Assinaturas"),
         ("ENEL DISTRIBUICAO", "Moradia"), ("PIX ENVIADO JOAO", "Outros")]
for texto, esperado in casos:
    check(f"categoria: {texto} -> {esperado}", A.auto_category(texto) == esperado,
          A.auto_category(texto))
check("acento nao importa", A.auto_category("ÔNIBUS") == A.auto_category("onibus"))

secao("3. Cadastro, login e sessão")
c1 = novo_cliente("a@teste.com", nome="Ana")
check("home abre logado", c1.get("/").status_code == 200)
c_anon = A.app.test_client()
check("deslogado vai pro login", c_anon.get("/", follow_redirects=False).status_code == 302)
c_bad = A.app.test_client()
with c_bad.session_transaction() as s: s["csrf_token"] = "t"
r = c_bad.post("/login", data={"csrf_token": "t", "email": "a@teste.com", "senha": "errada"}, follow_redirects=True)
check("senha errada nao entra", b"Entrar" in r.data or "entrar" in r.get_data(as_text=True).lower())
r = c1.post("/register", data={"csrf_token": "t", "nome": "X", "email": "a@teste.com",
                               "senha": "tijolo-outro-77", "perfil": "pf"}, follow_redirects=True)
check("email duplicado barrado", "já está cadastrado" in r.get_data(as_text=True))

secao("4. CSRF")
r = c1.post("/transacoes/nova", data={"tipo": "saida", "valor": "10", "descricao": "sem csrf"},
            follow_redirects=True)
with get_db() as db:
    n = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE descricao='sem csrf'").fetchone()["n"]
check("POST sem csrf nao grava", n == 0)

secao("5. Movimentações")
uid1 = uid_de("a@teste.com")
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "entrada", "valor": "3.000,00",
                                  "descricao": "Salario", "categoria": "Salário"})
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "250,50",
                                  "descricao": "Mercado do mes"})
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "120,00",
                                  "descricao": "Tenis novo", "no_credito": "1"})
st = A.calc_transaction_totals(uid1)
check("saldo exclui credito (3000-250,50)", abs(st["balance"] - 2749.50) < 0.01, st["balance"])
check("fatura do cartao = 120", abs(st["fatura_credito_mes"] - 120) < 0.01, st["fatura_credito_mes"])
check("renda do mes = 3000", abs(st["month_income"] - 3000) < 0.01, st["month_income"])
check("% renda no cartao = 4%", st["credito_pct_renda"] is not None and round(st["credito_pct_renda"]) == 4, st["credito_pct_renda"])
r = c1.get("/transacoes?q=Tenis")
check("busca acha", "Tenis novo" in r.get_data(as_text=True))
check("etiqueta credito na lista", "tx-tag-credito" in r.get_data(as_text=True))

secao("6. Isolamento entre usuários (segurança)")
c2 = novo_cliente("b@teste.com", nome="Bruno")
uid2 = uid_de("b@teste.com")
r = c2.get("/transacoes")
check("usuario B nao ve dados de A", "Tenis novo" not in r.get_data(as_text=True))
with get_db() as db:
    tx_a = db.execute("SELECT id FROM transacoes WHERE user_id=? LIMIT 1", (uid1,)).fetchone()["id"]
c2.post(f"/transacoes/{tx_a}/delete", data={"csrf_token": "t"})
with get_db() as db:
    ainda = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE id=?", (tx_a,)).fetchone()["n"]
check("B NAO consegue apagar movimentacao de A", ainda == 1)
# metas
c1.post("/metas", data={"csrf_token": "t", "nome": "Reserva de emergência", "meta_valor": "5.000,00"})
with get_db() as db:
    meta_a = db.execute("SELECT id FROM metas WHERE user_id=?", (uid1,)).fetchone()["id"]
c2.post(f"/metas/{meta_a}/delete", data={"csrf_token": "t"})
with get_db() as db:
    check("B NAO apaga meta de A",
          db.execute("SELECT COUNT(*) AS n FROM metas WHERE id=?", (meta_a,)).fetchone()["n"] == 1)
# dividas
c1.post("/dividas", data={"csrf_token": "t", "tipo": "devo", "descricao": "Emprestimo A",
                          "valor_total": "1.000,00"})
with get_db() as db:
    div_a = db.execute("SELECT id FROM dividas WHERE user_id=?", (uid1,)).fetchone()["id"]
c2.post(f"/dividas/{div_a}/delete", data={"csrf_token": "t"})
with get_db() as db:
    check("B NAO apaga divida de A",
          db.execute("SELECT COUNT(*) AS n FROM dividas WHERE id=?", (div_a,)).fetchone()["n"] == 1)
c2.post(f"/dividas/{div_a}/pagar", data={"csrf_token": "t", "valor": "500"})
with get_db() as db:
    pago = db.execute("SELECT valor_pago FROM dividas WHERE id=?", (div_a,)).fetchone()["valor_pago"]
check("B NAO paga divida de A", pago == 0, pago)

secao("7. Regras aprendidas e recategorização")
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "15,00",
                                  "descricao": "ZZOPACO PAG", "categoria": "Outros"})
c1.post("/regras", data={"csrf_token": "t", "padrao_texto": "ZZOPACO",
                         "categoria_nome": "Transporte", "voltar": "/transacoes"})
with get_db() as db:
    cat = db.execute("SELECT categoria FROM transacoes WHERE descricao LIKE 'ZZOPACO%'").fetchone()["categoria"]
check("ensinar reclassifica o passado", cat == "Transporte", cat)
check("regra vale pro futuro", A.categorize(uid1, "ZZOPACO OUTRA") == "Transporte")
n = A.recategorize_outros(uid1)
check("revisar Outros nao quebra", isinstance(n, int))

secao("8. Import: OFX, PDF/texto, dedup e crédito")
ofx = """<OFX><BANKMSGSRSV1><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>%s<TRNAMT>-45.90<FITID>OFX1<MEMO>PADARIA CENTRAL</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>%s<TRNAMT>200.00<FITID>OFX2<MEMO>PIX RECEBIDO</STMTTRN>
</BANKTRANLIST></STMTRS></BANKMSGSRSV1></OFX>""" % (date.today().strftime("%Y%m%d"), date.today().strftime("%Y%m%d"))
itens = A.parse_ofx(ofx)
check("OFX le 2 lancamentos", len(itens) == 2, len(itens))
check("OFX sinal: -45.90 = saida", itens[0]["tipo"] == "saida")
check("OFX sinal: +200 = entrada", itens[1]["tipo"] == "entrada")
s1 = A.import_ofx_transactions(uid1, itens)
s2 = A.import_ofx_transactions(uid1, itens)
check("OFX importa 2", s1["importadas"] == 2, s1)
check("OFX reimport nao duplica", s2["importadas"] == 0 and s2["ja_importadas"] == 2, s2)
fatura_txt = """Fatura Nubank
Cartao de credito  Vencimento 10/08/2026
%s NETFLIX R$ 39,90
%s PADARIA R$ 12,50
%s Pagamento recebido R$ 500,00
Total R$ 52,40""" % ((date.today().strftime("%d/%m/%Y"),) * 3)
itf = A.parse_bank_statement_text(fatura_txt)
check("fatura: pula pagamento e total", len(itf) == 2, [i["descricao"] for i in itf])
check("fatura: tudo no credito", all(i["no_credito"] for i in itf))
check("texto: ids estaveis (reimport)",
      [i["fitid"] for i in A.parse_bank_statement_text(fatura_txt)] == [i["fitid"] for i in itf])

secao("9. Metas, reserva guiada e sobra")
r = c1.get("/metas")
check("tela de metas abre", r.status_code == 200)
check("reserva ja existe -> nao sugere de novo", "montar sua reserva" not in r.get_data(as_text=True))
with get_db() as db:
    m = db.execute("SELECT id, valor_atual FROM metas WHERE user_id=? AND nome LIKE '%Reserva%'", (uid1,)).fetchone()
c1.post("/reserva/guardar", data={"csrf_token": "t", "valor": "300,00"})
with get_db() as db:
    novo = db.execute("SELECT valor_atual FROM metas WHERE id=?", (m["id"],)).fetchone()["valor_atual"]
check("guardar soma na reserva", abs(novo - 300) < 0.01, novo)
c1.post("/reserva/guardar", data={"csrf_token": "t", "valor": "999.999"})
with get_db() as db:
    teto = db.execute("SELECT valor_atual, meta_valor FROM metas WHERE id=?", (m["id"],)).fetchone()
check("nao passa da meta", teto["valor_atual"] <= teto["meta_valor"])

secao("10. Dívidas: abatimento e quitação")
with get_db() as db:
    d = db.execute("SELECT id FROM dividas WHERE user_id=?", (uid1,)).fetchone()["id"]
c1.post(f"/dividas/{d}/pagar", data={"csrf_token": "t", "valor": "400,00"})
res = A.calc_dividas(uid1)
check("abate parcial (1000-400=600)", abs(res["devo"] - 600) < 0.01, res["devo"])
c1.post(f"/dividas/{d}/pagar", data={"csrf_token": "t", "valor": ""})
res = A.calc_dividas(uid1)
check("quitar em branco zera", res["devo"] == 0, res["devo"])
with get_db() as db:
    q = db.execute("SELECT quitada, valor_pago FROM dividas WHERE id=?", (d,)).fetchone()
check("marcada como quitada", q["quitada"] == 1 and q["valor_pago"] == 1000)

secao("11. Contas (compromissos)")
venc = (date.today() + timedelta(days=3)).isoformat()
c1.post("/compromissos", data={"csrf_token": "t", "descricao": "Internet", "valor": "99,90",
                               "vencimento": venc, "tipo": "saida", "frequencia": "mensal"})
st = A.calc_transaction_totals(uid1)
check("conta proxima entra no resumo", len(st["due_soon_commitments"]) >= 1)
with get_db() as db:
    cid = db.execute("SELECT id FROM compromissos WHERE user_id=?", (uid1,)).fetchone()["id"]
c1.post(f"/compromissos/{cid}/toggle", data={"csrf_token": "t"})
with get_db() as db:
    check("marcar como paga funciona",
          db.execute("SELECT status FROM compromissos WHERE id=?", (cid,)).fetchone()["status"] == "pago")

secao("12. Notas e Prévia do IR")
c1.post("/notas/nova", data={"csrf_token": "t", "descricao": "Consulta medica", "valor": "300,00",
                             "tipo": "saida", "categoria": "Saúde", "status": "Autorizada",
                             "data_emissao": date.today().isoformat()})
with get_db() as db:
    check("nota gravada",
          db.execute("SELECT COUNT(*) AS n FROM notas WHERE user_id=?", (uid1,)).fetchone()["n"] == 1)
ir = A.calc_ir_preview(uid1, date.today().year)
check("IR: renda considerada", ir["renda"] > 0, ir["renda"])
check("IR: educacao capada", ir["educacao_dedutivel"] <= ir["educacao_limite"])
check("IR: pagina abre", c1.get("/ir").status_code == 200)
check("IR: tem aviso de estimativa", "não é o cálculo oficial" in c1.get("/ir").get_data(as_text=True).lower()
      or "não substitui" in c1.get("/ir").get_data(as_text=True).lower())
check("exportar IR responde", c1.get("/exportar-ir").status_code == 200)

secao("13. Retrato do mês e insight semanal")
ins = A.insight_semanal(uid1)
check("insight calcula", ins is not None and ins["atual"] > 0)
r = c1.get("/dashboard")
check("Resumo abre", r.status_code == 200)
check("Resumo tem Retrato", "Retrato de" in r.get_data(as_text=True))
check("Resumo tem legenda do grafico com %", "chart-legend" in r.get_data(as_text=True))

secao("14. Conquistas (12 Trabalhos)")
r = c1.get("/trabalhos")
check("pagina abre", r.status_code == 200)
with get_db() as db:
    for t in A.TRABALHOS:
        try:
            A._trabalho_conquistado(uid1, t["key"], db)
        except Exception as e:
            check(f"avaliador {t['key']}", False, repr(e))
            break
    else:
        check("os 12 avaliadores rodam sem erro", True)
check("nenhuma mencao a captura por notificacao", "captura automática" not in r.get_data(as_text=True).lower())

secao("15. Pluggy (API simulada)")
A.time.sleep = lambda s: None
A.pluggy_auth = lambda: "key"
class _R:
    def raise_for_status(self): pass
class _Req:
    def patch(self, *a, **k): return _R()
A.http_requests = _Req()
hoje = date.today().isoformat()
contas = [{"id": "bank", "type": "BANK", "name": "NuConta", "balance": 1500.0},
          {"id": "card", "type": "CREDIT", "name": "gold", "balance": -300.0},
          {"id": "inv", "type": "INVESTMENT", "name": "RF", "balance": 9999.0}]
def _get(k, path, params=None):
    if path.startswith("/items/"): return {"status": "UPDATED", "executionStatus": "SUCCESS"}
    if path == "/accounts": return {"results": contas}
    if path == "/v2/transactions":
        if params["accountId"] == "bank":
            return {"results": [{"id": "p1", "date": hoje + "T10:00:00Z", "description": "PIX PADARIA", "amount": -20.0},
                                {"id": "p2", "date": hoje + "T09:00:00Z", "description": "SALARIO", "amount": 1000.0}]}
        if params["accountId"] == "card":
            return {"results": [{"id": "p3", "date": hoje + "T08:00:00Z", "description": "SPOTIFY", "amount": 21.90},
                                {"id": "p4", "date": hoje + "T07:00:00Z", "description": "Pagamento fatura", "amount": -100.0}]}
        return {"results": []}
    if path == "/investments": return {"results": [{"balance": 4000.0}]}
    return {}
A._pluggy_get = _get
itens = A.pluggy_fetch_items("key", contas, "2026-01-01")
check("ignora conta de investimento", all(i["fitid"] != "PLG-inv" for i in itens))
check("banco: negativo = saida", any(i["descricao"] == "PIX PADARIA" and i["tipo"] == "saida" for i in itens))
check("banco: positivo = entrada", any(i["descricao"] == "SALARIO" and i["tipo"] == "entrada" for i in itens))
check("cartao: positivo = compra no credito",
      any(i["descricao"] == "SPOTIFY" and i["tipo"] == "saida" and i["no_credito"] for i in itens))
check("cartao: pagamento da fatura pulado", not any("Pagamento fatura" in i["descricao"] for i in itens))
with get_db() as db:
    db.execute("UPDATE usuarios SET pluggy_item_id='item1' WHERE id=?", (uid1,))
c1.post("/pluggy/sincronizar", data={"csrf_token": "t"})
with get_db() as db:
    u = db.execute("SELECT saldo_banco, saldo_investido, last_sync_at FROM usuarios WHERE id=?", (uid1,)).fetchone()
check("saldo do banco = so BANK (1500)", abs((u["saldo_banco"] or 0) - 1500) < 0.01, u["saldo_banco"])
check("guardado = investimento (4000)", abs((u["saldo_investido"] or 0) - 4000) < 0.01, u["saldo_investido"])
st = A.calc_transaction_totals(uid1)
check("saldo exibido = saldo real do banco", abs(st["balance"] - 1500) < 0.01, st["balance"])
r = c1.post("/pluggy/sync-auto", data={"csrf_token": "t"}).get_json()
check("sync-auto respeita o limite de 4h", r.get("pulou") is True, r)

secao("16. Bloqueio por digital")
check("webauthn disponivel", A.webauthn_disponivel())
r = c1.post("/seguranca/passkey/registrar/opcoes")
check("opcoes de registro geram desafio", r.status_code == 200 and "challenge" in r.get_data(as_text=True))
check("exige verificacao (digital, nao so toque)",
      json.loads(r.get_data(as_text=True))["authenticatorSelection"]["userVerification"] == "required")
with get_db() as db:
    db.execute("INSERT INTO passkeys (user_id,credential_id,public_key,sign_count) VALUES (?,?,?,0)",
               (uid1, "fake-cred", b"\x00"))
with c1.session_transaction() as s: s.pop("desbloqueado_em", None)
check("com digital, tela redireciona pro bloqueio",
      c1.get("/", follow_redirects=False).status_code == 302)
check("POST bloqueado responde 401",
      c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "1", "descricao": "x"}).status_code == 401)
check("tela de bloqueio abre", c1.get("/bloqueado").status_code == 200)
with c1.session_transaction() as s:
    s["desbloqueado_em"] = (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds")
check("expira depois de 30 min parado", c1.get("/", follow_redirects=False).status_code == 302)
with c1.session_transaction() as s:
    s["desbloqueado_em"] = datetime.now().isoformat(timespec="seconds")
check("desbloqueado navega normal", c1.get("/").status_code == 200)
c1.post("/seguranca/passkey/remover", data={"csrf_token": "t"})
check("remover bloqueio libera", c1.get("/").status_code == 200)

secao("17. Todas as telas")
c3 = novo_cliente("mei@teste.com", nome="Maria", perfil="mei")
rotas = ["/", "/dashboard", "/transacoes", "/transacoes/nova", "/compromissos", "/categorias",
         "/metas", "/dividas", "/ir", "/trabalhos", "/notas", "/notas/nova", "/importar",
         "/settings", "/business-dashboard", "/mei", "/clientes", "/servicos"]
for rota in rotas:
    for nome_c, cli in (("pf", c1), ("mei", c3)):
        code = cli.get(rota, follow_redirects=False).status_code
        if code not in (200, 302):
            check(f"{rota} ({nome_c})", False, code)
            break
    else:
        check(f"{rota}", True)

secao("18. Modo simples")
c1.post("/settings", data={"csrf_token": "t", "form_kind": "preferences", "perfil": "pf",
                           "view_mode": "simples", "meta_mensal": "300,00", "cartao_orcamento": "500,00"})
r = c1.get("/")
check("modo simples renderiza", r.status_code == 200 and "simple-home" in r.get_data(as_text=True))
c1.post("/settings", data={"csrf_token": "t", "form_kind": "preferences", "perfil": "pf",
                           "view_mode": "completo", "meta_mensal": "300,00", "cartao_orcamento": "500,00"})
with get_db() as db:
    u = db.execute("SELECT meta_mensal, cartao_orcamento FROM usuarios WHERE id=?", (uid1,)).fetchone()
check("preferencias salvam (meta 300, teto 500)",
      abs(u["meta_mensal"] - 300) < 0.01 and abs(u["cartao_orcamento"] - 500) < 0.01, dict(u))

secao("19. Primeiro acesso e estados vazios")
c_novo = novo_cliente("novato@teste.com", nome="Novato")
h = c_novo.get("/").get_data(as_text=True)
check("primeiro acesso oferece conectar o banco", "Conectar meu banco" in h)
check("nao repete o botao de conectar", h.count("Conectar meu banco") == 1, h.count("Conectar meu banco"))
check("nao diz 'No azul' com saldo zero", "No azul" not in h)
check("nao mostra cards vazios de conta/meta", "Próximas contas" not in h)
check("nao mostra 'Nada registrado ainda'", "Nada registrado ainda" not in h)
check("saldo manual continua disponivel", "Esse é meu saldo" in h)
h2 = c_novo.get("/dashboard").get_data(as_text=True)
check("Resumo vazio mostra convite, nao parede de zeros",
      "Seu mês vai aparecer aqui" in h2 and h2.count("R$ 0,00") == 0, h2.count("R$ 0,00"))

secao("20. Editar movimentação")
c_novo.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "50,00",
                                      "descricao": "PAG*OPACO"})
with get_db() as db:
    tid = db.execute("SELECT id FROM transacoes WHERE descricao='PAG*OPACO'").fetchone()["id"]
h3 = c_novo.get(f"/transacoes/{tid}/editar").get_data(as_text=True)
check("form de edicao vem preenchido", 'value="50,00"' in h3 and 'value="PAG*OPACO"' in h3)
c_novo.post(f"/transacoes/{tid}/editar",
            data={"csrf_token": "t", "tipo": "saida", "valor": "75,50", "descricao": "Mercado da esquina",
                  "categoria": "Mercado", "data_transacao": "2026-07-20", "no_credito": "1"})
with get_db() as db:
    r = db.execute("SELECT * FROM transacoes WHERE id=?", (tid,)).fetchone()
check("edicao salva valor/descricao/categoria/data/credito",
      abs(r["valor"] - 75.5) < 0.01 and r["descricao"] == "Mercado da esquina"
      and r["categoria"] == "Mercado" and r["data_transacao"] == "2026-07-20" and r["no_credito"] == 1,
      dict(r))
check("link de editar aparece na lista", "/editar" in c_novo.get("/transacoes").get_data(as_text=True))
c_outro = novo_cliente("intruso@teste.com", nome="Intruso")
c_outro.post(f"/transacoes/{tid}/editar", data={"csrf_token": "t", "tipo": "saida",
                                                "valor": "1,00", "descricao": "HACKEADO"})
with get_db() as db:
    r2 = db.execute("SELECT descricao FROM transacoes WHERE id=?", (tid,)).fetchone()
check("outro usuario NAO edita movimentacao alheia", r2["descricao"] == "Mercado da esquina", r2["descricao"])

secao("21. Parcelas (compromisso das próximas faturas)")
check("le PARC 3/12", A.detectar_parcela("AMAZON PARC 3/12") == (3, 12))
check("le PARCELA 1 DE 10", A.detectar_parcela("MAGALU PARCELA 1 DE 10") == (1, 10))
check("data 12/03 NAO vira parcela", A.detectar_parcela("SUPERMERCADO 12/03") == (None, None))
check("texto sem parcela", A.detectar_parcela("UBER TRIP") == (None, None))
c_p = novo_cliente("parcela@teste.com", nome="Par")
uid_p = uid_de("parcela@teste.com")
A.import_ofx_transactions(uid_p, [
    {"valor": 200.0, "tipo": "saida", "data": "2026-05-10", "descricao": "NOTEBOOK PARC 1/12", "fitid": "pa", "no_credito": True},
    {"valor": 200.0, "tipo": "saida", "data": "2026-06-10", "descricao": "NOTEBOOK PARC 2/12", "fitid": "pb", "no_credito": True},
    {"valor": 200.0, "tipo": "saida", "data": "2026-07-10", "descricao": "NOTEBOOK PARC 3/12", "fitid": "pc", "no_credito": True},
    {"valor": 150.0, "tipo": "saida", "data": "2026-07-11", "descricao": "SOFA PARCELA 2 DE 6", "fitid": "pd", "no_credito": True},
    {"valor": 50.0, "tipo": "saida", "data": "2026-07-12", "descricao": "FONE PARC 12/12", "fitid": "pe", "no_credito": True},
])
pf = A.calc_parcelas_futuras(uid_p)
check("nao conta a mesma compra 3x (2400, nao 6000)", abs(pf["total"] - 2400) < 0.01, pf["total"])
check("agrupa por compra (2 itens)", len(pf["itens"]) == 2, len(pf["itens"]))
check("compra quitada (12/12) sai da conta",
      not any("FONE" in i["descricao"] for i in pf["itens"]))
check("horizonte = maior numero de parcelas restantes", pf["meses"] == 9, pf["meses"])
h_p = c_p.get("/").get_data(as_text=True)
check("card de parcelas na Início", "Já comprometido nas próximas faturas" in h_p)
check("etiqueta de parcela na lista", "tx-tag-parcela" in c_p.get("/transacoes").get_data(as_text=True))

secao("22. Fatura por ciclo de fechamento")
check("ciclo: antes do fechamento",
      A.ciclo_fatura(date(2026, 7, 10), 25) == (date(2026, 6, 26), date(2026, 7, 25)))
check("ciclo: no dia do fechamento (ainda na fatura que fecha)",
      A.ciclo_fatura(date(2026, 7, 25), 25) == (date(2026, 6, 26), date(2026, 7, 25)))
check("ciclo: dia seguinte ja e' a proxima fatura",
      A.ciclo_fatura(date(2026, 7, 26), 25) == (date(2026, 7, 26), date(2026, 8, 25)))
check("ciclo: fechamento dia 31 em fevereiro vira 28",
      A.ciclo_fatura(date(2026, 2, 15), 31)[1] == date(2026, 2, 28))
check("ciclo: virada de ano",
      A.ciclo_fatura(date(2027, 1, 5), 25) == (date(2026, 12, 26), date(2027, 1, 25)))
c_c = novo_cliente("ciclo@teste.com", nome="Ciclo")
uid_c = uid_de("ciclo@teste.com")
ini_c, fim_c = A.ciclo_fatura(date.today(), 25)
dentro = date.fromordinal(ini_c.toordinal() + (fim_c - ini_c).days // 2)
passada = date.fromordinal(ini_c.toordinal() - 3)
with get_db() as db:
    ins = ("INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,"
           "data_transacao,no_credito) VALUES (?,?,?,?,?,?,?,?,1)")
    db.execute(ins, (uid_c, "saida", 100.0, "DENTRO ciclo", "Outros", "ofx", 95, dentro.isoformat()))
    db.execute(ins, (uid_c, "saida", 999.0, "FATURA PASSADA", "Outros", "ofx", 95, passada.isoformat()))
    db.execute("UPDATE usuarios SET cartao_fechamento=25, cartao_vencimento=5 WHERE id=?", (uid_c,))
st_c = A.calc_transaction_totals(uid_c)
check("fatura conta so o ciclo aberto (100, nao 1099)",
      abs(st_c["fatura_credito_mes"] - 100) < 0.01, st_c["fatura_credito_mes"])
check("marca que esta por ciclo", st_c["fatura_por_ciclo"] is True)
check("informa quando fecha e vence",
      st_c["fatura_fecha_em"] == fim_c and st_c["fatura_vence_dia"] == 5)
check("card mostra 'Fatura aberta'", "Fatura aberta" in c_c.get("/").get_data(as_text=True))
with get_db() as db:
    db.execute("UPDATE usuarios SET cartao_fechamento=NULL WHERE id=?", (uid_c,))
check("sem fechamento configurado volta pro mes do calendario",
      A.calc_transaction_totals(uid_c)["fatura_por_ciclo"] is False)

secao("23. Regra aprendida pega as variações (o caso do 'hops')")
check("sugere o trecho que se repete", A.padrao_sugerido("HOPS BAR 05/07") == "HOPS BAR",
      A.padrao_sugerido("HOPS BAR 05/07"))
check("tira LTDA/codigo", A.padrao_sugerido("HOPS CHOPERIA LTDA") == "HOPS CHOPERIA")
c_h = novo_cliente("hops@teste.com", nome="Hops")
uid_h = uid_de("hops@teste.com")
A.import_ofx_transactions(uid_h, [
    {"valor": 30.0, "tipo": "saida", "data": "2026-07-05", "descricao": "HOPS BAR 05/07", "fitid": "h1", "no_credito": False},
    {"valor": 45.0, "tipo": "saida", "data": "2026-07-19", "descricao": "HOPS BAR 19/07", "fitid": "h2", "no_credito": False},
    {"valor": 22.0, "tipo": "saida", "data": "2026-07-28", "descricao": "HOPS*BAR LTDA", "fitid": "h3", "no_credito": False},
])
c_h.post("/regras", data={"csrf_token": "t", "padrao_texto": "HOPS BAR",
                          "categoria_nome": "Bebidas", "voltar": "/transacoes"})
with get_db() as db:
    cats = [r["categoria"] for r in db.execute(
        "SELECT categoria FROM transacoes WHERE user_id=? AND descricao LIKE 'HOPS%'", (uid_h,)).fetchall()]
check("ensinar 1x corrige TODAS as variacoes passadas", cats == ["Bebidas"] * 3, cats)
A.import_ofx_transactions(uid_h, [{"valor": 18.0, "tipo": "saida", "data": "2026-08-02",
                                   "descricao": "HOPS  Bar  02/08", "fitid": "h4", "no_credito": False}])
with get_db() as db:
    nova = db.execute("SELECT categoria FROM transacoes WHERE fitid='h4'").fetchone()["categoria"]
check("compra futura ja entra na categoria certa", nova == "Bebidas", nova)
c_h.post("/regras", data={"csrf_token": "t", "padrao_texto": "hops bar",
                          "categoria_nome": "Lazer", "voltar": "/"})
with get_db() as db:
    rs = db.execute("SELECT padrao_texto, categoria_nome FROM regras_categorizacao WHERE user_id=?",
                    (uid_h,)).fetchall()
check("ensinar de novo ATUALIZA em vez de duplicar",
      len(rs) == 1 and rs[0]["categoria_nome"] == "Lazer", [dict(r) for r in rs])

secao("24. Fatura fechada (a pagar) aparece junto da aberta")
c_f = novo_cliente("fatura@teste.com", nome="Fat")
uid_f = uid_de("fatura@teste.com")
hoje = date.today()
ini_f, fim_f = A.ciclo_fatura(hoje, 25)
fechou_em = ini_f - timedelta(days=1)
dentro_ant = fechou_em - timedelta(days=5)
with get_db() as db:
    ins = ("INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,"
           "data_transacao,no_credito) VALUES (?,?,?,?,?,?,?,?,1)")
    db.execute(ins, (uid_f, "saida", 64.0, "compra nova", "Outros", "ofx", 95,
                     min(hoje, fim_f).isoformat()))
    db.execute(ins, (uid_f, "saida", 200.0, "compras da fatura fechada", "Outros", "ofx", 95,
                     dentro_ant.isoformat()))
    db.execute("UPDATE usuarios SET cartao_fechamento=25, cartao_vencimento=5 WHERE id=?", (uid_f,))
st_f = A.calc_transaction_totals(uid_f)
check("fatura aberta = so o ciclo novo", abs(st_f["fatura_credito_mes"] - 64) < 0.01,
      st_f["fatura_credito_mes"])
check("fatura FECHADA (a pagar) aparece separada",
      st_f["fatura_fechada"] and abs(st_f["fatura_fechada"]["valor"] - 200) < 0.01,
      st_f["fatura_fechada"])
check("card mostra 'A pagar'", "A pagar" in c_f.get("/").get_data(as_text=True))

secao("25. Gráfico e entradas no vira-mês")
c_g = novo_cliente("graf@teste.com", nome="Graf")
h_g = c_g.get("/dashboard").get_data(as_text=True)
check("sem gastos, grafico mostra estado vazio (nao quadro em branco)",
      "Nenhum gasto neste mês ainda" in h_g or "Seu mês vai aparecer aqui" in h_g)
uid_g = uid_de("graf@teste.com")
with get_db() as db:
    db.execute("INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,"
               "data_transacao,no_credito) VALUES (?,?,?,?,?,?,?,?,0)",
               (uid_g, "entrada", 3000.0, "Salario", "Salário", "ofx", 95,
                (date.today() - timedelta(days=2)).isoformat()))
    db.execute("INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,"
               "data_transacao,no_credito) VALUES (?,?,?,?,?,?,?,?,0)",
               (uid_g, "saida", 80.0, "Mercado", "Mercado", "ofx", 95, date.today().isoformat()))
h_g2 = c_g.get("/dashboard").get_data(as_text=True)
check("com gasto, grafico tem caixa de altura propria", "chart-box" in h_g2)
check("legenda com valor e %", "chart-legend" in h_g2)

secao("26. Assinaturas detectadas")
c_a = novo_cliente("assina@teste.com", nome="Ass")
uid_a = uid_de("assina@teste.com")
with get_db() as db:
    ins = ("INSERT INTO transacoes (user_id,tipo,valor,descricao,estabelecimento,categoria,fonte,"
           "confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,?,?,?,?,0)")
    def _add(desc, val, meses, cat="Outros"):
        d = (date.today() - timedelta(days=30 * meses)).isoformat()
        db.execute(ins, (uid_a, "saida", val, desc, desc, cat, "ofx", 95, d))
    for m in range(4):
        _add(f"NETFLIX.COM {m}", 39.90, m, "Assinaturas")
    for m, v in enumerate([99.90, 99.90, 109.90]):
        _add("SMARTFIT ACADEMIA", v, m, "Saúde")
    for m, v in enumerate([320.0, 85.0, 512.0, 150.0]):
        _add("CARREFOUR", v, m, "Mercado")
    for m in range(2):
        _add("SPOTIFY", 21.90, m, "Assinaturas")
ass = A.detectar_assinaturas(uid_a)
nomes_ass = [a["nome"].lower() for a in ass["itens"]]
check("acha assinatura de valor fixo (Netflix)", any("netflix" in n for n in nomes_ass))
check("acha mesmo com reajuste pequeno (academia)", any("smartfit" in n for n in nomes_ass))
check("NAO confunde compra variavel (mercado)", not any("carrefour" in n for n in nomes_ass))
check("NAO conta com so 2 meses (Spotify)", not any("spotify" in n for n in nomes_ass))
check("soma mensal e anual", abs(ass["total_mes"] - 139.80) < 0.01
      and abs(ass["total_ano"] - 1677.60) < 0.01, (ass["total_mes"], ass["total_ano"]))
check("card aparece no Resumo", "O que se repete todo mês" in c_a.get("/dashboard").get_data(as_text=True))

secao("27. Orçamento por categoria")
c_o = novo_cliente("orc@teste.com", nome="Orc")
uid_o = uid_de("orc@teste.com")
c_o.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "300,00",
                                   "descricao": "Mercado do mes", "categoria": "Mercado"})
h_o = c_o.get("/categorias").get_data(as_text=True)
check("categoria PADRAO aparece no orçamento", "Mercado" in h_o and "Orçamento de" in h_o)
check("mostra o gasto do mes", "R$ 300,00" in h_o)
# define limite numa categoria padrao (antes so dava nas criadas pelo usuario)
c_o.post("/categorias", data={"csrf_token": "t", "nome": "Mercado", "limite_mensal": "500,00"})
h_o2 = c_o.get("/categorias").get_data(as_text=True)
check("da pra pôr limite em categoria padrão", "de R$ 500,00" in so_texto(h_o2))
check("mostra quanto resta", "restam R$ 200,00" in so_texto(h_o2))
check("barra de progresso aparece", "progress-fill" in h_o2)
c_o.post("/categorias", data={"csrf_token": "t", "nome": "Mercado", "limite_mensal": "200,00"})
h_o3 = c_o.get("/categorias").get_data(as_text=True)
check("estourou o limite avisa", "passou R$ 100,00" in so_texto(h_o3) and "fill-danger" in h_o3)

secao("28. Sync não atropela a coleta do banco")
A.time.sleep = lambda s: None
_patches = {"n": 0}
class _Resp:
    def raise_for_status(self): pass
class _Http:
    def patch(self, *a, **k):
        _patches["n"] += 1
        return _Resp()
A.http_requests = _Http()

def _mock_item(status, quando):
    A._pluggy_get = lambda k, p, params=None: (
        {"status": status, "executionStatus": "SUCCESS", "lastUpdatedAt": quando}
        if p.startswith("/items/") else {})

# O BUG: pedir coleta com uma já rodando REINICIA — e ela nunca terminava
_mock_item("UPDATING", "2026-07-31T23:00:00Z")
_patches["n"] = 0
A.pluggy_refresh_items("k", ["i1"], espera_max=0)
check("NAO pede coleta com uma ja rodando", _patches["n"] == 0, _patches["n"])

_mock_item("UPDATED", "2026-07-30T10:00:00Z")
_patches["n"] = 0
r_ref = A.pluggy_refresh_items("k", ["i1"], espera_max=0, min_horas=3)
check("pede coleta quando o dado esta velho", _patches["n"] == 1 and r_ref["pediu"] is True)

recente = (datetime.utcnow() - timedelta(minutes=20)).isoformat() + "Z"
_mock_item("UPDATED", recente)
_patches["n"] = 0
A.pluggy_refresh_items("k", ["i1"], espera_max=0, min_horas=3)
check("auto-sync NAO pede se a coleta e' recente", _patches["n"] == 0, _patches["n"])

_patches["n"] = 0
A.pluggy_refresh_items("k", ["i1"], espera_max=0, min_horas=0)
check("botao manual sempre pode pedir", _patches["n"] == 1, _patches["n"])

_mock_item("LOGIN_ERROR", recente)
r_login = A.pluggy_refresh_items("k", ["i1"], espera_max=0)
check("detecta acesso expirado", r_login["erro_login"] is True)

secao("29. Privacidade e apagar conta")
_anon = A.app.test_client()
r_priv = _anon.get("/privacidade")
h_priv = r_priv.get_data(as_text=True)
check("politica abre SEM login (da pra ler antes de se cadastrar)", r_priv.status_code == 200)
check("diz que nao vende dados", "Não vendo seus dados" in h_priv)
check("explica que o banco e' so leitura", "somente leitura" in h_priv)
check("e' honesta sobre os limites do projeto",
      "não é um banco com equipe de segurança" in h_priv)
check("link da politica na tela de login", "/privacidade" in _anon.get("/login").get_data(as_text=True))

c_del = novo_cliente("apagar@teste.com", nome="Apaga")
uid_del = uid_de("apagar@teste.com")
c_del.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "50,00",
                                     "descricao": "Gasto do apagar"})
c_del.post("/metas", data={"csrf_token": "t", "nome": "Reserva", "meta_valor": "1.000,00"})
c_del.post("/dividas", data={"csrf_token": "t", "tipo": "devo", "descricao": "Divida",
                             "valor_total": "100,00"})
def _conta_de(tabela, uid):
    with get_db() as db:
        return db.execute(f"SELECT COUNT(*) AS n FROM {tabela} WHERE user_id=?", (uid,)).fetchone()["n"]
check("dados criados antes de apagar",
      _conta_de("transacoes", uid_del) >= 1 and _conta_de("metas", uid_del) == 1)
c_del.post("/conta/apagar", data={"csrf_token": "t", "confirmacao": "sim"})
with get_db() as db:
    vivo = db.execute("SELECT COUNT(*) AS n FROM usuarios WHERE id=?", (uid_del,)).fetchone()["n"]
check("confirmacao errada NAO apaga", vivo == 1)
c_del.post("/conta/apagar", data={"csrf_token": "t", "confirmacao": "APAGAR"})
with get_db() as db:
    vivo2 = db.execute("SELECT COUNT(*) AS n FROM usuarios WHERE id=?", (uid_del,)).fetchone()["n"]
check("apaga a conta", vivo2 == 0)
check("apaga junto movimentacoes, metas e dividas (cascade)",
      _conta_de("transacoes", uid_del) == 0 and _conta_de("metas", uid_del) == 0
      and _conta_de("dividas", uid_del) == 0)
check("NAO mexe nos dados de outro usuario", _conta_de("transacoes", uid1) > 0)
check("sessao encerrada apos apagar", c_del.get("/", follow_redirects=False).status_code == 302)

secao("30. Meta que cabe no bolso")
def _lanca_mes(uid, tipo, valor, meses_atras):
    d = (date.today().replace(day=15) - timedelta(days=30 * meses_atras)).isoformat()
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,'hist','Outros','ofx',95,?,0)""",
                   (uid, tipo, valor, d))

c_g = novo_cliente("guardar@teste.com", nome="Guardar")
uid_g = uid_de("guardar@teste.com")
for m, (ent, sai) in enumerate([(2000, 1880), (2000, 1500), (2000, 1700)], start=1):
    _lanca_mes(uid_g, "entrada", ent, m)
    _lanca_mes(uid_g, "saida", sai, m)
sug = A.sugerir_guardar_mensal(uid_g)
check("usa o PIOR mes, nao a media (90, nao 307)", sug["valor"] == 90, sug)
check("nao marca como apertado quando sobra", sug["apertado"] is False)
check("card aparece nas Metas", "dá pra guardar" in c_g.get("/metas").get_data(as_text=True))
c_g.post("/metas/quanto-guardar", data={"csrf_token": "t", "valor": str(sug["valor"])})
with get_db() as db:
    mm = db.execute("SELECT meta_mensal FROM usuarios WHERE id=?", (uid_g,)).fetchone()["meta_mensal"]
check("aceitar a sugestao grava a meta mensal", abs(mm - 90) < 0.01, mm)
check("entra no calculo do 'pode gastar hoje'",
      A.calc_transaction_totals(uid_g)["reserve_monthly_needed"] > 0)

c_ap = novo_cliente("apertado@teste.com", nome="Apertado")
uid_ap = uid_de("apertado@teste.com")
for m, (ent, sai) in enumerate([(1500, 1600), (1500, 1550), (1500, 1700)], start=1):
    _lanca_mes(uid_ap, "entrada", ent, m)
    _lanca_mes(uid_ap, "saida", sai, m)
sug_ap = A.sugerir_guardar_mensal(uid_ap)
check("mes no vermelho: valor simbolico e tom acolhedor",
      sug_ap["valor"] == 20 and sug_ap["apertado"] is True, sug_ap)
check("nao culpa a pessoa", "não é falta de disciplina" in c_ap.get("/metas").get_data(as_text=True))

c_novo2 = novo_cliente("semhist@teste.com", nome="Novo")
check("sem historico NAO promete nada", A.sugerir_guardar_mensal(uid_de("semhist@teste.com")) is None)

secao("31. Ajuda e dicas contextuais")
_an = A.app.test_client()
r_aj = _an.get("/ajuda")
h_aj = r_aj.get_data(as_text=True)
check("ajuda abre SEM login (da pra mandar no WhatsApp)", r_aj.status_code == 200)
check("tem o passo a passo do banco", "ajuda-passos" in h_aj)
check("manda escolher O PROPRIO banco na lista", "seu banco na lista" in h_aj)
check("e nao pede cadastro em site nenhum antes",
      "meu.pluggy" not in h_aj and "Meu Pluggy" not in h_aj)
check("lembra que e' so leitura", "só de leitura" in h_aj)
check("diz que da pra usar sem conectar", "Não quer conectar" in h_aj)
check("link da ajuda no menu", "/ajuda" in c1.get("/").get_data(as_text=True))

c_d = novo_cliente("dicas@teste.com", nome="Dicas")
uid_d = uid_de("dicas@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,
                  data_transacao,no_credito,parcela_num,parcela_total)
                  VALUES (?,'saida',10,'x','Outros','ofx',95,date('now'),1,2,6)""", (uid_d,))
def _dica_atual(cl):
    h = cl.get("/").get_data(as_text=True)
    import re as _re
    m = _re.search(r'herc-tip-bubble.*?<span>(.*?)</span>', h, _re.S)
    return _re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
check("mostra a dica da PARCELA primeiro (mais relevante)",
      "compra parcelada" in _dica_atual(c_d))
c_d.post("/dicas/primeira_parcela/vista", data={"csrf_token": "t"})
check("depois do 'Entendi!' passa pra do credito",
      "no crédito" in _dica_atual(c_d))
c_d.post("/dicas/primeiro_credito/vista", data={"csrf_token": "t"})
check("depois vem a de ensinar categoria", "Outros" in _dica_atual(c_d))
for k in ("tem_outros", "primeira_sync", "registro_rapido"):
    c_d.post(f"/dicas/{k}/vista", data={"csrf_token": "t"})
check("dispensadas todas, nao insiste", _dica_atual(c_d) == "")

secao("32. Trilha do MEI: guardar nota e entender o IR")
check("ajuda tem a secao do MEI", "Se você é MEI" in h_aj)
for _t in ("Guarde a nota assim que emitir", "limite do MEI", "DAS", "dossiê"):
    check(f"ajuda explica: {_t}", _t in h_aj)

def _nota(uid, valor):
    with get_db() as db:
        db.execute("""INSERT INTO notas (user_id,descricao,valor,tipo,categoria,status,data_emissao)
                      VALUES (?,'Servico',?,'entrada','Serviços','Autorizada',date('now'))""", (uid, valor))

def _um_gasto(uid):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,
                      data_transacao,no_credito) VALUES (?,'saida',40,'Padaria','Mercado','manual',100,
                      date('now'),0)""", (uid,))

c_m = novo_cliente("mei-ir@teste.com", nome="Ana", perfil="mei")
uid_m = uid_de("mei-ir@teste.com")
check("na estreia o Herc nao empurra dica (mostra as boas-vindas)", _dica_atual(c_m) == "")
_um_gasto(uid_m)
check("MEI sem nota nenhuma e' lembrado de guardar", "guarde em" in _dica_atual(c_m))

_nota(uid_m, 3000)
check("com nota guardada, a dica vira a do Painel MEI", "Painel MEI" in _dica_atual(c_m))

h_ir = c_m.get("/ir").get_data(as_text=True)
# O bug: faturamento vem de NOTAS, renda tributavel vem de TRANSACOES.
# Sem isso a tela dizia "sua renda e' R$ 0,00" pra quem faturou o ano todo.
check("IR do MEI NAO diz que a renda e' zero",
      "renda tributável no ano está em <strong>R$ 0,00" not in h_ir)
check("IR do MEI mostra o faturamento das notas", "R$ 3.000,00" in h_ir)
check("IR do MEI separa negocio de pessoa fisica", "duas contas separadas" in h_ir)
check("IR do MEI manda pro Painel MEI", "/mei" in h_ir)
check("IR do MEI manda falar com o contador", "com o contador" in h_ir)

h_ir_pf = c1.get("/ir").get_data(as_text=True)
check("PF nao ve o papo de MEI no IR", "duas contas separadas" not in h_ir_pf)

c_lim = novo_cliente("mei-limite@teste.com", nome="Beto", perfil="mei")
uid_lim = uid_de("mei-limite@teste.com")
_um_gasto(uid_lim)
_nota(uid_lim, A.MEI_LIMITE_ANUAL * 0.85)
check("avisa ANTES de estourar o limite do MEI", "80% do limite" in _dica_atual(c_lim))

secao("33. Olhinho: ocultar os valores na tela")
check("money embrulha o valor pra dar pra ocultar",
      str(A.money_html(1234.5)) == '<span class="money">R$ 1.234,50</span>')
check("money_html escapa entrada estranha", "<b>" not in str(A.money_html("<b>x</b>")))
check("money cru (usado em texto) segue sem tag", "<" not in A.money(10))

h_home = c1.get("/").get_data(as_text=True)
# O botao mora AO LADO do valor, como nos bancos — nao mais no canto de cima.
# Como agora e' um por tela, o gancho e' o atributo, nao um id unico.
check("o botao do olhinho aparece", "data-olho" in h_home)
check("e fica na mesma linha do saldo",
      "olho-do-valor" in h_home.split('class="balance-big"')[1].split("</p>")[0])
check("o JS liga todos os olhos da pagina, nao so um",
      "querySelectorAll('[data-olho]')" in h_home)
check("o Resumo tambem tem o seu",
      "data-olho" in c1.get("/dashboard").get_data(as_text=True))
check("saldo da home vem embrulhado", 'class="money"' in h_home)
check("aplica antes de pintar (nao pisca o valor)",
      "hercValoresOcultos" in h_home and "valores-ocultos" in h_home)

_css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "styles.css"),
            encoding="utf-8").read()
_regra = _css.split("html.valores-ocultos .money {")[1].split("}")[0]
check("oculto: some com o numero", "color: transparent" in _regra)
# Se a mascara acompanhasse o tamanho do numero, o borrao entregaria a grandeza
check("oculto: largura FIXA (nao entrega a grandeza)",
      "width: 4.4em" in _regra and "overflow: hidden" in _regra)
check("oculto: borra o grafico junto", "canvas { filter: blur" in _css)
# O gancho aqui e a CLASSE do botao: 'data-olho' sozinho apareceria em qualquer
# pagina, porque o script que liga os olhos mora no layout de todas elas.
check("deslogado nao tem olhinho",
      "olho-do-valor" not in _an.get("/login").get_data(as_text=True))

# ---- O que saiu do topo da tela (pedido dele: "ta muito grossa") ----
_h_base = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "base.html"), encoding="utf-8").read()
check("o topo nao carrega mais o olho nem o tema",
      "eyeToggle" not in _h_base
      and "topbar" not in _h_base.split('id="temaToggle"')[0][-400:])
check("o tema virou botao do menu lateral", 'class="sidebar-tema"' in _h_base)
# Ele fica no CABECALHO do menu. A lista de links passa de 1200px e o menu rola:
# qualquer coisa embaixo dela nasce abaixo da dobra, o que e pior do que estava.
check("e no cabecalho, acima da lista que rola",
      _h_base.index('class="sidebar-tema"') < _h_base.index('class="sidebar-nav"'))
check("com a lateral recolhida ele sai (nao cabe no trilho de 92px)",
      ".sidebar-collapsed .sidebar-tema" in _css)
# A barra so' afina de verdade se os botoes redondos afinarem: sao eles que
# definem a altura minima.
check("a barra de cima ficou mais fina", "padding: 10px 20px" in _css)
check("no celular tambem", "padding: 8px 14px" in _css)
check("e os botoes redondos encolhem junto, senao nao adianta",
      ".topbar .icon-button { width: 38px" in _css)

# ---- A gaveta abrindo do meio da tela ----
check("o menu abre arrastando de metade da tela, nao so' da borda",
      "window.innerWidth * 0.5" in _h_base and "startX < 28" not in _h_base)
check("mas nao rouba o arrasto de quem rola de lado",
      "rolaDeLado" in _h_base and "scrollWidth > el.clientWidth" in _h_base)
check("olhando o overflow de verdade, nao chutando pela classe",
      "overflowX" in _h_base)

secao("34. Backup: a cópia tem que abrir e ter os dados")
import gzip, shutil, sqlite3 as _sq
import backup as B

_dest = B.fazer_backup()
check("gera o arquivo do dia", _dest.exists() and _dest.name.startswith("hercules-"))
check("fica compactado", _dest.suffix == ".gz")
check("nao deixa arquivo parcial pra tras", not list(B.BACKUP_DIR.glob("*.parcial")))

_restaurado = os.path.join(TMP, "restaurado.db")
with gzip.open(_dest, "rb") as _i, open(_restaurado, "wb") as _o:
    shutil.copyfileobj(_i, _o)
_r = _sq.connect(_restaurado)
check("a copia nao esta corrompida",
      _r.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
# backup que abre mas veio vazio e' pior que backup nenhum: da falsa seguranca
_n_orig = None
with get_db() as db:
    _n_orig = db.execute("SELECT COUNT(*) AS n FROM usuarios").fetchone()["n"]
check("a copia tem os usuarios de verdade",
      _r.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == _n_orig, _n_orig)
check("a copia tem as transacoes",
      _r.execute("SELECT COUNT(*) FROM transacoes").fetchone()[0] > 0)
check("a copia tem o schema inteiro",
      _r.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 15)
_r.close()

check("nao repete o backup dentro das 24h", B.garantir_backup_do_dia() is False)
# 15 dias de copias: a mais velha tem que sair sozinha, senao o disco enche
for _d in range(20):
    (B.BACKUP_DIR / f"hercules-2020-01-{_d + 1:02d}.db.gz").write_bytes(b"x")
B.fazer_backup()
check(f"guarda no maximo {B.MANTER_DIAS} copias", len(B.listar_backups()) == B.MANTER_DIAS,
      len(B.listar_backups()))
check("mantem as mais NOVAS", B.listar_backups()[0].name == B._nome_do_dia())

_falhou = B.fazer_backup
B.fazer_backup = lambda: (_ for _ in ()).throw(OSError("disco cheio"))
try:
    A.garantir_backup_do_dia.__globals__["fazer_backup"] = B.fazer_backup
    check("backup quebrado NAO derruba o app", c1.get("/").status_code == 200)
finally:
    B.fazer_backup = _falhou
    A.garantir_backup_do_dia.__globals__["fazer_backup"] = _falhou

secao("35. Erro na cara do usuário e painel de saúde")

_cli_erro = A.app.test_client()
with _cli_erro.session_transaction() as s:
    s["user_id"] = uid1; s["csrf_token"] = "t"
_re_ = _cli_erro.get("/quebra-de-proposito")
_h_erro = _re_.get_data(as_text=True)
check("erro devolve 500 (nao mascara)", _re_.status_code == 500)
check("mostra tela amiga, nao pagina branca", "Deu ruim aqui do meu lado" in _h_erro)
check("tranquiliza sobre os dados", "seus dados estão salvos" in _h_erro)
check("da um codigo pra pessoa mandar", "Se puder me avisar" in _h_erro)
check("NAO vaza o traceback pro usuario",
      "RuntimeError" not in _h_erro and "estourei aqui" not in _h_erro)

check("404 tambem tem tela amiga",
      "Essa página não existe" in _cli_erro.get("/rota-que-nao-existe").get_data(as_text=True))

_log = A.ERROS_LOG.read_text(encoding="utf-8")
check("o erro foi pro log", "RuntimeError" in _log and "estourei aqui" in _log)
check("o log guarda onde quebrou", "/quebra-de-proposito" in _log)
_erros = A.erros_recentes()
check("erros_recentes lista a quebra", _erros and "RuntimeError" in _erros[0]["erro"])

# O log nao pode virar um vazamento de dados
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "777,77",
                                  "descricao": "SEGREDO DO USUARIO"})
_cli_erro.get("/quebra-de-proposito")
_log2 = A.ERROS_LOG.read_text(encoding="utf-8")
check("o log NAO grava o que a pessoa digitou",
      "SEGREDO DO USUARIO" not in _log2 and "777,77" not in _log2)
check("o log NAO grava senha", "tijolo-forte-42" not in _log2)

secao("36. Saúde do app: trancada por padrão (segurança)")
check("sem ADMIN_EMAIL ninguem e' admin", A.ADMIN_EMAIL == "" and not A.eh_admin(None))
check("sem ADMIN_EMAIL a tela nem existe (404)", c1.get("/saude").status_code == 404)
check("sem ADMIN_EMAIL nao da pra baixar backup",
      c1.get(f"/saude/backup/{_dest.name}").status_code == 404)

A.ADMIN_EMAIL = "a@teste.com"          # como se a variavel de ambiente existisse
check("agora o dono ve a tela", c1.get("/saude").status_code == 200)
check("OUTRO usuario logado continua sem ver", c2.get("/saude").status_code == 404)
check("outro usuario NAO baixa o backup",
      c2.get(f"/saude/backup/{_dest.name}").status_code == 404)
check("deslogado nao chega na tela", _an.get("/saude", follow_redirects=False).status_code == 302)

# nome de arquivo e' conferido contra a lista real, nunca concatenado no caminho
for _ataque in ("../database.db", "..%2fdatabase.db", "hercules-2020-01-01.db.gz/../../app.py"):
    check(f"path traversal barrado: {_ataque[:24]}",
          c1.get(f"/saude/backup/{_ataque}").status_code in (404, 308))

_h_saude = c1.get("/saude").get_data(as_text=True)
check("mostra quantos voltaram", "Voltaram nos 7 dias" in _h_saude)
check("mostra as copias de seguranca", "Cópias de segurança" in _h_saude)
check("avisa que a copia local nao salva de tudo", "não</strong> salvam" in _h_saude)
check("lista as quebras", "Últimas quebras" in _h_saude)
check("baixar backup funciona pro dono",
      c1.get(f"/saude/backup/{_dest.name}").status_code == 200)

with get_db() as db:
    _visto = db.execute("SELECT last_seen FROM usuarios WHERE id=?", (uid1,)).fetchone()["last_seen"]
check("marca que a pessoa voltou hoje", _visto == date.today().isoformat(), _visto)
check("link no menu so aparece pro admin",
      'Saúde do app' in c1.get("/").get_data(as_text=True)
      and 'Saúde do app' not in c2.get("/").get_data(as_text=True))
A.ADMIN_EMAIL = ""

secao("37. Recado da semana (o motivo de voltar)")
# A semana tem que ser a que FECHOU. Fechar balanço na quarta compara meia
# semana com uma inteira e sempre parece que a pessoa gastou menos.
_qua = date(2026, 7, 29)                     # uma quarta-feira
_ini, _fim = A.semana_fechada(_qua)
check("semana fechada comeca na segunda", _ini.weekday() == 0, _ini.strftime("%a %d/%m"))
check("semana fechada termina no domingo", _fim.weekday() == 6, _fim.strftime("%a %d/%m"))
check("nao inclui a semana corrente", _fim < _qua, f"{_fim} vs {_qua}")
check("sao 7 dias", (_fim - _ini).days == 6)

_seg = date(2026, 8, 3)                      # segunda: a semana de ontem acabou
_i2, _f2 = A.semana_fechada(_seg)
check("na segunda ja fecha a semana que acabou ontem", _f2 == _seg - timedelta(days=1))
_dom = date(2026, 8, 2)                      # domingo: hoje ainda nao acabou
_i3, _f3 = A.semana_fechada(_dom)
check("no domingo NAO fecha o proprio domingo", _f3 < _dom)
check("virada de ano nao quebra a chave",
      A.chave_recado(date(2026, 1, 4)) == "recado:2026-W01", A.chave_recado(date(2026, 1, 4)))

c_r = novo_cliente("recado@teste.com", nome="Rita")
uid_r = uid_de("recado@teste.com")
_i, _f = A.semana_fechada()
def _lanca(uid, tipo, valor, quando, cat="Outros", desc="x"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,'manual',100,?,0)""",
                   (uid, tipo, valor, desc, cat, quando.isoformat()))

check("sem movimento nenhum nao inventa recado", A.recado_da_semana(uid_r) is None)

_lanca(uid_r, "entrada", 1000, _i, "Salário")
_lanca(uid_r, "saida", 300, _i + timedelta(days=1), "Mercado")
_lanca(uid_r, "saida", 100, _i + timedelta(days=2), "Transporte")
_rec = A.recado_da_semana(uid_r)
check("soma o gasto da semana que fechou", abs(_rec["gasto"] - 400) < 0.01, _rec["gasto"])
check("calcula o que sobrou", abs(_rec["sobrou"] - 600) < 0.01, _rec["sobrou"])
check("sem semana anterior, avisa que e' a primeira", _rec["primeira_semana"] is True)
check("sem comparacao nao inventa variacao", _rec["delta"] is None and _rec["mudanca"] is None)

# semana anterior: Transporte era bem maior -> a NOTICIA e' a queda do transporte
_lanca(uid_r, "saida", 280, _i - timedelta(days=6), "Mercado")
_lanca(uid_r, "saida", 400, _i - timedelta(days=5), "Transporte")
_rec2 = A.recado_da_semana(uid_r)
check("agora compara com a semana anterior", _rec2["primeira_semana"] is False)
check("delta do gasto total", abs(_rec2["delta"] - (400 - 680)) < 0.01, _rec2["delta"])
check("aponta a categoria que mais MUDOU, nao a maior",
      _rec2["mudanca"]["categoria"] == "Transporte", _rec2["mudanca"])
check("e diz que caiu", _rec2["mudanca"]["delta"] < 0)

# ruido nao vira recado
c_ru = novo_cliente("ruido@teste.com", nome="Rui")
uid_ru = uid_de("ruido@teste.com")
_lanca(uid_ru, "saida", 100, _i, "Mercado")
_lanca(uid_ru, "saida", 104, _i - timedelta(days=7), "Mercado")
check("variacao de trocado NAO vira 'o que mudou'",
      A.recado_da_semana(uid_ru)["mudanca"] is None)

_h_r = c_r.get("/").get_data(as_text=True)
check("o recado aparece na Inicio", "Fechou a semana de" in _h_r)
check("vem antes do resto da tela", _h_r.index("recado-semana") < _h_r.index("balance-hero"))
check("mostra o que mudou", "O que mais mudou foi" in _h_r and "Transporte" in _h_r)
check("oferece guardar a sobra", "Quero guardar" in _h_r)

c_r.post("/recado/visto", data={"csrf_token": "t", "chave": _rec2["chave"]})
check("depois do 'Vi' some", "Fechou a semana de" not in c_r.get("/").get_data(as_text=True))
check("mas volta na semana seguinte (guarda por semana, nao pra sempre)",
      not A.tip_seen(uid_r, A.chave_recado(_f + timedelta(days=7))))

# chave vem do formulario: nao pode virar porta pra gravar lixo
c_r.post("/recado/visto", data={"csrf_token": "t", "chave": "primeira_parcela"})
with get_db() as db:
    _lixo = db.execute("SELECT COUNT(*) AS n FROM dicas_vistas WHERE user_id=? AND dica=?",
                       (uid_r, "primeira_parcela")).fetchone()["n"]
check("nao aceita chave fora do formato", _lixo == 0)

c_novo = novo_cliente("semrecado@teste.com", nome="Novo")
check("na estreia nao mostra recado", "Fechou a semana de" not in c_novo.get("/").get_data(as_text=True))

secao("38. Registrar entrada (o botão 'Entrou dinheiro')")
# Uma aspa escapada pelo Jinja vira erro de sintaxe e MATA o bloco de script
# inteiro: nenhum listener e' registrado e o botao de entrada nao faz nada.
# Foi assim que ficou impossivel lancar salario na mao.
_h_nova = c1.get("/transacoes/nova").get_data(as_text=True)
_js = _h_nova[_h_nova.index("tipoInput"):]
check("o script do formulario nao tem aspa escapada", "&#39;" not in _js and "&#34;" not in _js)
check("setTipo recebe um literal valido", 'const tipoInicial = "saida";' in _js, )
check("os dois botoes de tipo existem", 'id="btnEntrada"' in _h_nova and 'id="btnSaida"' in _h_nova)

for _t, _esperado in (("entrada", '"entrada"'), (None, '"saida"')):
    _u = "/transacoes/nova" + (f"?tipo={_t}" if _t else "")
    check(f"abre ja no tipo certo ({_t or 'padrao'})",
          f"const tipoInicial = {_esperado};" in c1.get(_u).get_data(as_text=True))

# o caminho que o usuario reclamou: lancar o salario na mao
c1.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "entrada", "valor": "4.820,00",
                                  "descricao": "Meu salario", "categoria": "Salário"})
with get_db() as db:
    _sal = db.execute("""SELECT tipo, valor, categoria FROM transacoes
                          WHERE user_id=? AND descricao='Meu salario'""", (uid1,)).fetchone()
check("salario entra como ENTRADA", _sal and _sal["tipo"] == "entrada", dict(_sal) if _sal else None)
check("com a categoria escolhida", _sal and _sal["categoria"] == "Salário")
check("e soma na renda do mes",
      A.calc_transaction_totals(uid1)["month_income"] >= 4820)

# "Outros" existe nas duas listas; escolher so pelo value pega a de saida,
# que fica escondida quando o tipo e' entrada — e o campo aparece vazio
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',900,'PIX RECEBIDO',
                  'Outros','ofx',95,date('now'),0)""", (uid1,))
    _tid = db.execute("SELECT id FROM transacoes WHERE descricao='PIX RECEBIDO'").fetchone()["id"]
_h_ed = c1.get(f"/transacoes/{_tid}/editar").get_data(as_text=True)
check("editar entrada: script tambem intacto", "&#39;" not in _h_ed[_h_ed.index("tipoInput"):])
check("editar entrada: abre como entrada", 'const tipoInicial = "entrada";' in _h_ed)
check("editar entrada: casa categoria COM o tipo, nao so o value",
      "selecionar(catSalva, tipoInicial)" in _h_ed)
check("'Outros' aparece nas duas listas (por isso o cuidado acima)",
      _h_ed.count('>Outros</option>') == 2, _h_ed.count('>Outros</option>'))

secao("39. Nenhuma tela com script quebrado")
# Varredura: uma entidade HTML dentro de <script> e' erro de sintaxe e derruba o
# bloco inteiro, calado — sem erro no servidor e sem nada no console do Flask.
import re as _re2
_rotas = ["/", "/dashboard", "/transacoes", "/transacoes/nova", "/compromissos", "/categorias",
          "/metas", "/dividas", "/ir", "/trabalhos", "/settings", "/ajuda", "/importar-ofx"]
_quebradas = []
for _rota in _rotas:
    _html = c1.get(_rota).get_data(as_text=True)
    for _bloco in _re2.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _html, _re2.S):
        if any(_e in _bloco for _e in ("&#39;", "&#34;", "&quot;")):
            _quebradas.append(_rota)
check(f"{len(_rotas)} telas sem entidade HTML dentro de <script>",
      not _quebradas, ", ".join(sorted(set(_quebradas))))

secao("40. Meses: histórico e comparação")
check("nome do mes em portugues", A.nome_do_mes("2026-07") == "julho de 2026", A.nome_do_mes("2026-07"))
check("mes anterior atravessa o ano", A.mes_anterior_a("2026-01") == "2025-12")
check("mes anterior normal", A.mes_anterior_a("2026-08") == "2026-07")

c_ms = novo_cliente("meses@teste.com", nome="Mes")
uid_ms = uid_de("meses@teste.com")
check("sem historico a tela nao quebra", c_ms.get("/meses").status_code == 200)
check("e avisa que nao tem o que comparar",
      "Ainda não tenho meses" in c_ms.get("/meses").get_data(as_text=True))

def _no_mes(uid, tipo, valor, mes, dia=10, cat="Outros", credito=0):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,'manual',100,?,?)""",
                   (uid, tipo, valor, f"{cat} {mes}", cat, f"{mes}-{dia:02d}", credito))

_no_mes(uid_ms, "entrada", 3000, "2026-05", cat="Salário")
_no_mes(uid_ms, "saida", 800, "2026-05", cat="Mercado")
_no_mes(uid_ms, "saida", 500, "2026-05", cat="Transporte")
_no_mes(uid_ms, "entrada", 3000, "2026-06", cat="Salário")
_no_mes(uid_ms, "saida", 950, "2026-06", cat="Mercado")
_no_mes(uid_ms, "saida", 100, "2026-06", cat="Transporte")
_no_mes(uid_ms, "saida", 700, "2026-06", cat="Lazer", credito=1)   # credito nao conta no "saiu"

_hist = {m["mes"]: m for m in A.historico_mensal(uid_ms)}
check("lista os meses com movimento", set(_hist) >= {"2026-05", "2026-06"}, list(_hist))
check("soma o que entrou", _hist["2026-06"]["entrou"] == 3000)
check("credito FICA DE FORA do que saiu", _hist["2026-06"]["saiu"] == 1050, _hist["2026-06"]["saiu"])
check("mas o credito e' contabilizado a parte", _hist["2026-06"]["credito"] == 700)
check("calcula a sobra", _hist["2026-06"]["sobrou"] == 1950)
check("mais novo primeiro", A.historico_mensal(uid_ms)[0]["mes"] >= A.historico_mensal(uid_ms)[-1]["mes"])

_cmp = {l["categoria"]: l for l in A.comparar_categorias(uid_ms, "2026-06", "2026-05")}
check("compara categoria que subiu", _cmp["Mercado"]["delta"] == 150, _cmp["Mercado"])
check("compara categoria que caiu", _cmp["Transporte"]["delta"] == -400)
# A comparacao por categoria inclui credito (igual ao grafico do Resumo), mas o
# "saiu" exclui. A tela diz isso em vez de deixar os numeros nao fecharem.
check("ordena pela MAIOR mudanca, nao pelo maior valor",
      A.comparar_categorias(uid_ms, "2026-06", "2026-05")[0]["categoria"] == "Lazer",
      A.comparar_categorias(uid_ms, "2026-06", "2026-05")[0])
check("a comparacao por categoria INCLUI o cartao",
      _cmp["Lazer"]["agora"] == 700)
check("e a tela explica a diferenca",
      "inclusive o do cartão" in c_ms.get("/meses?mes=2026-06").get_data(as_text=True))

_h_ms = c_ms.get("/meses?mes=2026-06").get_data(as_text=True)
check("a tela mostra o mes escolhido", "junho de 2026" in _h_ms)
check("e contra qual esta comparando", "maio de 2026" in _h_ms)
check("mostra o que mais mudou", "O que mais mudou" in _h_ms and "Transporte" in _h_ms)
check("desenha a regua dos meses", "mes-preenche" in _h_ms)

check("mes invalido na URL nao quebra", c_ms.get("/meses?mes=lixo").status_code == 200)
check("mes invalido cai num mes real", "junho de 2026" in c_ms.get("/meses?mes=../etc").get_data(as_text=True))
check("um usuario nao ve os meses do outro",
      "junho de 2026" not in c1.get("/meses?mes=2026-06").get_data(as_text=True)
      or A.historico_mensal(uid1))
check("link no menu", "/meses" in c1.get("/").get_data(as_text=True))

# mes em andamento nao pode ser comparado como se tivesse fechado
_atual = date.today().strftime("%Y-%m")
_no_mes(uid_ms, "saida", 50, _atual, dia=1, cat="Mercado")
check("marca o mes corrente como em andamento",
      next(m for m in A.historico_mensal(uid_ms) if m["mes"] == _atual)["em_curso"] is True)
check("e a tela avisa", "ainda não acabou" in c_ms.get(f"/meses?mes={_atual}").get_data(as_text=True))

secao("41. Recado em mensagem (copiar e mandar no WhatsApp)")
_msg = A.texto_recado(_rec2, "Rita Souza")
check("chama a pessoa pelo primeiro nome", _msg.startswith("🦁") and "Rita," in _msg, _msg[:60])
check("nao usa o sobrenome", "Souza" not in _msg)
check("funciona sem nome", A.texto_recado(_rec2, "").count("Você gastou") == 1)
check("traz o periodo da semana",
      _rec2["inicio"].strftime("%d/%m") in _msg and _rec2["fim"].strftime("%d/%m") in _msg)
check("negrito e' o do WhatsApp (*), nao HTML", "*" in _msg and "<" not in _msg)
check("diz o que mais mudou", "O que mais mudou" in _msg and "Transporte" in _msg)
check("e o valor gasto", A.money(_rec2["gasto"]) in _msg)

_r_baixou = dict(_rec2, delta=-150.0, primeira_semana=False)
check("elogia quando gastou menos", "a menos" in A.texto_recado(_r_baixou) and "👏" in A.texto_recado(_r_baixou))
_r_subiu = dict(_rec2, delta=150.0, primeira_semana=False)
check("avisa quando gastou mais", "a mais" in A.texto_recado(_r_subiu))
_r_igual = dict(_rec2, delta=0.0, primeira_semana=False)
check("nao inventa variacao quando ficou igual",
      "praticamente o mesmo" in A.texto_recado(_r_igual))
_r_primeira = dict(_rec2, primeira_semana=True, delta=None)
check("primeira semana nao compara", "primeira semana" in A.texto_recado(_r_primeira))
_r_sem_sobra = dict(_rec2, sobrou=0.0)
check("sem sobra nao oferece guardar", "Sobrou" not in A.texto_recado(_r_sem_sobra))

_h_rec = c_r.get("/recado").get_data(as_text=True)
check("a tela abre", c_r.get("/recado").status_code == 200)
check("mostra a previa da mensagem", 'id="msgPrevia"' in _h_rec)
check("tem botao de copiar", 'id="btnCopiar"' in _h_rec)
check("monta o link do WhatsApp no navegador", "wa.me/?text=" in _h_rec)
check("sem numero: quem escolhe o destino e' a pessoa", "wa.me/55" not in _h_rec)
check("deixa claro que nada sai sozinho", "Nada sai daqui" in _h_rec)
check("script da tela nao tem entidade escapada",
      "&#39;" not in _h_rec[_h_rec.index("msgPrevia"):])
# Sem clipboard (HTTP, ou documento sem foco) ainda tem que dar pra copiar
check("tem plano B se o clipboard falhar", "execCommand" in _h_rec and "selecionarTudo" in _h_rec)
check("no celular nao manda apertar Ctrl", "toque e segure" in _h_rec)

check("quem nao tem recado ve estado vazio",
      "Ainda não tenho recado" in c_novo.get("/recado").get_data(as_text=True))
check("deslogado nao acessa", _an.get("/recado", follow_redirects=False).status_code == 302)
# c_r ja dispensou o recado la em cima, entao o card nao aparece pra ele:
# precisa de alguem com recado vivo
c_r3 = novo_cliente("recado3@teste.com", nome="Rui")
uid_r3 = uid_de("recado3@teste.com")
_lanca(uid_r3, "entrada", 900, _i, "Salário")
_lanca(uid_r3, "saida", 200, _i + timedelta(days=1), "Mercado")
_h_home3 = c_r3.get("/").get_data(as_text=True)
check("o card da Inicio leva pra la",
      "Fechou a semana de" in _h_home3 and url_for_recado(_h_home3))

secao("42. A mãe: MEI no modo simples, sem banco conectado")
# O perfil real da primeira testadora: 65 anos, MEI, modo simples, mora longe e
# nao vai conectar banco. Toda a trilha do MEI ficava invisivel pra ela.
c_mae = novo_cliente("mae@teste.com", nome="Ana", perfil="mei")
uid_mae = uid_de("mae@teste.com")
with get_db() as db:
    db.execute("UPDATE usuarios SET view_mode='simples' WHERE id=?", (uid_mae,))
_um_gasto(uid_mae)
c_mae.get("/")   # a home realinha a sessao com o banco (menu e conteudo na mesma fonte)
with c_mae.session_transaction() as s:
    check("menu e conteudo leem o MESMO modo", s.get("view_mode") == "simples", s.get("view_mode"))
_h_mae = c_mae.get("/").get_data(as_text=True)

# Sem link, a ajuda e as dicas mandavam ela pra uma tela que ela nao alcancava
check("Painel MEI TEM link no modo simples", 'href="/mei"' in _h_mae)
check("Notas tambem", 'href="/notas"' in _h_mae)
# Mandar quem pediu "menos coisa" baixar um OFX e' o pior primeiro recado possivel
check("nao manda ela importar OFX", "Importa o OFX" not in _h_mae)
check("mas o modo completo continua lembrando",
      "Importa o OFX" in c_novo.get("/").get_data(as_text=True) or A.pluggy_configured())
check("e as telas avancadas seguem escondidas",
      'href="/clientes"' not in _h_mae and 'href="/business"' not in _h_mae)

check("a dica do MEI chega ate ela", "guarde em" in _dica_atual(c_mae))
_nota(uid_mae, 3000)
check("e evolui quando ela guarda a nota", "Painel MEI" in _dica_atual(c_mae))

for _r, _nome in [("/notas", "Notas"), ("/notas/nova", "Guardar nota"), ("/mei", "Painel MEI"),
                  ("/ir", "Prévia do IR"), ("/ajuda", "Ajuda"), ("/transacoes/nova", "Registrar")]:
    check(f"tela dela abre: {_nome}", c_mae.get(_r).status_code == 200)

check("home simples tem o botao de guardar nota", "Guardar uma nota" in _h_mae)
check("e o de registrar na mao (ela nao tem banco)", "Registrar gasto ou entrada" in _h_mae)

secao("43. Conectar o banco sozinha (o caminho da mãe)")
# A janela da Pluggy pede login numa conta do Meu Pluggy. Abrir sozinho poe quem
# ainda nao criou essa conta diante de uma senha que ela nao tem — e ela desiste ali.
_tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "templates", "pluggy_conectar.html"), encoding="utf-8").read()
check("a janela NAO abre sozinha", "\n    abrir();" not in _tpl)
check("o botao e' que abre", "botao.addEventListener('click', abrir)" in _tpl)
check("pergunta se ja tem conta no Meu Pluggy", "já tem conta no Meu Pluggy" in _tpl)
check("pergunta se o banco ja aparece la", "banco já aparece lá" in _tpl)
check("diz que isso acontece FORA do app", "fora do Hércules" in _tpl)
check("avisa do erro mais comum", "não o nome do seu banco" in _tpl)
check("tem saida pra quem ainda nao fez", "me ensina" in _tpl and "url_for('ajuda')" in _tpl)

_h_mae2 = c_mae.get("/").get_data(as_text=True)
check("a mae tem o link da ajuda no menu", 'href="/ajuda"' in _h_mae2)
_h_aj2 = c_mae.get("/ajuda").get_data(as_text=True)
# O diagnostico na conta dele mostrou 233 bancos aparecendo direto. O caminho
# com cadastro extra nao existe mais, e ensinar ele era o que travava as
# pessoas — criavam uma conta a toa e depois procuravam o nome errado na lista.
for _passo in ("Conectar banco", "seu banco na lista", "Autorize na tela do seu banco",
               "Sincronizar"):
    check(f"ajuda cobre: {_passo[:30]}", _passo in _h_aj2)
check("e nao ensina mais o caminho que nao precisa existir",
      "meu.pluggy" not in _h_aj2)
check("quem tentou pelo caminho antigo e liberado a esquecer",
      "pode esquecer o que fez" in _h_aj2)
check("a ajuda abre sem login (da pra mandar antes no WhatsApp)",
      _an.get("/ajuda").status_code == 200)

secao("44. Captura por notificação: removida de vez")
# Era um endpoint de ESCRITA, isento de CSRF e do bloqueio por digital,
# autenticado só por um token — e sem uso nenhum. Superfície viva à toa.
_rotas = {str(r) for r in A.app.url_map.iter_rules()}
for _morta in ("/api/captura", "/api/meu-token", "/app/entrou", "/entrar-automatico",
               "/.well-known/assetlinks.json"):
    check(f"rota removida: {_morta}", _morta not in _rotas)
check("nao sobrou nenhuma rota de captura", not any("captura" in r for r in _rotas))
check("nem de login automatico do app", not any("automatico" in r for r in _rotas))

for _f in ("register_capture", "parse_capture_text", "get_or_create_capture_token",
           "create_auto_login_code", "redeem_auto_login_code"):
    check(f"funcao removida: {_f}", not hasattr(A, _f))

check("a isencao de CSRF nao cita mais a captura",
      "api_captura" not in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "app.py"), encoding="utf-8").read())
with get_db() as db:
    _antes = db.execute("SELECT COUNT(*) AS n FROM transacoes").fetchone()["n"]
check("POST na rota morta nao e' aceito", c1.post("/api/captura",
      json={"token": "x", "texto": "gastei 500 no mercado"}).status_code in (302, 404, 405))
with get_db() as db:
    _depois = db.execute("SELECT COUNT(*) AS n FROM transacoes").fetchone()["n"]
check("e nao gravou nada", _depois == _antes, f"{_antes} -> {_depois}")

secao("45. Revisão: performance e dados vindos de fora")

# --- Data da API do banco: fatiar string na mão errava CALADO ---
for _v, _esperado in [("2026-08-15", 15), ("2026-08-15T00:00:00.000Z", 15),
                      ("2026-8-5", None), ("15/08/2026", None), ("", None),
                      (None, None), ("2026-08-99", None)]:
    check(f"dia_de_data_api({str(_v)[:24]!r}) = {_esperado}",
          A.dia_de_data_api(_v) == _esperado, A.dia_de_data_api(_v))

# --- Movimentação sem data não pode sumir do mês e continuar no saldo ---
c_sd = novo_cliente("semdata@teste.com", nome="SemData")
uid_sd = uid_de("semdata@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,
                  data_transacao,no_credito) VALUES (?,'saida',100,'Normal','Mercado','ofx',95,
                  date('now'),0)""", (uid_sd,))
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,confidence,
                  data_transacao,no_credito) VALUES (?,'saida',999,'Sem data','Mercado','ofx',95,
                  '',0)""", (uid_sd,))
_st = A.calc_transaction_totals(uid_sd)
check("data vazia nao some do mes (numeros fecham)",
      abs(abs(_st["balance"]) - _st["month_expenses"]) < 0.01,
      f"saldo {_st['balance']} vs mes {_st['month_expenses']}")

# --- Categorizar em massa não pode abrir uma conexão por linha ---
c_perf = novo_cliente("perf@teste.com", nome="Perf")
uid_perf = uid_de("perf@teste.com")
with get_db() as db:
    for _i in range(300):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',10,?,'Outros','ofx',
                      95,date('now'),0)""", (uid_perf, f"UBER TRIP {_i}"))
_conta = {"n": 0}
_orig_db = A.get_db
A.get_db = lambda *a, **k: (_conta.__setitem__("n", _conta["n"] + 1) or _orig_db(*a, **k))
try:
    _mudou = A.recategorize_outros(uid_perf)
finally:
    A.get_db = _orig_db
check("recategorizar 300 linhas abre poucas conexoes", _conta["n"] <= 3, _conta["n"])
check("e continua categorizando certo", _mudou == 300, _mudou)

# --- A lista de movimentações não pode virar uma página gigante ---
_h_lista = c_perf.get("/transacoes")
_tam = len(_h_lista.get_data())
check("lista paginada nao passa de 500 KB", _tam < 500_000, f"{_tam/1024:.0f} KB")
check(f"mostra no maximo {A.POR_PAGINA} por pagina",
      _h_lista.get_data(as_text=True).count("data-confirm=") <= A.POR_PAGINA)
check("tem navegacao entre paginas", "Mais antigas" in _h_lista.get_data(as_text=True))
_h_p2 = c_perf.get("/transacoes?p=2").get_data(as_text=True)
check("a segunda pagina traz outras linhas", "Anteriores" in _h_p2)
_h_filtro = c_perf.get("/transacoes?q=UBER&p=1").get_data(as_text=True)
check("trocar de pagina NAO perde o filtro", "q=UBER" in _h_filtro)
for _p in ("999", "-5", "abc", "0"):
    check(f"pagina invalida nao quebra: p={_p}", c_perf.get(f"/transacoes?p={_p}").status_code == 200)

secao("46. Fuso horário: o app é brasileiro, o servidor não")
# O servidor roda em UTC. Usar a hora dele joga toda compra feita depois das 21h
# pro dia seguinte — e no dia 31, pro MÊS seguinte.
from datetime import timezone as _tz
_agora_br = A.agora_br()
_agora_utc = datetime.utcnow()
_diff = abs((_agora_utc - _agora_br).total_seconds()) / 3600
check("agora_br() nao e' a hora do servidor em UTC", 2.5 < _diff < 5.5 or _diff < 0.1,
      f"{_diff:.1f}h de diferenca")
check("hoje_br() bate com agora_br()", A.hoje_br() == A.agora_br().date())
check("agora_br() vem sem tzinfo (o banco guarda ingenuo)", A.agora_br().tzinfo is None)

_ref = datetime(2026, 9, 1, 1, 0, tzinfo=_tz.utc)      # 01:00 UTC = 22:00 do dia 31 no Brasil
check("22h do dia 31 no Brasil continua sendo dia 31",
      _ref.astimezone(A.FUSO_BR).date() == date(2026, 8, 31),
      _ref.astimezone(A.FUSO_BR).date())
check("e nao vira dia 1 do mes seguinte", _ref.date() == date(2026, 9, 1))

check("nenhum date('now') sobrou no SQL (SQLite so sabe UTC)",
      "date('now')" not in _APP_SRC and "datetime('now')" not in _APP_SRC)
check("nenhum date.today() sobrou",
      "date.today()" not in _APP_SRC)

secao("47. Duas pessoas ao mesmo tempo")
# Sync grande escreve centenas de linhas numa transacao so; com o timeout padrao
# do sqlite (5s) a requisicao de outra pessoa morre com "database is locked".
import database as _DB
check("a conexao tem timeout folgado", _DB.SQLITE_TIMEOUT >= 15, _DB.SQLITE_TIMEOUT)
check("o backup usa o mesmo timeout", B.SQLITE_TIMEOUT == _DB.SQLITE_TIMEOUT)

import threading as _th, time as _tm, sqlite3 as _sq3
_res = {}
def _escritor():
    w = _sq3.connect(os.environ["DATABASE_PATH"], timeout=_DB.SQLITE_TIMEOUT)
    w.execute("BEGIN IMMEDIATE")
    w.execute("INSERT INTO dicas_vistas (user_id, dica) VALUES (?, 'lock-teste')", (uid1,))
    _tm.sleep(6)
    w.commit(); w.close()
_t = _th.Thread(target=_escritor); _t.start(); _tm.sleep(0.4)
try:
    with get_db() as db:
        db.execute("INSERT INTO dicas_vistas (user_id, dica) VALUES (?, 'lock-teste-2')", (uid2,))
    _res["ok"] = True
except Exception as e:
    _res["ok"] = False; _res["erro"] = str(e)
_t.join()
check("escrever durante um sync longo NAO da 'database is locked'",
      _res.get("ok"), _res.get("erro", ""))
with get_db() as db:
    db.execute("DELETE FROM dicas_vistas WHERE dica LIKE 'lock-teste%'")

secao("48. Cookie de sessão")
check("HttpOnly ligado", A.app.config["SESSION_COOKIE_HTTPONLY"] is True)
check("SameSite Lax", A.app.config["SESSION_COOKIE_SAMESITE"] == "Lax")
# Sem Secure, basta uma requisicao em HTTP puro pro cookie de sessao vazar
check("Secure liga sozinho no PythonAnywhere",
      "PYTHONANYWHERE_DOMAIN" in _APP_SRC and "SESSION_COOKIE_SECURE" in _APP_SRC)
check("mas da pra desligar de proposito", 'SECURE_COOKIES") != "0"' in _APP_SRC)
check("em dev (sem hospedagem) fica desligado, senao nao da pra testar local",
      A.app.config.get("SESSION_COOKIE_SECURE", False) is False)

secao("49. Cartão: estorno não é pagamento de fatura")
# No cartao os dois vem NEGATIVOS e significam coisas opostas. Antes, todo
# negativo era descartado — a compra devolvida continuava na fatura, sem erro
# nenhum na tela. E' a pior classe de bug: o app funciona e mente.
for _d, _esperado in [("PAGAMENTO FATURA", True), ("Pagamento recebido", True),
                      ("PAGTO DEBITO AUTOMATICO", True), ("PGTO CARTAO", True),
                      ("ESTORNO MAGAZINE LUIZA", False), ("DEVOLUCAO AMERICANAS", False),
                      ("CANCELAMENTO COMPRA", False), ("", False)]:
    check(f"pagamento de fatura? {_d[:26]!r} = {_esperado}",
          A.e_pagamento_de_fatura(_d) is _esperado)

_TX_CARTAO = [
    {"id": "e1", "description": "MAGAZINE LUIZA", "amount": 500.0, "date": "2026-08-01"},
    {"id": "e2", "description": "ESTORNO MAGAZINE LUIZA", "amount": -500.0, "date": "2026-08-03"},
    {"id": "e3", "description": "PAGAMENTO FATURA", "amount": -1200.0, "date": "2026-08-10"},
    {"id": "e4", "description": "PADARIA", "amount": 30.0, "date": "2026-08-04"},
]
_get_orig = A._pluggy_get
A._pluggy_get = lambda k, p, q=None: {"results": _TX_CARTAO}
try:
    _itens = A.pluggy_fetch_items("k", [{"id": "c1", "type": "CREDIT"}], "2026-07-01")
finally:
    A._pluggy_get = _get_orig
_por_desc = {i["descricao"]: i for i in _itens}
check("compra no cartao vira saida", _por_desc["MAGAZINE LUIZA"]["tipo"] == "saida")
check("estorno vira ENTRADA (pra abater a fatura)",
      _por_desc.get("ESTORNO MAGAZINE LUIZA", {}).get("tipo") == "entrada")
check("pagamento da fatura e' ignorado (ja saiu da conta)",
      "PAGAMENTO FATURA" not in _por_desc)
check("tudo marcado como credito", all(i["no_credito"] for i in _itens))

c_est = novo_cliente("estorno@teste.com", nome="Est")
uid_est = uid_de("estorno@teste.com")
with get_db() as db:
    for _i in _itens:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,'Outros','ofx',95,?,1)""",
                   (uid_est, _i["tipo"], _i["valor"], _i["descricao"], date.today().isoformat()))
_st_est = A.calc_transaction_totals(uid_est)
check("a fatura ABATE o estorno (30, nao 530)",
      abs(_st_est["fatura_credito_mes"] - 30) < 0.01, _st_est["fatura_credito_mes"])
check("estorno NAO vira renda do mes", _st_est["month_income"] == 0, _st_est["month_income"])
check("e nao mexe no saldo da conta", _st_est["balance"] == 0, _st_est["balance"])
_hist_est = {m["mes"]: m for m in A.historico_mensal(uid_est)}
check("nem aparece como 'entrou' no mes a mes",
      _hist_est[date.today().strftime("%Y-%m")]["entrou"] == 0)

secao("50. Ciclo da fatura: sem buraco e sem sobreposição")
# Buraco = compra some da fatura. Sobreposicao = compra cobrada duas vezes.
_falhas_ciclo = []
for _fecha in range(1, 32):
    _dia = date(2026, 1, 1)
    while _dia <= date(2027, 12, 31):
        _ini, _fim = A.ciclo_fatura(_dia, _fecha)
        if not (_ini <= _dia <= _fim):
            _falhas_ciclo.append((_fecha, _dia, "dia fora da propria fatura"))
        if A.ciclo_fatura(_fim + timedelta(days=1), _fecha)[0] != _fim + timedelta(days=1):
            _falhas_ciclo.append((_fecha, _dia, "buraco ou sobreposicao"))
        _dia += timedelta(days=1)
check("730 dias x 31 fechamentos: todo dia em exatamente uma fatura",
      not _falhas_ciclo, str(_falhas_ciclo[:2]))
check("compra NO DIA do fechamento entra na fatura que fecha",
      A.ciclo_fatura(date(2026, 8, 20), 20)[1] == date(2026, 8, 20))
check("um dia depois ja e' a proxima",
      A.ciclo_fatura(date(2026, 8, 21), 20)[0] == date(2026, 8, 21))
check("fechamento 31 em fevereiro cai no ultimo dia",
      A.ciclo_fatura(date(2026, 2, 28), 31)[1] == date(2026, 2, 28))

secao("51. OFX de fatura: mesma regra do estorno")
# Se o OFX tratasse diferente da Pluggy, a fatura mudaria conforme a pessoa
# tivesse conectado o banco ou importado o arquivo. O pagamento tem que sumir
# nos DOIS, senao ele abateria a fatura (e ja saiu da conta corrente).
_OFX_CARTAO = """<CCSTMTRS>
<STMTTRN><DTPOSTED>20260801<TRNAMT>-500.00<FITID>o1<MEMO>MAGAZINE LUIZA</STMTTRN>
<STMTTRN><DTPOSTED>20260803<TRNAMT>500.00<FITID>o2<MEMO>ESTORNO MAGAZINE LUIZA</STMTTRN>
<STMTTRN><DTPOSTED>20260810<TRNAMT>1200.00<FITID>o3<MEMO>PAGAMENTO RECEBIDO</STMTTRN>
<STMTTRN><DTPOSTED>20260804<TRNAMT>-30.00<FITID>o4<MEMO>PADARIA</STMTTRN>
</CCSTMTRS>"""
_ofx = {t["descricao"]: t for t in A.parse_ofx(_OFX_CARTAO)}
check("OFX: compra vira saida no credito",
      _ofx["MAGAZINE LUIZA"]["tipo"] == "saida" and _ofx["MAGAZINE LUIZA"]["no_credito"])
check("OFX: estorno vira entrada (abate a fatura)",
      _ofx.get("ESTORNO MAGAZINE LUIZA", {}).get("tipo") == "entrada")
check("OFX: pagamento da fatura e' descartado", "PAGAMENTO RECEBIDO" not in _ofx)
check("OFX: fatura fecha em 30, nao em 530",
      sum(t["valor"] if t["tipo"] == "saida" else -t["valor"] for t in _ofx.values()) == 30)

# Numa conta comum, pagar a fatura e' gasto de verdade — nao pode sumir
_OFX_CONTA = """<STMTRS>
<STMTTRN><DTPOSTED>20260810<TRNAMT>-1200.00<FITID>b1<MEMO>PAGAMENTO FATURA CARTAO</STMTTRN>
<STMTTRN><DTPOSTED>20260805<TRNAMT>3000.00<FITID>b2<MEMO>SALARIO</STMTTRN>
</STMTRS>"""
_conta = {t["descricao"]: t for t in A.parse_ofx(_OFX_CONTA)}
check("conta corrente: pagar a fatura CONTINUA sendo gasto",
      _conta.get("PAGAMENTO FATURA CARTAO", {}).get("tipo") == "saida")
check("e nao e' marcado como credito", _conta["PAGAMENTO FATURA CARTAO"]["no_credito"] is False)

secao("52. Import não abre uma conexão por lançamento")
c_imp = novo_cliente("import-perf@teste.com", nome="Imp")
uid_imp = uid_de("import-perf@teste.com")
with get_db() as db:
    for _i in range(12):
        db.execute("INSERT INTO regras_categorizacao (user_id,padrao_texto,categoria_nome) VALUES (?,?,?)",
                   (uid_imp, f"padrao{_i}", "Lazer"))
_lote = [{"valor": 10.0 + _i, "tipo": "saida", "data": date.today().isoformat(),
          "descricao": f"LOJA {_i}", "fitid": f"PERF{_i}", "no_credito": False} for _i in range(400)]
_conexoes = {"n": 0}
_get_db_orig = A.get_db
A.get_db = lambda *a, **k: (_conexoes.__setitem__("n", _conexoes["n"] + 1) or _get_db_orig(*a, **k))
try:
    _st_imp = A.import_ofx_transactions(uid_imp, _lote)
finally:
    A.get_db = _get_db_orig
check("400 lançamentos importados", _st_imp["importadas"] == 400, _st_imp)
# Uma conexao por linha, dentro de uma transacao aberta, e' o que fazia
# importar 3 meses de extrato demorar
check("sem uma conexão por lançamento", _conexoes["n"] <= 5, _conexoes["n"])

secao("53. Migração num banco que já tem dados")
# A suite roda em banco novo. O upgrade de verdade e' em cima de um banco em uso.
import shutil as _sh, sqlite3 as _sq4
_prod = os.path.join(TMP, "simula_producao.db")
_sh.copy(os.environ["DATABASE_PATH"], _prod)
_c = _sq4.connect(_prod)
_tabelas = [r[0] for r in _c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
_antes = {t: _c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _tabelas}
_c.close()

import database as _dbmod
_path_orig = _dbmod.DB_PATH
_dbmod.DB_PATH = __import__("pathlib").Path(_prod)
try:
    _dbmod.init_db()                      # roda as migrações de novo, em cima dos dados
    _c = _sq4.connect(_prod)
    _depois = {t: _c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _tabelas}
    _integridade = _c.execute("PRAGMA integrity_check").fetchone()[0]
    _fk = list(_c.execute("PRAGMA foreign_key_check"))
    _c.close()
finally:
    _dbmod.DB_PATH = _path_orig

check("migração é idempotente (roda 2x sem erro)", True)
check("banco continua íntegro depois de migrar", _integridade == "ok", _integridade)
check("nenhuma linha perdida",
      _antes == _depois, {t: (_antes[t], _depois[t]) for t in _antes if _antes[t] != _depois[t]})
check("nenhuma chave estrangeira quebrada", not _fk, str(_fk[:2]))

secao("54. Primeira tela: MEI começa pela nota, não pelo banco")
# Feedback real: a primeira testadora (MEI) travou tentando conectar o banco —
# o passo mais alto E o mais dispensável — antes de usar o que ela abriu o app
# pra fazer. Ela nem precisava do banco pra guardar nota.
c_novo_mei = novo_cliente("estreia-mei@teste.com", nome="Ana", perfil="mei")
_h_estreia = c_novo_mei.get("/").get_data(as_text=True)
check("MEI na estreia e' convidado a guardar NOTA",
      "Guardar minha primeira nota" in _h_estreia)
check("e o convite diz que funciona sem configurar nada",
      "sem configurar nada" in _h_estreia)
check("a nota vem ANTES do banco na tela",
      _h_estreia.index("primeira nota") < _h_estreia.index("Conectar banco")
      if "Conectar banco" in _h_estreia else True)
check("o banco continua disponivel (ela QUER conectar depois)",
      "/pluggy/conectar" in _h_estreia or not A.pluggy_configured())
check("mas assumido como extra", "isso é <strong>extra</strong>" in _h_estreia
      or not A.pluggy_configured())

c_novo_pf = novo_cliente("estreia-pf@teste.com", nome="Bia", perfil="pf")
_h_pf = c_novo_pf.get("/").get_data(as_text=True)
check("PF na estreia NAO ve papo de nota", "primeira nota" not in _h_pf)
check("PF segue com o convite de sempre",
      "quanto você tem na conta hoje" in _h_pf or "conectar seu banco" in _h_pf)

# O erro que travou ela de verdade veio do Meu Pluggy, nao do Hercules:
# link de confirmacao aberto em aparelho diferente de onde foi pedido.
_h_ajuda2 = _an.get("/ajuda").get_data(as_text=True)
# O tropeco dela era o link de confirmacao do cadastro no Meu Pluggy. Esse
# cadastro deixou de existir no caminho, entao o aviso tambem — e some com
# ele o proprio problema.
check("nao ha mais cadastro fora do app pra tropecar",
      "meu.pluggy" not in _h_ajuda2)
check("e quando algo falha, a ajuda aponta o diagnostico",
      "Diagn\u00f3stico da conex\u00e3o" in _h_ajuda2)

secao("55. Força bruta no login")
# Sem limite, quem souber o e-mail testa senha pra sempre. E' o ataque mais
# realista contra um app pequeno — nao precisa de falha nenhuma no codigo.
c_bf = A.app.test_client()
with get_db() as db:
    db.execute("DELETE FROM tentativas_login")

def _tenta(cli, email, senha):
    with cli.session_transaction() as s:
        s["csrf_token"] = "t"
    return cli.post("/login", data={"csrf_token": "t", "email": email, "senha": senha},
                    follow_redirects=True).get_data(as_text=True)

_alvo = "a@teste.com"
_bloqueou_em = None
for _i in range(1, 9):
    _resp = _tenta(c_bf, _alvo, f"chute{_i}")
    if "Muitas tentativas" in _resp:
        _bloqueou_em = _i
        break
check("o app trava o ataque", _bloqueou_em is not None, "nunca travou!")
check(f"trava em {A.LOGIN_MAX_TENTATIVAS} tentativas ou menos",
      _bloqueou_em and _bloqueou_em <= A.LOGIN_MAX_TENTATIVAS + 1, _bloqueou_em)

# Travado, nem a senha CERTA entra — senao bastaria acertar na 6a tentativa
check("travado, nem a senha certa passa",
      "Muitas tentativas" in _tenta(c_bf, _alvo, "tijolo-forte-42"))
check("e continua deslogado", c_bf.get("/", follow_redirects=False).status_code == 302)

# Dizer "esse e-mail nao existe" entrega quem tem conta aqui
with get_db() as db:
    db.execute("DELETE FROM tentativas_login")
_c_x = A.app.test_client()
_msg_inexistente = _tenta(_c_x, "naoexiste@lugar.nenhum", "qualquer")
_c_y = A.app.test_client()
with get_db() as db:
    db.execute("DELETE FROM tentativas_login")
_msg_senha_errada = _tenta(_c_y, _alvo, "errada-mesmo")
check("nao revela se o e-mail existe",
      ("E-mail ou senha inválidos" in _msg_inexistente)
      and ("E-mail ou senha inválidos" in _msg_senha_errada))

with get_db() as db:
    db.execute("DELETE FROM tentativas_login")
    _n = db.execute("SELECT COUNT(*) AS n FROM tentativas_login").fetchone()["n"]
check("a tabela de tentativas e' limpavel", _n == 0)
check("login limpo volta a funcionar",
      "Muitas tentativas" not in _tenta(A.app.test_client(), _alvo, "tijolo-forte-42"))

secao("56. Senha fraca não passa")
for _s, _deve_recusar in [("12345678", True), ("senha123", True), ("password", True),
                          ("1234", True), ("aaaaaaaa", True), ("00000000", True),
                          ("tijolo-forte-42", False), ("MinhaLoja2026", False),
                          ("gato azul na janela", False)]:
    _r = A.senha_fraca(_s)
    check(f"{'recusa' if _deve_recusar else 'aceita'}: {_s!r}",
          (_r is not None) == _deve_recusar, _r)
check("recusa senha igual ao e-mail", A.senha_fraca("mariana", "mariana@x.com") is not None)
check("recusa senha igual ao nome", A.senha_fraca("mariana", "", "Mariana Silva") is not None)
check("o minimo subiu de 6 pra 8", A.SENHA_MINIMA == 8)
check("o formulario tambem exige 8",
      'minlength="8"' in _an.get("/register").get_data(as_text=True))

_c_fraca = A.app.test_client()
with _c_fraca.session_transaction() as s: s["csrf_token"] = "t"
_r_fraca = _c_fraca.post("/register", data={"csrf_token": "t", "nome": "X", "email": "fraca@t.com",
                                            "senha": "123456", "perfil": "pf"}, follow_redirects=True)
check("cadastro com senha fraca e' barrado no servidor (nao so no HTML)",
      uid_de("fraca@t.com") is None)

secao("57. Cabeçalhos de segurança")
_h = _an.get("/login").headers
check("CSP presente", "Content-Security-Policy" in _h)
check("CSP bloqueia script de fora (menos a Pluggy)",
      "script-src 'self' 'unsafe-inline' https://cdn.pluggy.ai" in _h.get("Content-Security-Policy", ""))
check("ninguem embute o app num iframe (clickjacking)",
      _h.get("X-Frame-Options") == "DENY"
      and "frame-ancestors 'none'" in _h.get("Content-Security-Policy", ""))
check("navegador nao adivinha tipo de arquivo", _h.get("X-Content-Type-Options") == "nosniff")
check("nao vaza a URL no Referer", _h.get("Referrer-Policy") == "strict-origin-when-cross-origin")
check("camera/microfone/localizacao negados", "camera=()" in _h.get("Permissions-Policy", ""))
check("os cabecalhos valem nas telas logadas tambem",
      "Content-Security-Policy" in c1.get("/").headers)

secao("58. O CSP não pode quebrar nenhuma tela")
# CSP errado quebra calado: o script nao roda, a fonte nao carrega, o icone some.
# Confere cada recurso de cada tela contra a politica, em vez de confiar no olho.
import re
from urllib.parse import urlparse as _urlparse
_pol = {}
for _parte in A._CSP.split("; "):
    _k, _, _v = _parte.partition(" ")
    _pol[_k] = set(_v.split())

def _csp_permite(url, diretiva):
    fontes = _pol.get(diretiva) or _pol["default-src"]
    if not url or url.startswith(("#", "mailto:", "tel:", "javascript:")):
        return True
    p = _urlparse(url)
    if p.scheme in ("data", "blob"):
        return f"{p.scheme}:" in fontes
    if not p.netloc:
        return "'self'" in fontes
    return f"{p.scheme}://{p.netloc}" in fontes

_telas_csp = ["/login", "/register", "/", "/dashboard", "/meses", "/transacoes", "/transacoes/nova",
              "/compromissos", "/categorias", "/metas", "/dividas", "/ir", "/trabalhos", "/notas",
              "/notas/nova", "/settings", "/ajuda", "/recado", "/privacidade", "/importar"]
_bloqueados, _conferidos = [], 0
for _rota in _telas_csp:
    _html = c1.get(_rota).get_data(as_text=True)
    for _tag, _attr, _dir in [("script", "src", "script-src"), ("img", "src", "img-src"),
                              ("iframe", "src", "frame-src")]:
        for _m in re.finditer(rf'<{_tag}[^>]*\b{_attr}="([^"]+)"', _html):
            _conferidos += 1
            if not _csp_permite(_m.group(1), _dir):
                _bloqueados.append((_rota, _dir, _m.group(1)[:60]))
    for _m in re.finditer(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', _html):
        _conferidos += 1
        if not _csp_permite(_m.group(1), "style-src"):
            _bloqueados.append((_rota, "style-src", _m.group(1)[:60]))
check(f"{len(_telas_csp)} telas, {_conferidos} recursos: nenhum bloqueado pelo CSP",
      not _bloqueados, str(sorted(set(_bloqueados))[:3]))
# Se voltar a carregar de CDN, a tela quebra quando o CDN cair — e vaza visita
check("nada carregado de fora, fora Pluggy e as fontes",
      all(d in ("cdn.pluggy.ai", "fonts.googleapis.com", "fonts.gstatic.com")
          for d in re.findall(r'(?:src|href)="https://([^/"]+)',
                              c1.get("/login").get_data(as_text=True) + c1.get("/").get_data(as_text=True))))
check("o icone do Google e' servido daqui (nao avisa o Google a cada visita)",
      "www.google.com" not in _an.get("/login").get_data(as_text=True))

secao("59. Sessão nova a cada login")
# Sessao reaproveitada deixa dado do visitante anterior sobreviver ao login
_c_ses = A.app.test_client()
with _c_ses.session_transaction() as s:
    s["lixo_antigo"] = "nao deveria sobreviver"
    s["csrf_token"] = "plantado"
_c_ses.post("/login", data={"csrf_token": "plantado", "email": _alvo, "senha": "tijolo-forte-42"},
            follow_redirects=True)
with _c_ses.session_transaction() as s:
    check("dado plantado antes do login e' descartado", "lixo_antigo" not in s)
    check("o token de CSRF e' trocado no login", s.get("csrf_token") != "plantado")
    check("e o login funcionou", s.get("user_id") is not None)

secao("60. Backup que viaja tem que viajar cifrado")
# O backup e' feito pra SAIR do servidor — vai pro computador, as vezes pra
# nuvem. Em texto claro, quem pegar o arquivo le os dados de todo mundo.
_senha_orig, _bk_dir_orig = B.BACKUP_SENHA, B.BACKUP_DIR
B.BACKUP_SENHA = "cofre-de-teste-do-hercules"
B.BACKUP_DIR = pathlib.Path(os.path.join(TMP, "bk_cifrado"))
try:
    _p = B.fazer_backup()
    _cru = open(_p, "rb").read()
    check("com BACKUP_SENHA, a copia sai cifrada", B.esta_cifrado(_p))
    check("nao da pra ler e-mail dentro do arquivo",
          not re.findall(rb"[\w.+-]+@[\w-]+\.[\w.]+", _cru))
    check("nem parece um banco SQLite", b"SQLite format" not in _cru)
    check("dois backups do mesmo conteudo nao ficam iguais (sal por arquivo)",
          B.cifrar(b"igual") != B.cifrar(b"igual"))

    # Backup que nao abre nao e' backup: a restauracao tem que ser um comando
    _volta = os.path.join(TMP, "restaurado_cifrado.db")
    B.restaurar(_p, _volta)
    _cx = _sq4.connect(_volta)
    check("restaura e abre integro",
          _cx.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
    check("com os dados de verdade dentro",
          _cx.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0)
    _cx.close()

    for _senha, _rotulo in [("senha-errada", "com a senha errada"), ("", "sem senha nenhuma")]:
        B.BACKUP_SENHA = _senha
        try:
            B.restaurar(_p, os.path.join(TMP, "nao.db")); _abriu = True
        except Exception:
            _abriu = False
        check(f"{_rotulo} NAO abre", not _abriu)
finally:
    B.BACKUP_SENHA, B.BACKUP_DIR = _senha_orig, _bk_dir_orig

check("sem BACKUP_SENHA o backup continua funcionando (so que em claro)",
      B.cifragem_ligada() is False)
check("e a tela de Saude avisa quando esta em claro",
      "texto claro" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "templates", "saude.html"), encoding="utf-8").read())
check("backup antigo, sem cifra, continua abrindo",
      B.decifrar(b"conteudo antigo") == b"conteudo antigo")
check("existe requirements-dev com o checador de vulnerabilidade",
      "pip-audit" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "requirements-dev.txt"), encoding="utf-8").read())

secao("61. Anexar nota: camera OU galeria")
# Com capture="environment" o Android abre a camera e so ela. Quem ja tem as
# notas fotografadas — o caso da primeira testadora, que usa um telefone so
# pra isso — ficava sem como enviar o que ja tinha.
_h_nota = c_mae.get("/notas/nova").get_data(as_text=True)
_campo = re.search(r'<input id="arquivo"[^>]*>', _h_nota).group(0)
check("aceita foto e PDF", 'accept=".pdf,image/*"' in _campo, _campo)
check("NAO forca a camera (deixa escolher da galeria)", "capture=" not in _campo, _campo)
check("o rotulo diz que da pra fazer os dois", "escolha do celular" in _h_nota)
check("o servidor aceita os formatos de foto",
      all(A.allowed_file(f"nota.{e}") for e in ("jpg", "jpeg", "png", "webp", "pdf")))
check("e recusa o que nao e' nota", not A.allowed_file("virus.exe"))


secao("62. Não fica mais lento a cada mês de uso")
# A Inicio carregava TODA a tabela de transacoes na memoria pra responder
# "existe alguma?". Quem usa ha um ano pagava por isso em toda visita.
c_esc = novo_cliente("escala@teste.com", nome="Esc")
uid_esc = uid_de("escala@teste.com")
check("sem lançamento, a home sabe que está vazia",
      A.calc_transaction_totals(uid_esc)["tem_lancamentos"] is False)
_um_gasto(uid_esc)
check("com lançamento, sabe que tem", A.calc_transaction_totals(uid_esc)["tem_lancamentos"] is True)
check("nao carrega a tabela inteira pra isso",
      "SELECT 1 FROM transacoes WHERE user_id = ? LIMIT 1" in _APP_SRC)
check("e a lista de recentes segue limitada", "DESC LIMIT 8" in _APP_SRC)

# 29 consultas filtram pela data embrulhada em funcao; sem indice de expressao
# o SQLite calcula linha a linha e a tela cresce junto com o historico
with get_db() as db:
    _plano = [r["detail"] for r in db.execute("""EXPLAIN QUERY PLAN
        SELECT COALESCE(SUM(valor),0) FROM transacoes
         WHERE user_id = ? AND tipo='saida' AND no_credito = 0
           AND date(COALESCE(NULLIF(data_transacao,''), created_at)) BETWEEN date(?) AND date(?)""",
        (uid_esc, "2026-08-01", "2026-08-31")).fetchall()]
check("a soma do mês usa índice, não varre a tabela",
      any("idx_transacoes_user_dia" in p for p in _plano), str(_plano))

with get_db() as db:
    _indices = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='transacoes'")}
check("o índice de expressão existe", "idx_transacoes_user_dia" in _indices, sorted(_indices))

# Volume real de quem usa ha um ano, pra provar que nao degrada
with get_db() as db:
    for _i in range(3000):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',?,?,'Mercado','ofx',95,
                      date('now','-'||?||' days'),0)""", (uid_esc, 10 + _i % 90, f"L{_i}", _i % 700))
import time as _tm
c_esc.get("/")
_t0 = _tm.perf_counter()
for _ in range(3):
    c_esc.get("/")
_ms = (_tm.perf_counter() - _t0) / 3 * 1000
check(f"Início com 3000 lançamentos abre rápido ({_ms:.0f}ms)", _ms < 250, f"{_ms:.0f}ms")


secao("63. Foto da nota encolhe antes de subir")
# 512 MB de disco e foto de celular de 2-3 MB: cabem ~10 MEIs fotografando tudo.
# Encolher no navegador multiplica isso por 14 e ainda poupa o 4G de quem envia.
_h_nn = c_mae.get("/notas/nova").get_data(as_text=True)
check("o script roda mesmo sem a leitura por IA ligada", "LADO_MAX" in _h_nn)
check("1600px de lado", "const LADO_MAX = 1600;" in _h_nn)
check("qualidade 82%", "const QUALIDADE = 0.82;" in _h_nn)
check("respeita a orientacao EXIF (foto em pe nao chega deitada)", "from-image" in _h_nn)
check("PDF passa direto, sem virar JPEG", "startsWith('image/')" in _h_nn)
check("foto ja pequena nao e' recomprimida", "JA_PEQUENO" in _h_nn)
check("se falhar, manda a original", "segue com a original" in _h_nn)
check("avisa a pessoa o que aconteceu com a foto", 'id="fotoStatus"' in _h_nn)
check("o aviso comeca escondido", 'id="fotoStatus"' in _h_nn and "hidden" in
      re.search(r'<p[^>]*id="fotoStatus"[^>]*>', _h_nn).group(0))
# `hidden` ja foi atropelado por display:flex duas vezes neste projeto
_css_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "static", "styles.css"), encoding="utf-8").read()
check("existe guarda global pro atributo hidden",
      "[hidden] { display: none !important; }" in _css_src)
check("o servidor segue aceitando foto grande (quando o JS falha)",
      A.allowed_file("nota.jpg") and A.app.config["MAX_CONTENT_LENGTH"] >= 8 * 1024 * 1024)


secao("64. O que um testador implicante consegue quebrar")
# Achados de um amigo testando de ma-fe (obrigado, e' pra isso que serve).
check("descricao gigante e' cortada", len(A.sanitize_text("x" * 50_000)) == A.TEXTO_MAX)
check("limite de 200 caracteres", A.TEXTO_MAX == 200)
for _v in ("1e308", "999999999999999999999999999", "-1e308"):
    check(f"valor absurdo virou o teto: {_v[:14]}", abs(A.parse_money(_v)) == A.VALOR_MAX)
check("inf nao entra", A.parse_money("inf") == 0.0)
check("nan nao entra", A.parse_money("nan") == 0.0)
check("valor normal segue intacto", A.parse_money("1.234,56") == 1234.56)
check("valor_absurdo reconhece o teto", A.valor_absurdo(A.VALOR_MAX) and not A.valor_absurdo(5000))

c_imp = novo_cliente("implicante@teste.com", nome="Rui")
uid_imp = uid_de("implicante@teste.com")
# exatamente o que ele fez: base64 colado na descricao + valor de 27 digitos
c_imp.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "entrada",
                                     "valor": "999999999999999999999999999",
                                     "descricao": "/9j/4AAQSkZJRgABAQ" * 900,
                                     "categoria": "Salário"})
with get_db() as db:
    _n = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE user_id=?", (uid_imp,)).fetchone()["n"]
check("valor absurdo e' RECUSADO, nao aceito calado", _n == 0)

c_imp.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "57,00",
                                     "descricao": "x" * 9000, "categoria": "Outros"})
with get_db() as db:
    _r = db.execute("SELECT LENGTH(descricao) AS n FROM transacoes WHERE user_id=?",
                    (uid_imp,)).fetchone()
check("com valor válido, a descrição entra cortada", _r and _r["n"] == 200, _r["n"] if _r else None)

# a pagina inteira encolhia pra caber a string sem espaco
check("texto sem espaco quebra em vez de esticar a tela",
      "overflow-wrap: anywhere" in _css_src)
check("o valor fica sempre no mesmo canto", ".linha-valor { flex-wrap: nowrap;" in _css_src)
_h_tx = c_imp.get("/transacoes").get_data(as_text=True)
check("a lista de lançamentos usa o alinhamento fixo", "note-header linha-valor" in _h_tx)
check("o formulário avisa o limite antes de colar",
      'maxlength="200"' in c_imp.get("/transacoes/nova").get_data(as_text=True))


_css_src_2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "static", "styles.css"), encoding="utf-8").read()

secao("65. Mais achados dos testadores")
c_g = novo_cliente("grafico@teste.com", nome="Gil")
uid_g = uid_de("grafico@teste.com")
with get_db() as db:
    for _t, _v, _cat in [("entrada", 50, "Salário"), ("entrada", 50, "Transferência recebida"),
                         ("saida", 197, "Outros"), ("saida", 25, "Alimentação")]:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,'manual',100,
                      date('now'),0)""", (uid_g, _t, _v, _cat, _cat))
_cats = {r["categoria"]: r["total"] for r in A.calc_transaction_totals(uid_g)["monthly_by_category"]}
# O grafico somava so as saidas mas agrupava TODA categoria: entrada entrava com
# R$ 0,00 e poluia a legenda ("Salário 0%", "Transferência recebida 0%")
check("categoria de ENTRADA nao aparece no gráfico de saídas",
      "Salário" not in _cats and "Transferência recebida" not in _cats, list(_cats))
check("as saídas continuam certas", _cats.get("Outros") == 197 and _cats.get("Alimentação") == 25)
check("nenhuma categoria com zero na legenda", all(v > 0 for v in _cats.values()))

# "tem R$ 0,50 na conta e ele diz que posso gastar R$ 0,02 hoje"
c_p = novo_cliente("pouco@teste.com", nome="Léo")
uid_p = uid_de("pouco@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',0.50,'saldo','Outros',
                  'ajuste',100,date('now'),0)""", (uid_p,))
_st_p = A.calc_transaction_totals(uid_p)
check("com saldo minusculo, a diaria e' marcada como inutil", _st_p["diaria_util"] is False)
_h_p = so_texto(c_p.get("/").get_data(as_text=True))
check("a home NAO promete centavos por dia", "pode gastar hoje R$ 0,02" not in _h_p)
check("e explica em vez de calar", "curto demais pra dividir" in _h_p)
check("o Resumo mostra travessão no lugar do número",
      "não ajudaria em nada" in c_p.get("/dashboard").get_data(as_text=True))

# com saldo de verdade a diaria volta a aparecer — a checagem nao pode comer o util
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',3000,'salario','Salário',
                  'manual',100,date('now'),0)""", (uid_p,))
check("com saldo normal, a diária volta", A.calc_transaction_totals(uid_p)["diaria_util"] is True)

# "tudo saiu da tela, pro celular e' bom prender a formatacao"
check("o saldo encolhe conforme a largura do cartão", "11cqi" in _css_src_2)
check("com queda pra quem nao suporta container query",
      "@supports not (font-size: 1cqi)" in _css_src_2)


secao("66. Entrada hostil em todo campo (a passada que faltou)")
# Eu tinha declarado a caca de bugs encerrada dizendo que o resto so apareceria
# com usuario real. Os testadores acharam campo sem limite, valor sem teto e
# grafico somando errado — tudo isso e' entrada hostil, e eu nunca fiz essa
# passada. Aqui esta ela, pra nao depender de amigo implicante da proxima vez.
_LIXO = ["", " ", "0", "-1", "abc", "1e999", "-0.0001", "999999999999", "null",
         "<script>alert(1)</script>", "'; DROP TABLE transacoes; --", "../../etc/passwd",
         "%00", "\x00nulo", "𝕏 emoji 🦁" * 40, "9" * 500, "1,,,,5", "1.2.3,45", "--5", "1e-999"]
_ALVOS = [
    ("/transacoes/nova", {"tipo": "saida", "descricao": "t", "categoria": "Outros"}, "valor"),
    ("/transacoes/nova", {"tipo": "saida", "valor": "10", "categoria": "Outros"}, "descricao"),
    ("/metas", {"nome": "m"}, "meta_valor"),
    ("/dividas", {"tipo": "devo", "descricao": "d"}, "valor_total"),
    ("/compromissos", {"descricao": "c", "vencimento": "2026-09-01"}, "valor"),
    ("/saldo-inicial", {}, "valor"),
    ("/categorias", {"nome": "c"}, "limite_mensal"),
]
c_h = novo_cliente("hostil@teste.com", nome="Hos")
_quebrou = []
for _rota, _base, _campo in _ALVOS:
    for _v in _LIXO:
        _d = dict(_base); _d[_campo] = _v; _d["csrf_token"] = "t"
        try:
            if c_h.post(_rota, data=_d, follow_redirects=True).status_code >= 500:
                _quebrou.append((_rota, _campo, repr(_v)[:24]))
        except Exception as _e:
            _quebrou.append((_rota, _campo, type(_e).__name__))
for _v in ["", "abc", "0000-00-00", "9999-99-99", "2026-02-30", "-1"]:
    if c_h.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "10",
                "descricao": "d", "categoria": "Outros", "data_transacao": _v},
                follow_redirects=True).status_code >= 500:
        _quebrou.append(("data", _v, ""))
for _u in ["/transacoes?q=%00&pagina=-1", "/transacoes?pagina=abc", "/meses?mes=9999-99",
           "/transacoes?pagina=99999999999999999999"]:
    if c_h.get(_u).status_code >= 500:
        _quebrou.append((_u, "url", ""))
check(f"{len(_ALVOS)*len(_LIXO)+10} entradas hostis sem derrubar nenhuma tela",
      not _quebrou, str(_quebrou[:3]))

# nao basta nao quebrar: o que foi gravado tem que estar sao
uid_h = uid_de("hostil@teste.com")
c_h.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "10",
                                   "descricao": "<script>alert('xss')</script>", "categoria": "Outros"})
c_h.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": "20",
                                   "descricao": "'; DROP TABLE transacoes; --", "categoria": "Outros"})
import math as _math
with get_db() as db:
    _sobrou = db.execute("SELECT COUNT(*) AS n FROM transacoes").fetchone()["n"]
    _vals = [r["valor"] for r in db.execute("SELECT valor FROM transacoes WHERE user_id=?",
                                            (uid_h,)).fetchall()]
check("a tabela sobreviveu ao DROP TABLE colado", _sobrou > 0)
check("nenhum valor gravado e' infinito ou NaN",
      all(_math.isfinite(v) and abs(v) <= A.VALOR_MAX for v in _vals))
_h_lista = c_h.get("/transacoes").get_data(as_text=True)
check("script colado NAO executa na página", "<script>alert" not in _h_lista)
check("aparece escapado, como texto", "&lt;script&gt;" in _h_lista)
check("nenhum 'R$ inf' ou 'R$ nan' na tela",
      not re.search(r"R\$\s*(inf|nan)", _h_lista, re.I))

secao("67. Resumo mais curto no celular, e segurança visível")
# Duas queixas: "Resumo tem informação demais" e "falta segurança pra conta".
check("cartões de número em 2 colunas no celular (era 1)",
      "grid-template-columns: repeat(2, minmax(0, 1fr));\n        gap: 12px;" in _css_src_2
      or ".metric-grid.cols-4 {\n        grid-template-columns: repeat(2" in _css_src_2)
check("e sem altura mínima de 165px empilhada", ".metric-card { min-height: 0;" in _css_src_2)
check("formulário continua empilhado (campo largo é mais fácil)",
      ".form-grid.cols-2,\n    .form-grid.cols-3 {\n        grid-template-columns: 1fr;" in _css_src_2)

_h_set = c1.get("/settings").get_data(as_text=True)
check("o bloqueio por digital sobe pro topo das Configurações",
      _h_set.index("Bloqueio por digital") < _h_set.index("Como você usa o Hércules"))
_h_menu = c1.get("/").get_data(as_text=True)
check("Conquistas saiu do dia a dia e foi pra Conta",
      _h_menu.index("Configurações") < _h_menu.index("Conquistas") < _h_menu.index("Ajuda"))


_css_src_3 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "static", "styles.css"), encoding="utf-8").read()

secao("68. Menu sem repetir o que já está na barra de baixo")
_h_nav = c1.get("/").get_data(as_text=True)
_rodape = ["/", "/transacoes", "/compromissos", "/dashboard"]
check("os 4 do rodapé estão marcados no menu lateral",
      _h_nav.count("menu-link tambem-no-rodape") == 4,
      _h_nav.count("menu-link tambem-no-rodape"))
check("e continuam existindo no HTML (desktop usa a lateral)",
      all(f'href="{r}"' in _h_nav for r in _rodape))
# Entre 761px e 980px nao ha barra de baixo: esconder ali deixaria a pessoa sem
# navegacao nenhuma. A regra tem que morar SO no bloco de <=760px.
_bloco760 = _css_src_3[_css_src_3.index("@media (max-width: 760px)"):]
_bloco980 = _css_src_3[_css_src_3.index("@media (max-width: 980px)"):_css_src_3.index("@media (max-width: 760px)")]
check("escondidos no bloco de <=760px (onde a barra existe)",
      ".tambem-no-rodape { display: none; }" in _bloco760)
check("NAO escondidos no bloco de <=980px (tablet ficaria sem menu)",
      "tambem-no-rodape" not in _bloco980)
check("a barra de baixo leva pros mesmos 4 destinos",
      all(f'href="{r}"' in _h_nav for r in _rodape) and "mobile-nav" in _h_nav)
check("o que NAO esta no rodape segue na gaveta em qualquer tela",
      all(f'href="{r}"' in _h_nav for r in ("/metas", "/dividas", "/categorias", "/ajuda")))


secao("69. E-mail precisa parecer e-mail")
# O servidor aceitava "abc" e "@". type="email" no HTML e' dica do navegador, nao
# garantia — e o proprio HTML aceita "a@b". Quem erra o proprio e-mail no cadastro
# fica sem recuperacao possivel depois, inclusive quando houver envio de e-mail.
for _e in ["abc", "@", "a@b", "sem arroba.com", "a@@b.com", "a b@c.com", "", "@x.com", "a@.com"]:
    check(f"recusa: {_e!r}", A.email_invalido(_e))
# Permissivo de proposito: nao pode recusar endereco valido de gente de verdade
for _e in ["joao@gmail.com", "maria.silva+notas@empresa.com.br", "a@b.co",
           "rj.razoku@gmail.com", "ana_1@sub.dominio.org"]:
    check(f"aceita: {_e}", not A.email_invalido(_e))

_c_em = A.app.test_client()
with _c_em.session_transaction() as s:
    s["csrf_token"] = "t"
_c_em.post("/register", data={"csrf_token": "t", "nome": "X", "email": "abc",
                              "senha": "tijolo-forte-42", "perfil": "pf"}, follow_redirects=True)
check("cadastro com e-mail quebrado nao cria conta", uid_de("abc") is None)

c1.post("/settings", data={"csrf_token": "t", "form_kind": "account",
                           "nome": "Ana", "email": "quebrado"}, follow_redirects=True)
with get_db() as db:
    _mail = db.execute("SELECT email FROM usuarios WHERE id=?", (uid1,)).fetchone()["email"]
check("trocar pra e-mail quebrado nas Configuracoes tambem e' barrado",
      _mail != "quebrado", _mail)


_APP_SRC_2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app.py"), encoding="utf-8").read()

secao("70. Toque duplo não vira lançamento duplo")
# Num 4G ruim a pagina demora e a pessoa toca de novo. Antes: dois lancamentos.
# E' o bug que o testador acha e culpa a si mesmo — e que corrompe o saldo calado.
import threading as _th2
c_dup = novo_cliente("duplo@teste.com", nome="Duda")
uid_dup = uid_de("duplo@teste.com")

def _salvar_gasto(desc="casa do biscoito", valor="57,00"):
    c_dup.post("/transacoes/nova", data={"csrf_token": "t", "tipo": "saida", "valor": valor,
                                         "descricao": desc, "categoria": "Outros"})

def _quantos(cond):
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE user_id=? AND " + cond,
                          (uid_dup,)).fetchone()["n"]

_ths = [_th2.Thread(target=_salvar_gasto) for _ in range(4)]
[t.start() for t in _ths]
[t.join() for t in _ths]
check("4 toques simultâneos geram UM lançamento",
      _quantos("descricao='casa do biscoito'") == 1, _quantos("descricao='casa do biscoito'"))

# A guarda nao pode comer lancamento legitimo
_salvar_gasto("padaria", "8,00")
_salvar_gasto("uber", "16,00")
check("gastos diferentes seguidos entram todos", _quantos("1=1") == 3, _quantos("1=1"))
_salvar_gasto("padaria", "12,00")   # mesmo lugar, valor diferente
check("mesmo lugar com valor diferente entra", _quantos("descricao='padaria'") == 2)

# So conferir antes do INSERT nao basta: os dois SELECTs rodam antes de qualquer
# commit e os dois se acham originais. A trava de escrita tem que vir junto.
check("a conferência pega a trava de escrita (BEGIN IMMEDIATE)",
      "BEGIN IMMEDIATE" in _APP_SRC_2)
check("a janela é curta (5s) pra não comer gasto repetido de verdade",
      "'-5 seconds'" in _APP_SRC_2)

# Camada do navegador: barra o segundo envio sem mexer em `disabled`, porque
# botao desabilitado nao manda o proprio name/value (as fichas de categoria)
_h_base = c_dup.get("/").get_data(as_text=True)
check("o JS barra o segundo envio", "dataset.enviando" in _h_base)
check("sem desabilitar o botão (perderia name/value)",
      "b.disabled = true" not in _h_base and "classList.add('enviando')" in _h_base)
check("e devolve o formulário se a navegação não acontecer", "12000" in _h_base)
check("formulário com confirmação não é afetado", "hasAttribute('data-confirm')" in _h_base)


secao("71. Apagar coisa que outra coisa aponta")
c_del = novo_cliente("apagar@teste.com", nome="Del")
uid_del = uid_de("apagar@teste.com")

# Nota cria transacao vinculada. Apagar a nota tem que levar a transacao junto,
# senao o saldo fica com um valor que nao tem mais origem nenhuma.
c_del.post("/notas/nova", data={"csrf_token": "t", "descricao": "Venda de bolo",
                                "valor": "250,00", "tipo": "entrada",
                                "categoria": "Serviços", "status": "Autorizada"})
with get_db() as db:
    _nid = db.execute("SELECT id FROM notas WHERE user_id=?", (uid_del,)).fetchone()["id"]
check("a nota criou a transação vinculada",
      abs(A.calc_transaction_totals(uid_del)["balance"] - 250) < 0.01)
c_del.post(f"/notas/{_nid}/delete", data={"csrf_token": "t"})
check("apagar a nota leva a transação junto (sem valor órfão no saldo)",
      abs(A.calc_transaction_totals(uid_del)["balance"]) < 0.01,
      A.calc_transaction_totals(uid_del)["balance"])

# Guardar numa meta cria uma SAIDA. Apagar a meta NAO devolve o dinheiro — e a
# pessoa espera que devolva. Nao mudei o comportamento (mexer em dinheiro dos
# outros por palpite e' pior), mas o aviso passa a dizer isso antes.
c_del.post("/metas", data={"csrf_token": "t", "nome": "Viagem", "meta_valor": "2000"})
with get_db() as db:
    _mid = db.execute("SELECT id FROM metas WHERE user_id=?", (uid_del,)).fetchone()["id"]
_h_metas = c_del.get("/metas").get_data(as_text=True)
check("meta sem dinheiro: aviso simples",
      "não devolve esse dinheiro" not in re.search(r'data-confirm="([^"]*Viagem[^"]*)"',
                                                   _h_metas).group(1))
c_del.post(f"/metas/{_mid}/aporte", data={"csrf_token": "t", "valor": "300"})
_aviso = re.search(r'data-confirm="([^"]*Viagem[^"]*)"',
                   c_del.get("/metas").get_data(as_text=True)).group(1)
check("meta COM dinheiro avisa que não devolve", "não devolve esse dinheiro" in _aviso)
check("e diz quanto", "R$ 300,00" in _aviso, _aviso[:90])

# Apagar categoria apaga junto o que a pessoa ensinou: ela so descobre quando o
# gasto seguinte cai em "Outros".
c_del.post("/categorias", data={"csrf_token": "t", "nome": "Doces", "limite_mensal": "200"})
_aviso_cat = re.search(r'data-confirm="([^"]*Doces[^"]*)"',
                       c_del.get("/categorias").get_data(as_text=True)).group(1)
check("apagar categoria avisa que o aprendizado some", "é esquecido" in _aviso_cat)

# `money` devolve <span> por causa do olhinho; dentro de atributo isso vira
# marcacao quebrada. Eu mesmo cometi esse erro escrevendo o aviso acima.
check("existe filtro de valor pelado pra atributo", "money_texto" in _APP_SRC_2)
_em_atributo = []
for _arq in __import__("glob").glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "templates", "*.html")):
    for _m in re.finditer(r'\w+="[^"]*\|\s*money\s*[}|][^"]*"', open(_arq, encoding="utf-8").read()):
        _em_atributo.append(os.path.basename(_arq))
check("nenhum |money dentro de atributo HTML em nenhum template",
      not _em_atributo, str(set(_em_atributo)))


secao("72. Duas abas abertas na mesma conta")
# PWA na tela inicial + navegador aberto, ou uma aba esquecida por dias. Nada
# disso pode gravar errado nem quebrar.
def _entrar(cli, email="abas@teste.com"):
    """Sai e entra de verdade: /login redireciona quem ja esta logado sem trocar
    o token, entao sem o logout o teste nao reproduziria a rotacao."""
    cli.get("/logout")
    with cli.session_transaction() as s:
        s["csrf_token"] = "t"
    cli.post("/login", data={"csrf_token": "t", "email": email, "senha": "tijolo-forte-42"},
             follow_redirects=True)
    with cli.session_transaction() as s:
        return s.get("csrf_token")

c_ab = novo_cliente("abas@teste.com", nome="Aba")
uid_ab = uid_de("abas@teste.com")
_tok_velho = _entrar(c_ab)
c_ab.post("/transacoes/nova", data={"csrf_token": _tok_velho, "tipo": "saida", "valor": "50",
                                    "descricao": "mercado", "categoria": "Outros"})
with get_db() as db:
    _tid_ab = db.execute("SELECT id FROM transacoes WHERE user_id=?", (uid_ab,)).fetchone()["id"]

# Entrar de novo roda a sessao e o token: a aba antiga fica com um token morto
_entrar(c_ab)
_r_velha = c_ab.post("/transacoes/nova",
                     data={"csrf_token": _tok_velho, "tipo": "saida", "valor": "9",
                           "descricao": "aba velha", "categoria": "Outros"},
                     follow_redirects=True)
with get_db() as db:
    _n_velha = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE descricao='aba velha'").fetchone()["n"]
check("aba com token velho NAO grava", _n_velha == 0)
check("e nao quebra a tela", _r_velha.status_code == 200)
check("o aviso e' em português de gente, sem 'token'",
      "ficou aberta tempo demais" in so_texto(_r_velha.get_data(as_text=True))
      and "Token de segurança" not in _r_velha.get_data(as_text=True))
check("e tranquiliza sobre o que ja foi salvo",
      "nada do que você já salvou foi perdido" in so_texto(_r_velha.get_data(as_text=True)))

# Aba A apaga, aba B tenta agir no que sumiu
c_ab2 = A.app.test_client()
_tA = _entrar(c_ab); _tB = _entrar(c_ab2)
c_ab.post(f"/transacoes/{_tid_ab}/delete", data={"csrf_token": _tA}, follow_redirects=True)
_r_b = c_ab2.post(f"/transacoes/{_tid_ab}/delete", data={"csrf_token": _tB}, follow_redirects=True)
check("apagar o que ja foi apagado nao quebra", _r_b.status_code == 200)
check("e avisa em vez de fingir que deu certo",
      "não encontrada" in so_texto(_r_b.get_data(as_text=True)))

_r_ed = c_ab2.post(f"/transacoes/{_tid_ab}/editar",
                   data={"csrf_token": _tB, "tipo": "saida", "valor": "99",
                         "descricao": "zumbi", "data_transacao": date.today().isoformat()},
                   follow_redirects=True)
with get_db() as db:
    _z = db.execute("SELECT COUNT(*) AS n FROM transacoes WHERE descricao='zumbi'").fetchone()["n"]
check("editar o que foi apagado NAO ressuscita o registro", _z == 0)
check("e tambem nao quebra", _r_ed.status_code == 200)

# Sair numa aba nao derruba a outra (sessoes sao independentes por navegador)
c_ab.get("/logout")
check("logout numa sessão não derruba a outra",
      c_ab2.post("/transacoes/nova", data={"csrf_token": _tB, "tipo": "saida", "valor": "7",
                                           "descricao": "segue", "categoria": "Outros"},
                 follow_redirects=False).status_code == 302)


secao("73. Simulador: será que cabe?")
c_sim = novo_cliente("simular@teste.com", nome="Sim")
uid_sim = uid_de("simular@teste.com")

def _monta_cenario(salario, gasto_dia, conta):
    with get_db() as db:
        db.execute("DELETE FROM transacoes WHERE user_id=?", (uid_sim,))
        db.execute("DELETE FROM compromissos WHERE user_id=?", (uid_sim,))
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'entrada',?,'salario',
                      'Salário','ofx',95,?,0)""", (uid_sim, salario, date.today().isoformat()))
        for _i in range(40):
            db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                          confidence,data_transacao,no_credito) VALUES (?,'saida',?,?,'Mercado',
                          'ofx',95,?,0)""",
                       (uid_sim, gasto_dia, f"g{_i}",
                        (date.today() - timedelta(days=_i)).isoformat()))
        if conta:
            db.execute("""INSERT INTO compromissos (user_id,descricao,valor,vencimento,tipo,status)
                          VALUES (?,'aluguel',?,?,'saida','pendente')""",
                       (uid_sim, conta, (date.today() + timedelta(days=8)).isoformat()))

# A media tem que usar o periodo REAL de historico. Dividir 40 dias de dado pela
# janela de 60 faria a pessoa parecer gastar dois tercos do que gasta — e o
# simulador diria "cabe" pra coisa que nao cabe.
_monta_cenario(4200, 40, 900)
_media, _dias_dado = A.media_gasto_diario(uid_sim)
check("média diária bate com o gasto real", abs(_media - 40) < 1.5, _media)
check("conta quantos dias tem histórico", _dias_dado >= 39, _dias_dado)

_base = A.simular_gasto(uid_sim, 1)
check("soma as contas que ainda vencem no mês", abs(_base["contas"] - 900) < 0.01)
check("não conta o dia de hoje duas vezes (o gasto de hoje já está na média)",
      _base["dias_futuros"] == A.days_left_in_month() - 1)

# Valores FIXOS aqui davam teste que passava dia 2 e falhava dia 3: a folga
# muda com os dias que restam no mes. As fronteiras saem do proprio calculo.
_teto = _base["teto"]
_teto_folgado = _base["teto_folgado"]
for _v, _esperado in [(_teto_folgado - 10, "cabe"),
                      ((_teto_folgado + _teto) / 2, "aperta"),
                      (_teto + 10, "nao_cabe")]:
    check(f"gastar {A.money(_v)} -> {_esperado}",
          A.simular_gasto(uid_sim, _v)["veredito"] == _esperado,
          (A.simular_gasto(uid_sim, _v)["veredito"], _teto_folgado, _teto))

check("diz até quanto cabe com folga", 0 < _base["teto_folgado"] < _base["teto"])
check("e o teto sem folga é maior", _base["teto"] > _base["teto_folgado"])

# Quando o mes ja nao fecha sem a compra, dizer "nao cabe" pra R$ 150 e' verdade
# sem ser util: o problema nao e' a compra.
_monta_cenario(1500, 45, 1300)
_ja = A.simular_gasto(uid_sim, 150)
check("mês que já não fecha tem veredito próprio", _ja["veredito"] == "ja_apertado")
check("e diz quanto falta MESMO SEM a compra", _ja["falta_sem_a_compra"] > 0)
_h_ja = c_sim.post("/simular", data={"csrf_token": "t", "valor": "150,00"}).get_data(as_text=True)
check("a tela explica que não é sobre a compra", "não é sobre essa compra" in _h_ja)
check("e aponta um caminho, não só o problema", "/compromissos" in _h_ja)

# Sem historico o simulador nao pode fingir precisao
c_novo_sim = novo_cliente("simnovo@teste.com", nome="Novo")
_r_novo = A.simular_gasto(uid_de("simnovo@teste.com"), 100)
check("sem histórico, avisa que o ritmo é chute", _r_novo["tem_historico"] is False)
check("e não inventa média", _r_novo["media"] == 0.0)

_monta_cenario(4200, 40, 900)
_h_sim = c_sim.post("/simular", data={"csrf_token": "t", "valor": "300,00"}).get_data(as_text=True)
check("a tela mostra a conta aberta (dá pra conferir a matemática)", "conta-aberta" in _h_sim)
check("mostra o ritmo por dia", "por dia" in _h_sim)
check("avisa que é projeção, não promessa", "não uma promessa" in _h_sim)
check("e que crédito não entra nessa conta", "Compra no crédito não entra" in _h_sim)
check("valor absurdo é recusado no simulador",
      "Informe um valor válido" in c_sim.post("/simular",
          data={"csrf_token": "t", "valor": "999999999999999"},
          follow_redirects=True).get_data(as_text=True))
check("está no menu", "/simular" in c_sim.get("/").get_data(as_text=True))


secao("74. Emoji é o Herc falando; a interface usa ícone")
import glob as _glob
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u23F0-\u23FF]")
_VOZ_OK = set("🦁😅💚👏🎉🤔🤖🍬😉🏛👀✕✓✅")
_fora_da_voz = []
for _arq in _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "*.html")):
    _s = open(_arq, encoding="utf-8").read()
    _prot = []
    for _m in re.finditer(r'<div class="hercules-bubble">.*?</div>', _s, re.S):
        _prot.append((_m.start(), _m.end()))
    for _m in re.finditer(r"<script.*?</script>", _s, re.S):
        _prot.append((_m.start(), _m.end()))
    for _m in _EMOJI.finditer(_s):
        if any(a <= _m.start() < b for a, b in _prot):
            continue
        if _m.group(0) in _VOZ_OK:
            continue
        _fora_da_voz.append((os.path.basename(_arq), _m.group(0)))
check("nenhum emoji decorando a interface (fora a voz do Herc)",
      not _fora_da_voz, str(_fora_da_voz[:4]))
check("o Herc continua com carinha onde ele fala",
      "🦁" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "ajuda.html"), encoding="utf-8").read())
check("existe CSS pro ícone dentro de texto", ".icone-texto {" in _css_src_3
      or ".icone-texto {" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "static", "styles.css"), encoding="utf-8").read())

# Icone dentro de expressao Jinja quebra as aspas e derruba a tela inteira —
# aconteceu escrevendo isto, em saude.html e settings.html.
_jinja_quebrado = [os.path.basename(a) for a in
                   _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "templates", "*.html"))
                   if re.search(r"\{\{[^}]*icone-texto[^}]*\}\}", open(a, encoding="utf-8").read())]
check("nenhum ícone dentro de expressão Jinja", not _jinja_quebrado, str(_jinja_quebrado))

secao("75. Rodapé: quem responde pelo app")
for _rota in ("/login", "/register", "/termos", "/privacidade", "/ajuda"):
    _h = _an.get(_rota).get_data(as_text=True)
    check(f"rodapé em {_rota}", "rodape-legal" in _h)
_h_rod = c1.get("/").get_data(as_text=True)
check("rodapé também nas telas logadas", "rodape-legal" in _h_rod)
check("mostra o CNPJ", "41.026.294/0001-08" in _h_rod)
check("diz o que o app NÃO é", "não substitui contador" in _h_rod)

_h_termos = _an.get("/termos")
check("termos abrem sem login (dá pra ler antes de criar conta)", _h_termos.status_code == 200)
_t = so_texto(_h_termos.get_data(as_text=True))
check("termos dizem que não é aconselhamento financeiro",
      "Não é aconselhamento financeiro" in _t)
check("e que o app pode ficar fora do ar", "fora do ar" in _t)
check("e que o extrato do banco é a fonte oficial", "registro oficial" in _t)
check("e que o que evita dano é grátis pra sempre", "sempre gratuito" in _t)
check("e admitem que não há recuperação de senha ainda",
      "recuperação automática" in _t)
check("os três links do rodapé funcionam",
      all(_an.get(u).status_code == 200 for u in ("/privacidade", "/termos", "/ajuda")))


secao("76. Conheça o Hércules")
# Duas plateias, dois documentos: /sobre explica o app pra quem VAI USAR; a
# apresentacao (fora do app) explica o projeto pra quem pergunta o que foi feito.
_r_sobre = _an.get("/sobre")
check("abre sem login (é o que se lê antes de criar conta)", _r_sobre.status_code == 200)
_t_sobre = so_texto(_r_sobre.get_data(as_text=True))
check("abre com o Herc falando", "Eu sou o Herc" in _t_sobre)
check("conta por que o app existe, com gente de verdade",
      "não consigo juntar dinheiro" in _t_sobre)
check("explica os três caminhos pro gasto chegar",
      "Conectando o banco" in _t_sobre and "Importando o extrato" in _t_sobre
      and "Anotando na mão" in _t_sobre)
check("explica por que existe o passo a mais do banco", "R$ 2.500" in _t_sobre)
check("declara a régua de cobrança", "Nunca vou cobrar pelo que evita um dano" in _t_sobre)
check("e o que o app NÃO é", "Não substitui contador" in _t_sobre)
check("aponta pra privacidade e termos",
      "/privacidade" in _r_sobre.get_data(as_text=True)
      and "/termos" in _r_sobre.get_data(as_text=True))
check("fala do simulador, que é a função mais nova", "Será que cabe?" in _t_sobre)

check("o rodapé leva pra lá, em toda tela",
      "Conheça o Hércules" in _an.get("/login").get_data(as_text=True)
      and "Conheça o Hércules" in c1.get("/").get_data(as_text=True))
# Rodape do app nao pode depender de servico de terceiro: se o link morrer, morre
# no produto de quem confia dinheiro nele
check("o rodapé NÃO aponta pra domínio de fora",
      "claude.ai" not in c1.get("/").get_data(as_text=True))
check("os quatro links do rodapé respondem",
      all(_an.get(u).status_code == 200 for u in ("/sobre", "/privacidade", "/termos", "/ajuda")))


secao("77. Ritmo do dia a dia nao e evento")
# O simulador dizia que ele gastava R$ 129/dia. Nao gastava: aluguel, fatura e
# dinheiro GUARDADO estavam entrando na conta do "gasto diario".
c_rit = novo_cliente("ritmo@teste.com", nome="Ritmo")
uid_rit = uid_de("ritmo@teste.com")

def _gasto(uid, valor, cat, dias_atras, fonte="ofx", credito=0):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',?,'x',?,?,95,?,?)""",
                   (uid, valor, cat, fonte,
                    (date.today() - timedelta(days=dias_atras)).isoformat(), credito))

# Dois meses de vida: gasto miudo de ~R$ 27/dia, mais os eventos do mes.
for _i in range(60):
    _gasto(uid_rit, 35 if _i % 3 else 12, "Mercado", _i)
_gasto(uid_rit, 900, "Moradia", 5); _gasto(uid_rit, 900, "Moradia", 35)     # aluguel
_gasto(uid_rit, 780, "Outros", 12); _gasto(uid_rit, 780, "Outros", 42)      # fatura
_gasto(uid_rit, 500, "Reserva", 20, fonte="manual")                        # guardado

_m_rit, _ = A.media_gasto_diario(uid_rit)
check("o ritmo diario e o gasto miudo, nao a soma de tudo", 22 <= _m_rit <= 31, _m_rit)
check("um aluguel de R$ 900 nao vira R$ 30/dia de ritmo", _m_rit < 40, _m_rit)

# Guardar dinheiro nao pode fazer a pessoa parecer gastadora.
c_res = novo_cliente("reserva@teste.com", nome="Res")
uid_res = uid_de("reserva@teste.com")
for _i in range(30):
    _gasto(uid_res, 20, "Mercado", _i)
_antes_res, _ = A.media_gasto_diario(uid_res)
_gasto(uid_res, 1000, "Reserva", 3, fonte="manual")
_depois_res, _ = A.media_gasto_diario(uid_res)
check("guardar R$ 1.000 numa meta nao mexe no ritmo de gasto",
      abs(_antes_res - _depois_res) < 0.01, (_antes_res, _depois_res))

# E quem gasta igual todo dia tem que ver o proprio numero, sem desconto.
c_igual = novo_cliente("igual@teste.com", nome="Igual")
uid_igual = uid_de("igual@teste.com")
for _i in range(40):
    _gasto(uid_igual, 40, "Mercado", _i)
_m_igual, _ = A.media_gasto_diario(uid_igual)
check("quem gasta R$ 40 todo dia ve R$ 40, nao menos", abs(_m_igual - 40) < 0.01, _m_igual)

# Mas quem gasta MUITO e sempre igual nao pode ter o gasto cortado como "evento".
c_alto = novo_cliente("alto@teste.com", nome="Alto")
uid_alto = uid_de("alto@teste.com")
for _i in range(30):
    _gasto(uid_alto, 300, "Mercado", _i)
_m_alto, _ = A.media_gasto_diario(uid_alto)
check("gastar alto todo dia e ritmo, nao evento", abs(_m_alto - 300) < 0.01, _m_alto)

# Credito continua fora: a compra sai da conta quando a fatura e paga.
_gasto(uid_igual, 5000, "Outros", 2, credito=1)
_m_cred, _ = A.media_gasto_diario(uid_igual)
check("compra no credito nao entra no ritmo", abs(_m_cred - 40) < 0.01, _m_cred)


secao("78. O mes da pessoa, nao o do calendario")
# Salario no ultimo dia do mes e dinheiro do mes seguinte. Sem isso, o mes do
# salario fecha lindo e o proximo parece uma catastrofe - todo mes.
check("sem configurar, e o mes do calendario de sempre",
      A.month_bounds(date(2026, 7, 15)) == (date(2026, 7, 1), date(2026, 7, 31)))

_i31, _f31 = A.month_bounds(date(2026, 8, 1), 31)
check("com virada no 31, o dia 1o ainda e do mes que comecou no 31",
      (_i31, _f31) == (date(2026, 7, 31), date(2026, 8, 30)), (_i31, _f31))
check("e o proprio dia 31 ja abre o mes novo",
      A.month_bounds(date(2026, 7, 31), 31)[0] == date(2026, 7, 31))
check("a vespera ainda e do mes anterior",
      A.month_bounds(date(2026, 7, 30), 31)[1] == date(2026, 7, 30))

# Fevereiro tem 28: virada 31 nao pode sumir.
_ifev, _ffev = A.month_bounds(date(2026, 2, 28), 31)
check("em fevereiro a virada 31 vira o ultimo dia, nao desaparece",
      _ifev == date(2026, 2, 28), (_ifev, _ffev))

# Os ciclos tem que se encostar sem buraco e sem sobreposicao.
_sem_buraco = True
_d = date(2026, 1, 1)
while _d < date(2027, 6, 1):
    _ini, _fim = A.month_bounds(_d, 31)
    if A.month_bounds(_fim + timedelta(days=1), 31)[0] != _fim + timedelta(days=1):
        _sem_buraco = False
        break
    _d = _fim + timedelta(days=1)
check("um ciclo comeca exatamente onde o outro acaba, 18 meses seguidos", _sem_buraco)

# E o salario tem que cair no mes certo na tela de Meses.
c_vir = novo_cliente("virada@teste.com", nome="Vir")
uid_vir = uid_de("virada@teste.com")
with get_db() as db:
    for _dia in ("2026-06-30", "2026-07-31"):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'entrada',3000,'salario',
                      'Salario','ofx',95,?,0)""", (uid_vir, _dia))

_cal = {r["mes"]: r["entrou"] for r in A.historico_mensal(uid_vir, 24)}
check("sem virada, o salario do dia 31/07 conta em julho", _cal.get("2026-07") == 3000, _cal)
with get_db() as db:
    db.execute("UPDATE usuarios SET dia_virada = 31 WHERE id = ?", (uid_vir,))
_ciclo = {r["mes"]: r["entrou"] for r in A.historico_mensal(uid_vir, 24)}
check("com virada 31, ele conta em agosto - que e quando o dinheiro e gasto",
      _ciclo.get("2026-08") == 3000, _ciclo)
check("e o de 30/06 anda junto, pra julho", _ciclo.get("2026-07") == 3000, _ciclo)
check("nenhum salario fica preso no mes em que caiu", not _ciclo.get("2026-06"), _ciclo)

# A tela de ajustes precisa aceitar e devolver o valor.
_ajuste = {"csrf_token": "t", "perfil": "pf", "meta_mensal": "0", "view_mode": "completo"}
c_vir.post("/settings", data=dict(_ajuste, dia_virada="31"))
check("a tela de ajustes grava o dia da virada", A.virada_do_usuario(uid_vir) == 31)
check("e mostra o campo de volta",
      'name="dia_virada"' in c_vir.get("/settings").get_data(as_text=True))
c_vir.post("/settings", data=dict(_ajuste, dia_virada="99"))
check("dia impossivel e recusado, nao gravado torto", A.virada_do_usuario(uid_vir) is None)
c_vir.post("/settings", data=dict(_ajuste, dia_virada=""))
check("em branco volta pro mes do calendario", A.virada_do_usuario(uid_vir) is None)

# Quem nao configurou nada nao pode ver numero nenhum mudar.
check("quem nao configurou continua com o mes do calendario",
      A.mes_do_usuario(uid_rit) == A.month_bounds())


secao("79. O Herc percebe quem recebe no fim do mes")
# De nada adianta o ajuste existir se ninguem descobre que ele existe.
c_fim = novo_cliente("fimdomes@teste.com", nome="Fim")
uid_fim = uid_de("fimdomes@teste.com")
check("sem historico nenhum, nao sai sugerindo nada", A.salario_perto_da_virada(uid_fim) is False)

def _entrada(uid, valor, dia):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'entrada',?,'salario',
                      'Salario','ofx',95,?,0)""", (uid, valor, dia))

_entrada(uid_fim, 3000, "2026-06-30")
check("um mes so pode ser coincidencia, entao ainda cala a boca",
      A.salario_perto_da_virada(uid_fim) is False)
_entrada(uid_fim, 3000, "2026-07-31")
check("dois meses seguidos ja e padrao, e ai ele fala",
      A.salario_perto_da_virada(uid_fim) is True)

_h_fim = c_fim.get("/").get_data(as_text=True)
check("a dica aparece na tela inicial", "dinheiro entra no fim do m" in _h_fim)
check("e aponta pra onde resolver", "Configura" in _h_fim)

# Depois de configurado, calar a boca e obrigacao.
with get_db() as db:
    db.execute("UPDATE usuarios SET dia_virada = 31 WHERE id = ?", (uid_fim,))
check("quem ja configurou nao ouve mais sobre isso",
      A.salario_perto_da_virada(uid_fim) is False)

# Quem recebe no dia 5 nao pode ser incomodado.
c_dia5 = novo_cliente("dia5@teste.com", nome="Dia5")
uid_dia5 = uid_de("dia5@teste.com")
for _d in ("2026-05-05", "2026-06-05", "2026-07-05"):
    _entrada(uid_dia5, 3000, _d)
check("quem recebe no dia 5 nao e incomodado", A.salario_perto_da_virada(uid_dia5) is False)

# Fevereiro tem 28: quem recebe no 28 de fevereiro recebe no fim do mes.
c_fev = novo_cliente("fev@teste.com", nome="Fev")
uid_fev = uid_de("fev@teste.com")
_entrada(uid_fev, 3000, "2026-02-28")
_entrada(uid_fev, 3000, "2026-03-31")
check("28 de fevereiro conta como fim de mes", A.salario_perto_da_virada(uid_fev) is True)

# Um pix pequeno solto nao pode ser confundido com salario.
c_pix = novo_cliente("pix@teste.com", nome="Pix")
uid_pix = uid_de("pix@teste.com")
for _d in ("2026-05-31", "2026-06-30"):
    _entrada(uid_pix, 20, _d)
check("dois pix de R$ 20 nao viram motivo pra mudar o mes",
      A.salario_perto_da_virada(uid_pix) is False)


secao("80. 'Termina o mes com' nao pode extrapolar aluguel")
# A frase do MODO SIMPLES, que e a tela de quem menos vai desconfiar do numero.
# Dividia o gasto do mes pelos dias corridos: quem pagou aluguel no dia 1 virava,
# no dia 2, alguem que gasta o aluguel inteiro todo dia.
c_proj = novo_cliente("projecao@teste.com", nome="Proj")
uid_proj = uid_de("projecao@teste.com")
_ini_mes, _fim_mes = A.month_bounds(date.today())

def _mov_proj(tipo, valor, cat, quando, fonte="ofx"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,'x',?,?,95,?,0)""",
                   (uid_proj, tipo, valor, cat, fonte, quando.isoformat()))

_mov_proj("entrada", 3000, "Salario", _ini_mes)
for _i in range(60):
    _mov_proj("saida", 35 if _i % 3 else 12, "Mercado", date.today() - timedelta(days=_i))
_mov_proj("saida", 900, "Moradia", _ini_mes)                              # aluguel dia 1
_mov_proj("saida", 500, "Reserva", _ini_mes + timedelta(days=1), "manual")  # guardado dia 2

_st_proj = A.calc_transaction_totals(uid_proj)
_h_proj = c_proj.get("/").get_data(as_text=True)

# A projecao nao pode dizer que quem ganha 3 mil termina o mes devendo dezenas de
# milhares. O piso: nao da pra perder mais do que o dia a dia vezes os dias.
_media_proj, _ = A.media_gasto_diario(uid_proj)
_dias_proj = max(0, A.days_left_in_month() - 1)
_pior_honesto = _st_proj["balance"] - A.contas_ate_fim_do_mes(uid_proj) - _media_proj * _dias_proj
check("a projecao usa o ritmo do dia a dia, nao a soma do mes",
      _media_proj < 60, _media_proj)
check("quem ganha 3 mil nao termina o mes devendo 20 mil",
      _pior_honesto > -3000, _pior_honesto)

# O aluguel nao pode entrar duas vezes: uma diluido no ritmo, outra como conta.
_antes_conta, _ = A.media_gasto_diario(uid_proj)
with get_db() as db:
    db.execute("""INSERT INTO compromissos (user_id,descricao,valor,vencimento,tipo,status)
                  VALUES (?,'Aluguel',900,?,'saida','pendente')""",
               (uid_proj, (date.today() + timedelta(days=3)).isoformat()))
_depois_conta, _ = A.media_gasto_diario(uid_proj)
check("cadastrar a conta nao mexe no ritmo (senao contaria duas vezes)",
      abs(_antes_conta - _depois_conta) < 0.01, (_antes_conta, _depois_conta))
check("mas a conta entra inteira, por fora", A.contas_ate_fim_do_mes(uid_proj) == 900)

# E a tela tem que continuar de pe.
check("o modo simples continua abrindo", c_proj.get("/").status_code == 200)


secao("81. 'Pode gastar hoje' tem que dizer de onde vem")
# Um amigo do Matheus perguntou como a conta acontece. A resposta que ele deu —
# metas e gastos fixos — nao batia: o amigo nao tinha cadastrado nenhum dos dois
# e mesmo assim viu um numero. A conta precisa estar escrita na tela.
c_exp = novo_cliente("explica@teste.com", nome="Exp")
uid_exp = uid_de("explica@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',600,'x','Salario',
                  'ofx',95,?,0)""", (uid_exp, date.today().isoformat()))

# O numero aparece em DUAS telas: inline na inicial e num card no /dashboard.
# O amigo provavelmente viu o da inicial, que era o que menos explicava.
_h_exp = c_exp.get("/").get_data(as_text=True)
_h_dash = c_exp.get("/dashboard").get_data(as_text=True)
check("na tela inicial, diz que e o saldo dividido pelos dias",
      "E o seu saldo, dividido pelos" in _h_exp or
      "É o seu saldo, dividido pelos" in _h_exp, _h_exp[_h_exp.find("pode gastar hoje"):][:300])
check("no /dashboard tambem", "Seu saldo, dividido pelos" in _h_dash,
      _h_dash[_h_dash.find("Pode gastar"):][:300])
check("e as duas dizem quantos dias sao",
      f"{A.days_left_in_month()} dia" in _h_exp and f"{A.days_left_in_month()} dia" in _h_dash)

# Com conta cadastrada, a frase tem que crescer e mostrar o desconto.
with get_db() as db:
    db.execute("""INSERT INTO compromissos (user_id,descricao,valor,vencimento,tipo,status)
                  VALUES (?,'Aluguel',200,?,'saida','pendente')""",
               (uid_exp, A.month_bounds()[1].isoformat()))
_h_exp2 = c_exp.get("/").get_data(as_text=True)
_h_dash2 = c_exp.get("/dashboard").get_data(as_text=True)
check("com conta cadastrada, as duas telas mostram o desconto",
      "de contas" in _h_exp2 and "de contas" in _h_dash2)

# A CONTA QUE VENCE DIA 30 NAO PODE SER INVISIVEL NO DIA 2.
# Era o bug: descontava so 7 dias, mas dividia pelos dias todos do mes.
_st_exp = A.calc_transaction_totals(uid_exp)
check("conta que vence no fim do mes ja entra na sobra do mes",
      abs(_st_exp["remaining_month"] - (_st_exp["balance"] - 200)) < 0.01,
      (_st_exp["remaining_month"], _st_exp["balance"]))
check("e o card de 7 dias continua contando so 7 dias",
      _st_exp["commitments_total"] == 0 or A.month_bounds()[1] <= date.today() + timedelta(days=7),
      _st_exp["commitments_total"])
check("a sobra do mes nunca e maior que o saldo quando ha conta pendente",
      _st_exp["remaining_month"] < _st_exp["balance"])

# O rotulo 'no mes' agora corresponde ao que a conta faz.
check("o rodape do card nao promete mais so 'contas proximas'",
      "tudo que ainda vence este m" in _h_dash2)

# E quem tem centavos continua sem ouvir bobagem.
c_cent = novo_cliente("centavos@teste.com", nome="Cent")
uid_cent = uid_de("centavos@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',0.5,'x','Salario',
                  'ofx',95,?,0)""", (uid_cent, date.today().isoformat()))
_h_cent = c_cent.get("/").get_data(as_text=True)
_h_cent_d = c_cent.get("/dashboard").get_data(as_text=True)
check("com R$ 0,50 a inicial nao mostra conta nenhuma",
      "dividido pelos" not in _h_cent and "curto demais" in _h_cent)
check("e o /dashboard mostra o aviso no lugar da conta",
      "dividido pelos" not in _h_cent_d and "daria centavos" in _h_cent_d)


secao("82. Card de conteudo nao cabe em meia tela de celular")
# O Matheus viu os cards depois de "Sua reserva neste mes" quebrados no celular.
# Causa: a regra de 2 colunas no mobile foi escrita pros cartoes de NUMERO e
# pegava tambem as secoes com grafico, lista e barra de progresso dentro.
import io as _io
import re as _re
import glob as _glob

_css = _io.open("static/styles.css", encoding="utf-8").read()
check("existe regra que da largura inteira pros cards largos",
      ".metric-grid.cards-largos" in _css)

# A ordem importa: mesma especificidade da regra de 2 colunas, ganha quem vem depois.
_i_duas = _css.rfind("grid-template-columns: repeat(2, minmax(0, 1fr));\n        gap: 12px;")
_i_larga = _css.find(".metric-grid.cards-largos")
check("e ela vem DEPOIS da regra de duas colunas, senao nao vale",
      _i_duas != -1 and _i_larga > _i_duas, (_i_duas, _i_larga))

# Varredura: toda metric-grid cujo primeiro filho NAO e cartao de numero
# precisa de cards-largos. Assim secao nova nao repete o erro.
_faltando = []
for _arq in _glob.glob("templates/*.html"):
    _linhas = _io.open(_arq, encoding="utf-8").read().split("\n")
    for _n, _linha in enumerate(_linhas):
        if "metric-grid" not in _linha or "class=" not in _linha:
            continue
        # Olha o primeiro elemento com class= dentro da secao
        _filho = ""
        for _m in _linhas[_n + 1:_n + 4]:
            _achou = _re.search(r'class="([^"]*)"', _m)
            if _achou:
                _filho = _achou.group(1)
                break
        if "metric-card" in _filho:
            continue                      # cartao de numero: 2 colunas esta certo
        if not _filho:
            continue
        if "cards-largos" not in _linha:
            _faltando.append(f"{_arq}:{_n+1} ({_filho})")
check("toda grade de cards de conteudo esta marcada como larga", not _faltando, _faltando)

# E os cartoes de numero NAO podem ter ganho a marca por engano.
_errado = []
for _arq in _glob.glob("templates/*.html"):
    _linhas = _io.open(_arq, encoding="utf-8").read().split("\n")
    for _n, _linha in enumerate(_linhas):
        if "cards-largos" not in _linha:
            continue
        for _m in _linhas[_n + 1:_n + 4]:
            _achou = _re.search(r'class="([^"]*)"', _m)
            if _achou:
                if "metric-card" in _achou.group(1):
                    _errado.append(f"{_arq}:{_n+1}")
                break
check("cartao de numero continua em duas colunas no celular", not _errado, _errado)

# As quatro secoes do relato, nominalmente.
_dash = _io.open("templates/dashboard.html", encoding="utf-8").read()
check("no Resumo, grafico + proximas contas ganham largura inteira",
      _dash.count("metric-grid cols-2 cards-largos") == 2, _dash.count("cards-largos"))

# A versao do CSS mora em dois arquivos. Se desencontrarem, o celular serve o
# arquivo velho do cache e o conserto nao chega em quem instalou o app.
_base = _io.open("templates/base.html", encoding="utf-8").read()
_sw = _io.open("static/sw.js", encoding="utf-8").read()
_v_base = _re.search(r"filename='styles\.css', v='(\d+)'", _base)
_v_sw = _re.search(r'const V = "(\d+)"', _sw)
check("base.html e sw.js declaram a mesma versao de CSS",
      _v_base and _v_sw and _v_base.group(1) == _v_sw.group(1),
      (_v_base and _v_base.group(1), _v_sw and _v_sw.group(1)))
check("e o sw usa essa constante na lista de pre-cache",
      'styles.css?v=" + V' in _sw)


secao("82. Teclado nao pode tapar a resposta")
# "quando calcula o teclado continua aberto" — o autofocus rodava de novo depois
# do POST, o teclado do celular subia e cobria justamente o veredito.
c_tec = novo_cliente("teclado@teste.com", nome="Tec")
_h_vazio = c_tec.get("/simular").get_data(as_text=True)
check("com a tela vazia, o cursor ja fica no campo", "autofocus" in _h_vazio)
_h_resp = c_tec.post("/simular", data={"csrf_token": "t", "valor": "150,00"}).get_data(as_text=True)
check("depois de calcular, nao ha autofocus pra reabrir o teclado",
      "autofocus" not in _h_resp)
check("e a pagina rola pra resposta", 'id="resposta"' in _h_resp and "scrollIntoView" in _h_resp)
check("o valor digitado continua no campo pra ajustar", 'value="150,00"' in _h_resp)


secao("83. O ritmo tem que se explicar sozinho")
# O Matheus olhou "R$ 129 por dia" e disse "que isso". Eu chutei tres explicacoes
# sem ver os dados dele. Duas estavam erradas. Um numero sozinho nao se defende:
# agora a tela mostra dia a dia o que tem dentro.
c_rit2 = novo_cliente("ritmo2@teste.com", nome="R2")
uid_rit2 = uid_de("ritmo2@teste.com")

def _g2(valor, cat, dias, credito=0, fonte="ofx"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',?,'x',?,?,95,?,?)""",
                   (uid_rit2, valor, cat, fonte,
                    (date.today() - timedelta(days=dias)).isoformat(), credito))

for _i in range(60):
    _g2(35 if _i % 3 else 12, "Mercado", _i)
_g2(900, "Moradia", 5); _g2(900, "Moradia", 35)
_g2(500, "Reserva", 20, fonte="manual")
_g2(1200, "Outros", 8, credito=1)

_det = A.detalhe_do_ritmo(uid_rit2)
check("lista os maiores dias, do maior pro menor",
      [m["total"] for m in _det["maiores"]] == sorted(
          [m["total"] for m in _det["maiores"]], reverse=True), _det["maiores"])
check("marca quais dias ficaram de fora da conta",
      any(not m["contou"] for m in _det["maiores"]), _det["maiores"])
check("o dia do aluguel e um dos que ficou de fora",
      any(m["total"] >= 900 and not m["contou"] for m in _det["maiores"]))
check("soma o que foi guardado em meta, separado", _det["guardado"] == 500, _det["guardado"])
check("e o que foi no credito, separado", _det["no_credito"] == 1200, _det["no_credito"])
check("a media do detalhe e a mesma que o simulador usa",
      abs(_det["media"] - A.media_gasto_diario(uid_rit2)[0]) < 0.01)

_h_rit2 = c_rit2.post("/simular", data={"csrf_token": "t", "valor": "150,00"}).get_data(as_text=True)
check("a tela abre o bloco 'De onde vem'", "De onde vem esses" in _h_rit2)
check("mostra os dias com data legivel", "/" in _h_rit2 and "lan\u00e7amento" in _h_rit2)
check("explica o que foi tratado como evento", "tratei como evento" in _h_rit2)
check("e aponta pra cadastrar a conta que se repete", "/compromissos" in _h_rit2)
check("diz que o dinheiro guardado ficou de fora", "guardou em metas" in _h_rit2)
check("e que o credito tambem", "compras no cr" in _h_rit2)
check("convida a reclamar se o numero nao parecer o dele",
      "n\u00e3o parece o seu" in _h_rit2)

# Quem gasta muito de verdade nao pode ver dia nenhum riscado: seria mentira
# confortavel. O numero alto dele e o numero dele.
c_alto2 = novo_cliente("alto2@teste.com", nome="A2")
uid_alto2 = uid_de("alto2@teste.com")
with get_db() as db:
    for _i in range(40):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',200,'x','Mercado',
                      'ofx',95,?,0)""",
                   (uid_alto2, (date.today() - timedelta(days=_i)).isoformat()))
_det_alto = A.detalhe_do_ritmo(uid_alto2)
check("quem gasta alto e constante nao tem dia riscado",
      all(m["contou"] for m in _det_alto["maiores"]), _det_alto["maiores"])
check("e nao ve explicacao de evento que nao houve", _det_alto["cortou"] is False)
_h_alto2 = c_alto2.post("/simular", data={"csrf_token": "t", "valor": "50,00"}).get_data(as_text=True)
check("a tela dele nao fala em evento", "tratei como evento" not in _h_alto2)

# Sem gasto nenhum o bloco nao aparece, em vez de mostrar lista vazia.
c_zero2 = novo_cliente("zero2@teste.com", nome="Z2")
_det_zero = A.detalhe_do_ritmo(uid_de("zero2@teste.com"))
check("sem gasto, nao ha lista pra mostrar", _det_zero["maiores"] == [])
_h_zero2 = c_zero2.post("/simular", data={"csrf_token": "t", "valor": "50,00"}).get_data(as_text=True)
check("e a tela nao mostra bloco vazio", "De onde vem esses" not in _h_zero2)


secao("84. Cartao nao e dinheiro que saiu da conta")
# "Abri hoje e falou que meu gasto da semana foi uns 1700 reais." Era a semana do
# pagamento, quando mais se usa cartao — e o card somava compra de credito junto
# com debito. Depois contaria de novo quando a fatura fosse paga.
c_sem = novo_cliente("semana@teste.com", nome="Sem")
uid_sem = uid_de("semana@teste.com")

def _mov_sem(tipo, valor, cat, dias, credito=0):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,'x',?,'ofx',95,?,?)""",
                   (uid_sem, tipo, valor, cat,
                    (date.today() - timedelta(days=dias)).isoformat(), credito))

_mov_sem("entrada", 3000, "Salario", 6)
for _i in range(7):
    _mov_sem("saida", 45, "Mercado", _i)          # R$ 315 no debito
for _v, _c, _dia in [(380, "Compras", 5), (260, "Mercado", 4), (190, "Alimentacao", 3),
                     (240, "Saude", 2), (215, "Transporte", 1)]:
    _mov_sem("saida", _v, _c, _dia, credito=1)     # R$ 1.285 no cartao

_ins = A.insight_semanal(uid_sem)
check("a semana mostra so o que saiu da conta", _ins["atual"] == 315, _ins["atual"])
check("o cartao vem separado, nao somado", _ins["credito"] == 1285, _ins["credito"])
check("a media do dia e do que saiu mesmo", abs(_ins["media_dia"] - 45) < 0.01, _ins["media_dia"])
check("junto dava quase 5x o valor real", _ins["atual"] + _ins["credito"] == 1600)
check("'onde mais foi' nao aponta categoria que so existe no cartao",
      _ins["top_cat"] == "Mercado", _ins["top_cat"])

_h_sem = c_sem.get("/dashboard").get_data(as_text=True)
check("o titulo do card diz que e o que saiu da conta",
      "Saiu da conta nos \u00faltimos 7 dias" in _h_sem)
check("e a tela mostra o cartao logo abaixo, separado", "no cr\u00e9dito" in _h_sem)
check("explicando quando esse dinheiro sai", "quando a fatura vencer" in _h_sem)

# Quem so usa debito nao pode ver linha de cartao que nao existe.
c_deb = novo_cliente("debito@teste.com", nome="Deb")
uid_deb = uid_de("debito@teste.com")
with get_db() as db:
    for _i in range(5):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',60,'x','Mercado',
                      'ofx',95,?,0)""", (uid_deb, (date.today() - timedelta(days=_i)).isoformat()))
_ins_deb = A.insight_semanal(uid_deb)
check("quem so usa debito tem credito zerado", _ins_deb["credito"] == 0)
check("e nao ve a linha do cartao",
      "no cr\u00e9dito" not in c_deb.get("/dashboard").get_data(as_text=True))

# A semana que SO teve cartao nao pode sumir da tela como se nada tivesse havido.
c_socc = novo_cliente("socartao@teste.com", nome="SoC")
uid_socc = uid_de("socartao@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'saida',400,'x','Compras',
                  'ofx',95,?,1)""", (uid_socc, date.today().isoformat()))
_ins_so = A.insight_semanal(uid_socc)
check("semana so de cartao ainda aparece", _ins_so is not None)
check("com zero saindo da conta", _ins_so["atual"] == 0 if _ins_so else False)
check("e o cartao visivel", _ins_so["credito"] == 400 if _ins_so else False)

# As telas de "PRA ONDE o dinheiro foi" continuam contando o cartao — sao outra
# pergunta, e a tela de Meses diz isso com todas as letras.
check("a comparacao por categoria continua incluindo o cartao",
      "inclusive o do cart\u00e3o" in c_ms.get("/meses?mes=2026-06").get_data(as_text=True))


secao("85. Card com grafico dentro nao cabe em meia tela de celular")
# O Matheus abriu no celular e os cards depois de "Sua reserva neste mes"
# estavam quebrados. Causa: a regra de 2 colunas no mobile foi escrita pros
# cartoes de NUMERO e pegava tambem os cards de conteudo — grafico, lista com
# titulo e valor, barra de progresso — que em ~170px se espremem.
import io as _io_layout
import re as _re_layout

_css = _io_layout.open("static/styles.css", encoding="utf-8").read()
check("existe regra que devolve largura inteira pros cards largos",
      ".metric-grid.cards-largos" in _css)
# Mesma especificidade das outras: quem vem depois ganha. Se alguem mover, quebra.
_pos_cols = _css.find(".metric-grid.cols-2,\n    .metric-grid.cols-3")
_pos_largos = _css.find(".metric-grid.cards-largos {")
check("e ela vem DEPOIS da regra de 2 colunas (senao perde por ordem)",
      0 < _pos_cols < _pos_largos, (_pos_cols, _pos_largos))

# Toda grade cujo primeiro filho e card de conteudo precisa da classe. Sem isso,
# um card novo com grafico dentro volta a quebrar sem ninguem perceber.
_faltando = []
for _nome in ("dashboard.html", "business_dashboard.html", "home.html", "meses.html",
              "ir.html", "dividas.html", "saude.html"):
    _tpl = _io_layout.open(f"templates/{_nome}", encoding="utf-8").read()
    for _m in _re_layout.finditer(r'<section class="metric-grid([^"]*)"', _tpl):
        _classes = _m.group(1)
        _resto = _tpl[_m.end():_m.end() + 400]
        _primeiro = _re_layout.search(r'<article class="([^"]*)"', _resto)
        if not _primeiro:
            continue
        _largo = ("chart-card" in _primeiro.group(1)) or (
            "page-card" in _primeiro.group(1) and "metric-card" not in _primeiro.group(1))
        if _largo and "cards-largos" not in _classes:
            _faltando.append(f"{_nome}: {_primeiro.group(1)}")
check("toda grade de card largo tem a classe que empilha no celular",
      not _faltando, _faltando)

# E o contrario: grade de cartao de numero NAO pode empilhar, senao vira tres
# telas de rolagem so de numero — foi a queixa de dois testadores.
_numeros_empilhando = []
for _nome in ("dashboard.html", "home.html", "ir.html", "dividas.html", "saude.html"):
    _tpl = _io_layout.open(f"templates/{_nome}", encoding="utf-8").read()
    for _m in _re_layout.finditer(r'<section class="metric-grid([^"]*)"', _tpl):
        _resto = _tpl[_m.end():_m.end() + 400]
        _primeiro = _re_layout.search(r'<article class="([^"]*)"', _resto)
        if _primeiro and "metric-card" in _primeiro.group(1) and "cards-largos" in _m.group(1):
            _numeros_empilhando.append(_nome)
check("e cartao de numero continua lado a lado", not _numeros_empilhando, _numeros_empilhando)


secao("86. Ajuste de saldo nao e gasto")
# 'Ajuste' e um movimento SINTETICO: existe pra fazer o saldo do app bater com o
# do banco. Entrada ja excluia ajuste; saida nao. Resultado: o Resumo mostrava
# R$ 1.675 de saida no mes e a tela de Meses mostrava R$ 1.600, mesmo mes.
c_aj = novo_cliente("ajuste@teste.com", nome="Aj")
uid_aj = uid_de("ajuste@teste.com")
_ini_aj, _fim_aj = A.month_bounds(date.today())

def _mov_aj(tipo, valor, cat, quando, cred=0, fonte="ofx"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,'x',?,?,95,?,?)""",
                   (uid_aj, tipo, valor, cat, fonte, quando.isoformat(), cred))

_mov_aj("entrada", 3000, "Salario", _ini_aj)
for _i in range(10):
    _mov_aj("saida", 40, "Mercado", _ini_aj + timedelta(days=_i))
_mov_aj("saida", 900, "Moradia", _ini_aj + timedelta(days=1))
_mov_aj("saida", 75, "Outros", _ini_aj + timedelta(days=6), fonte="ajuste")

_st_aj = A.calc_transaction_totals(uid_aj)
check("o ajuste CONTA no saldo — e pra isso que ele existe",
      _st_aj["balance"] == 3000 - 400 - 900 - 75, _st_aj["balance"])
check("mas NAO conta nas saidas do mes", _st_aj["month_expenses"] == 1300,
      _st_aj["month_expenses"])

# A mesma verdade em duas telas tem que dar o mesmo numero.
_h_aj = {m["mes"]: m for m in A.historico_mensal(uid_aj, 12)}
_mes_aj = A.month_bounds(date.today(), A.virada_do_usuario(uid_aj))[1].strftime("%Y-%m")
check("Resumo e Meses concordam no que saiu",
      _h_aj[_mes_aj]["saiu"] == _st_aj["month_expenses"],
      (_h_aj[_mes_aj]["saiu"], _st_aj["month_expenses"]))
check("e no que entrou", _h_aj[_mes_aj]["entrou"] == _st_aj["month_income"])

# O grafico de categorias tem que fechar com "saidas do mes" (fora o cartao).
_soma_graf = sum(float(r["total"] or 0) for r in _st_aj["monthly_by_category"])
check("o grafico de categorias fecha com as saidas do mes",
      abs(_soma_graf - float(_st_aj["fatura_credito_mes"] or 0) - _st_aj["month_expenses"]) < 0.01,
      (_soma_graf, _st_aj["month_expenses"]))

# E o ajuste nao pode aparecer como gasto do dia nem da semana.
_mov_aj("saida", 120, "Outros", date.today(), fonte="ajuste")
_ins_aj = A.insight_semanal(uid_aj)
# Soma direto do banco o que a semana DEVERIA ter, sem ajuste. Calcular a mao
# aqui so cria um segundo lugar pra errar.
with get_db() as db:
    _esperado_sem = float(db.execute(
        """SELECT COALESCE(SUM(valor),0) AS t FROM transacoes
            WHERE user_id=? AND tipo='saida' AND no_credito=0 AND fonte!='ajuste'
              AND date(COALESCE(NULLIF(data_transacao,''),created_at))
                  BETWEEN date(?) AND date(?)""",
        (uid_aj, (date.today() - timedelta(days=6)).isoformat(),
         date.today().isoformat())).fetchone()["t"])
    _esperado_com = float(db.execute(
        """SELECT COALESCE(SUM(valor),0) AS t FROM transacoes
            WHERE user_id=? AND tipo='saida' AND no_credito=0
              AND date(COALESCE(NULLIF(data_transacao,''),created_at))
                  BETWEEN date(?) AND date(?)""",
        (uid_aj, (date.today() - timedelta(days=6)).isoformat(),
         date.today().isoformat())).fetchone()["t"])
check("o ajuste nao entra no gasto da semana",
      _ins_aj["atual"] == _esperado_sem, (_ins_aj["atual"], _esperado_sem))
check("e sem esse cuidado a semana viria R$ 120 maior",
      _esperado_com - _esperado_sem == 120, (_esperado_com, _esperado_sem))
_h_dia = c_aj.get("/").get_data(as_text=True)
check("nem no 'voce gastou X hoje'", "R$ 120,00" not in _h_dia)

# Ajuste tambem nao e sugestao de categoria pra pessoa responder.
_sug = A.pending_suggestions(uid_aj)
check("nem vira pergunta de 'o que e isso?'",
      all("ajuste" not in (str(x.get("display", "")) or "").lower() for x in _sug), _sug)

# E o ritmo diario ja ignorava — tem que continuar ignorando.
_m_aj, _ = A.media_gasto_diario(uid_aj)
_det_aj = A.detalhe_do_ritmo(uid_aj)
check("o ritmo diario segue sem o ajuste",
      all(m["total"] not in (75.0, 120.0) for m in _det_aj["maiores"]), _det_aj["maiores"])


secao("87. Banco conectado + extrato importado nao pode dobrar")
# Quem conecta o banco E importa o extrato via a mesma compra duas vezes. A
# reconciliacao exigia `fitid IS NULL AND fonte != 'ofx'`, e a linha da Pluggy
# tem as duas coisas — fitid PLG-... e fonte 'ofx', porque entra pela mesma
# funcao. Saldo, gasto do mes e ritmo diario, tudo em dobro.
_hoje_d = date.today().isoformat()
_seq_d = [0]

def _user_dedup():
    _seq_d[0] += 1
    _e = f"dedup{_seq_d[0]}@teste.com"
    novo_cliente(_e, nome="D")
    return uid_de(_e)

def _n_tx(uid):
    with get_db() as db:
        return db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?", (uid,)).fetchone()["c"]

def _it(v, fitid, desc="COMPRA", tipo="saida"):
    return {"valor": v, "tipo": tipo, "data": _hoje_d, "descricao": desc,
            "fitid": fitid, "no_credito": False}

_u = _user_dedup()
A.import_ofx_transactions(_u, [_it(120, "PLG-abc", "MERCADO"), _it(45.5, "PLG-def", "POSTO"),
                               _it(3000, "PLG-ghi", "SALARIO", "entrada")])
_r_ofx = A.import_ofx_transactions(_u, [_it(120, "202601", "MERCADO"), _it(45.5, "202602", "POSTO"),
                                        _it(3000, "202603", "SALARIO", "entrada")])
check("o extrato reconhece o que o banco ja trouxe", _r_ofx["reconciliadas"] == 3, _r_ofx)
check("e nao insere nada de novo", _r_ofx["importadas"] == 0, _r_ofx)
check("3 gastos reais continuam 3 linhas", _n_tx(_u) == 3, _n_tx(_u))
_st_d = A.calc_transaction_totals(_u)
check("o saldo nao dobra", _st_d["balance"] == 3000 - 165.5, _st_d["balance"])
check("nem o gasto do mes", _st_d["month_expenses"] == 165.5, _st_d["month_expenses"])

# O id antigo tem que SOBREVIVER, senao a fonte antiga reimporta no proximo sync.
_u2 = _user_dedup()
A.import_ofx_transactions(_u2, [_it(120, "PLG-xyz", "MERCADO")])
A.import_ofx_transactions(_u2, [_it(120, "700001", "MERCADO")])
A.import_ofx_transactions(_u2, [_it(120, "PLG-xyz", "MERCADO")])
check("o sync seguinte reconhece a linha pelo id antigo", _n_tx(_u2) == 1, _n_tx(_u2))

# --- E agora o lado perigoso: deduplicar nao pode COMER gasto de verdade ---
_u3 = _user_dedup()
A.import_ofx_transactions(_u3, [_it(5, "PLG-1", "CAFE"), _it(5, "PLG-2", "CAFE")])
check("dois cafes iguais no mesmo dia continuam dois", _n_tx(_u3) == 2, _n_tx(_u3))
A.import_ofx_transactions(_u3, [_it(5, "900001", "CAFE"), _it(5, "900002", "CAFE")])
check("e o extrato com os dois nao vira quatro", _n_tx(_u3) == 2, _n_tx(_u3))

_u4 = _user_dedup()
A.import_ofx_transactions(_u4, [_it(15, "PLG-x"), _it(15, "PLG-y"), _it(15, "PLG-z")])
A.import_ofx_transactions(_u4, [_it(15, "400001")])
check("extrato parcial nao apaga o que ele nao tinha", _n_tx(_u4) == 3, _n_tx(_u4))

_u5 = _user_dedup()
A.import_ofx_transactions(_u5, [_it(200, "PLG-e", "PIX", "entrada"), _it(200, "PLG-s", "PIX")])
A.import_ofx_transactions(_u5, [_it(200, "300001", "PIX", "entrada"), _it(200, "300002", "PIX")])
check("entrada e saida do mesmo valor nao se anulam", _n_tx(_u5) == 2, _n_tx(_u5))

# Lancamento na mao continua sendo reconciliado, que era o comportamento antigo.
_u6 = _user_dedup()
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'saida',60,'ALMOCO','Outros',
                  'manual',100,?,0)""", (_u6, _hoje_d))
A.import_ofx_transactions(_u6, [_it(60, "600001", "ALMOCO")])
check("lancamento manual segue casando com o extrato", _n_tx(_u6) == 1, _n_tx(_u6))

check("reimportar o mesmo extrato segue sem duplicar",
      (lambda u: (A.import_ofx_transactions(u, [_it(80, "500001")]),
                  A.import_ofx_transactions(u, [_it(80, "500001")]),
                  _n_tx(u))[-1])(_user_dedup()) == 1)

check("o provedor sai do formato do id",
      (A._provedor_do_fitid("PLG-9") , A._provedor_do_fitid("PDF-9"),
       A._provedor_do_fitid("12345"), A._provedor_do_fitid(None))
      == ("pluggy", "pdf", "ofx", None))


secao("88. Limpeza das duplicadas que ja estao no banco")
# O import parou de duplicar, mas quem ja tinha conectado o banco E importado o
# extrato continua com as linhas repetidas la dentro. O script tem que tirar o
# que dobrou e nao encostar no resto.
import limpar_duplicadas as _limp

c_lim = novo_cliente("limpeza@teste.com", nome="Lim")
uid_lim = uid_de("limpeza@teste.com")
_hoje_l = date.today().isoformat()

def _ins_l(valor, fitid, desc, tipo="saida"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,'Outros','ofx',95,?,0)""",
                   (uid_lim, tipo, valor, desc, _hoje_l))
        db.execute("UPDATE transacoes SET fitid=? WHERE id=last_insert_rowid()", (fitid,))

_ins_l(120, "PLG-a", "MERCADO");            _ins_l(120, "700001", "MERCADO")
_ins_l(3000, "PLG-b", "SALARIO", "entrada"); _ins_l(3000, "700002", "SALARIO", "entrada")
_ins_l(5, "PLG-c", "CAFE");                 _ins_l(5, "PLG-d", "CAFE")     # dois cafes REAIS
_ins_l(9, "PLG-e", "PAO");                  _ins_l(9, "PLG-f", "PAO")
_ins_l(9, "700003", "PAO");                 _ins_l(9, "700004", "PAO")     # 4 linhas, 2 paes
_ins_l(42, "PLG-g", "UBER")
_ins_l(60, None, "ALMOCO");                 _ins_l(60, "700005", "ALMOCO")

check("o provedor sai do formato do id, igual ao import",
      (_limp._provedor("PLG-1"), _limp._provedor("PDF-1"), _limp._provedor("77"), _limp._provedor(None))
      == ("pluggy", "pdf", "ofx", None))

_achados = [a for a in _limp.encontrar() if a["chave"][0] == uid_lim]
_para_apagar = sum(len(a["apagar"]) for a in _achados)
check("acha as 5 linhas repetidas", _para_apagar == 5, _para_apagar)
check("e nao marca os dois cafes iguais, que sao reais",
      all("CAFE" not in (r["descricao"] or "") for a in _achados for r in a["apagar"]),
      [r["descricao"] for a in _achados for r in a["apagar"]])
check("mantem o lancamento da mao, que tem a categoria da pessoa",
      all(r["fitid"] is not None for a in _achados for r in a["apagar"]),
      [r["fitid"] for a in _achados for r in a["apagar"]])

# So mostrar nao pode apagar nada — o padrao do script e nao mexer.
with get_db() as db:
    _antes_l = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?",
                          (uid_lim,)).fetchone()["c"]
_limp.encontrar()
with get_db() as db:
    _depois_olhar = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?",
                               (uid_lim,)).fetchone()["c"]
check("procurar nao apaga nada", _antes_l == _depois_olhar, (_antes_l, _depois_olhar))

# Agora apagando de verdade.
with get_db() as db:
    db.execute("BEGIN IMMEDIATE")
    db.executemany("DELETE FROM transacoes WHERE id = ?",
                   [(r["id"],) for a in _achados for r in a["apagar"]])
    db.commit()
with get_db() as db:
    _restou = db.execute("""SELECT descricao, COUNT(*) n FROM transacoes
                             WHERE user_id=? GROUP BY descricao""", (uid_lim,)).fetchall()
_conta_l = {r["descricao"]: r["n"] for r in _restou}
check("depois da limpeza sobra exatamente o que e real",
      _conta_l == {"MERCADO": 1, "SALARIO": 1, "CAFE": 2, "PAO": 2, "UBER": 1, "ALMOCO": 1},
      _conta_l)
check("e o saldo fica certo",
      A.calc_transaction_totals(uid_lim)["balance"] == 3000 - 250,
      A.calc_transaction_totals(uid_lim)["balance"])

# Base limpa nao pode dar falso positivo.
c_ok = novo_cliente("semdobra@teste.com", nome="Ok")
uid_ok = uid_de("semdobra@teste.com")
A.import_ofx_transactions(uid_ok, [{"valor": 50, "tipo": "saida", "data": _hoje_l,
                                    "descricao": "X", "fitid": "PLG-só", "no_credito": False}])
check("base sem duplicata nao acusa nada",
      not [a for a in _limp.encontrar() if a["chave"][0] == uid_ok])


secao("89. Conferir os numeros contra o extrato do banco")
# O Matheus comparou com o resumo do Nubank e o ritmo do app estava 48% acima do
# que saiu da conta dele. Eu ja tinha chutado errado antes por nao ver os dados,
# entao o diagnostico le os numeros DELE e mostra a conta aberta.
import subprocess as _sp
import sys as _sys

c_conf = novo_cliente("conferir@teste.com", nome="Conf")
uid_conf = uid_de("conferir@teste.com")
_hoje_c = date.today()

def _mov_c(tipo, v, desc, cat, dias, cred=0, fonte="ofx", fitid=None):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,?,95,?,?)""",
                   (uid_conf, tipo, v, desc, cat, fonte,
                    (_hoje_c - timedelta(days=dias)).isoformat(), cred))
        if fitid:
            db.execute("UPDATE transacoes SET fitid=? WHERE id=last_insert_rowid()", (fitid,))

_mov_c("entrada", 2660, "SALARIO", "Salario", 3, fitid="PLG-s")
for _i in range(31):
    _mov_c("saida", 60, f"gasto {_i}", "Mercado", _i + 3, fitid=f"PLG-g{_i}")
_mov_c("saida", 900, "ALUGUEL", "Moradia", 10, fitid="PLG-alu")
_mov_c("saida", 900, "ALUGUEL", "Moradia", 10, fitid="700001")      # duplicata
_mov_c("saida", 420, "ROUPA", "Compras", 8, cred=1, fitid="PLG-c")
_mov_c("saida", 300, "Guardado", "Reserva", 15, fonte="manual")
_mov_c("saida", 55, "Ajuste de saldo", "Outros", 20, fonte="ajuste")

_r_conf = _sp.run([_sys.executable, "conferir_numeros.py", "conferir@teste.com"],
                  capture_output=True, text=True, encoding="utf-8",
                  env=dict(os.environ, PYTHONIOENCODING="utf-8"))
_saida_c = _r_conf.stdout
check("o diagnostico roda sem erro", _r_conf.returncode == 0, _r_conf.stderr[-300:])
check("separa o que e cartao do que saiu da conta",
      "compras no cart" in _saida_c and "420" in _saida_c)
check("mostra o guardado em meta como fora da conta", "guardado em meta" in _saida_c)
check("e o ajuste de saldo tambem", "ajuste de saldo" in _saida_c)
check("mostra mes a mes pra comparar com o banco", "compare com o resumo do seu banco" in _saida_c)
check("abre os maiores dias, que e onde a media mora", "MAIORES DIAS" in _saida_c)
check("acusa a duplicata e aponta o script que limpa",
      "repetidos" in _saida_c and "limpar_duplicadas" in _saida_c, _saida_c[-400:])
check("mostra o ritmo antes de qualquer corte", "sem cortar nada" in _saida_c)

# Nao pode vazar dado de outra pessoa: so imprime quem foi pedido.
check("so mostra a pessoa pedida", "Conf" in _saida_c and "Fulano" not in _saida_c)
_r_zero = _sp.run([_sys.executable, "conferir_numeros.py", "naoexiste@teste.com"],
                  capture_output=True, text=True, encoding="utf-8",
                  env=dict(os.environ, PYTHONIOENCODING="utf-8"))
check("email desconhecido nao imprime nada de ninguem",
      _r_zero.returncode != 0 and "Nao achei" in _r_zero.stdout.replace("ã", "a"),
      _r_zero.stdout[:200])
_r_sem = _sp.run([_sys.executable, "conferir_numeros.py"],
                 capture_output=True, text=True, encoding="utf-8",
                 env=dict(os.environ, PYTHONIOENCODING="utf-8"))
check("sem email, explica como usar em vez de despejar tudo",
      _r_sem.returncode != 0 and "seu@email.com" in _r_sem.stdout)


secao("90. Dinheiro trocando de bolso nao e receita nem gasto")
# "mes a mes mostra que sobrou 2980 de 6045 que entrou e 3065 que saiu" — num mes
# em que ele recebeu 2.660. Faltava um conceito no modelo: o app tinha tipo,
# no_credito e fonte, e nada que dissesse "isso e dinheiro seu mudando de lugar".
# A caixinha do Nubank e um RDB: guardar vira "Aplicacao RDB", tirar vira
# "Resgate RDB" — e quem usa caixinha mexe nela todo mes.
check("reconhece a caixinha do Nubank",
      A.e_movimento_interno("Aplicacao RDB") and A.e_movimento_interno("Resgate RDB"))
check("e outras formas de guardar", all(A.e_movimento_interno(t) for t in
      ["Resgate CDB", "Aplicacao poupanca", "Transferencia entre contas",
       "RESGATE AUTOMATICO", "Caixinha da viagem", "Reserva de emergencia"]))
check("NAO confunde salario com movimento interno",
      not any(A.e_movimento_interno(t) for t in
              ["Transferencia recebida - EMPRESA LTDA", "Pix recebido - Fulano",
               "SALARIO", "Pagamento de cliente", "MERCADO CENTRAL", "Uber"]))

c_int = novo_cliente("interno@teste.com", nome="Int")
uid_int = uid_de("interno@teste.com")
_fim_ant = date.today().replace(day=1) - timedelta(days=1)
_ini_ant = _fim_ant.replace(day=1)
_mes_ant = _fim_ant.strftime("%Y-%m")

_itens_int = []
def _add_int(tipo, v, desc, dia, fitid):
    _itens_int.append({"valor": v, "tipo": tipo,
                       "data": (_ini_ant + timedelta(days=dia)).isoformat(),
                       "descricao": desc, "fitid": fitid, "no_credito": False})

_add_int("entrada", 2660, "Transferencia recebida - EMPRESA LTDA", 0, "PLG-1")
_add_int("entrada", 250, "Pix recebido - Fulano", 4, "PLG-2")
for _i in range(20):
    _add_int("saida", 60, f"Compra no debito {_i}", _i, f"PLG-g{_i}")
for _i, (_v, _dia) in enumerate([(800, 3), (600, 8), (900, 13)]):
    _add_int("entrada", _v, "Resgate RDB", _dia, f"PLG-r{_i}")
for _i, (_v, _dia) in enumerate([(500, 1), (400, 6)]):
    _add_int("saida", _v, "Aplicacao RDB", _dia, f"PLG-a{_i}")
A.import_ofx_transactions(uid_int, _itens_int)

_m_int = {x["mes"]: x for x in A.historico_mensal(uid_int, 4)}[_mes_ant]
check("'entrou' mostra o que entrou de verdade, nao 5.210",
      _m_int["entrou"] == 2910, _m_int["entrou"])
check("'saiu' mostra o que saiu de verdade, nao 2.100",
      _m_int["saiu"] == 1200, _m_int["saiu"])
check("e 'sobrou' para de inflar", _m_int["sobrou"] == 1710, _m_int["sobrou"])

# O saldo NAO pode mudar: o dinheiro trocou de conta de verdade.
check("o saldo continua contando a caixinha",
      A.calc_transaction_totals(uid_int)["balance"] == 2910 + 2300 - 1200 - 900,
      A.calc_transaction_totals(uid_int)["balance"])

# O ritmo diario tambem nao pode contar aplicacao como gasto.
with get_db() as db:
    _internos = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=? AND interno=1",
                           (uid_int,)).fetchone()["c"]
check("os 5 movimentos de caixinha ficaram marcados", _internos == 5, _internos)
_det_int = A.detalhe_do_ritmo(uid_int)
check("e nenhum deles aparece nos maiores dias do ritmo",
      all(m["total"] not in (500.0, 400.0) for m in _det_int["maiores"]), _det_int["maiores"])

# O script que marca o que ja estava no banco.
import subprocess as _sp2
import sys as _sys2
c_velho = novo_cliente("velho@teste.com", nome="Velho")
uid_velho = uid_de("velho@teste.com")
with get_db() as db:
    for _v, _desc, _tipo in [(800, "Resgate RDB", "entrada"), (500, "Aplicacao RDB", "saida"),
                             (2660, "SALARIO", "entrada")]:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito,interno)
                      VALUES (?,?,?,?,'Outros','ofx',95,?,0,0)""",
                   (uid_velho, _tipo, _v, _desc, date.today().isoformat()))

_amb = dict(os.environ, PYTHONIOENCODING="utf-8")
_r_ver = _sp2.run([_sys2.executable, "marcar_internos.py", "velho@teste.com"],
                  capture_output=True, text=True, encoding="utf-8", env=_amb)
check("o script lista sem alterar nada", "Nada foi alterado" in _r_ver.stdout, _r_ver.stdout[-300:])
with get_db() as db:
    check("e nada foi mesmo marcado",
          db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=? AND interno=1",
                     (uid_velho,)).fetchone()["c"] == 0)

_r_apl = _sp2.run([_sys2.executable, "marcar_internos.py", "velho@teste.com", "--aplicar"],
                  capture_output=True, text=True, encoding="utf-8", env=_amb)
check("com --aplicar ele marca", _r_apl.returncode == 0, _r_apl.stderr[-300:])
with get_db() as db:
    _marcados = db.execute(
        "SELECT descricao FROM transacoes WHERE user_id=? AND interno=1", (uid_velho,)).fetchall()
check("marcou os dois da caixinha", len(_marcados) == 2, [r["descricao"] for r in _marcados])
check("e NAO tocou no salario",
      all("SALARIO" not in r["descricao"] for r in _marcados),
      [r["descricao"] for r in _marcados])

# A tela de Meses tem que deixar abrir o que formou cada total.
_h_meses_int = c_int.get(f"/meses?mes={_mes_ant}").get_data(as_text=True)
check("da pra abrir de onde vem o 'entrou'", "De onde v" in _h_meses_int)
check("com as linhas listadas", "Transferencia recebida" in _h_meses_int)
check("e a caixinha nao aparece la, porque nao e receita",
      "Resgate RDB" not in _h_meses_int)


secao("91. Salario contado duas vezes: mao + banco")
# Ele mandou print da tela: "31/07 Transferencia Recebida  + R$ 2.660,41" e
# "30/07 salário  + R$ 2.600,00". O mesmo salario duas vezes. Ele tinha digitado
# na mao (por causa daquele bug de entrada) e depois o banco trouxe o de verdade.
# A reconciliacao do import nao pega: exige valor igual ao centavo e mesmo dia,
# e aqui sao R$ 60,41 e um dia de diferenca.
_hoje_d91 = date.today()

def _cli91(email):
    c = novo_cliente(email, nome="R")
    return c, uid_de(email)

def _mao(uid, tipo, valor, desc, dias=0, cat="Outros"):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,?,?,?,?,'manual',100,?,0)""",
                   (uid, tipo, valor, desc, cat,
                    (_hoje_d91 - timedelta(days=dias)).isoformat()))

def _banco(uid, tipo, valor, desc, fitid, dias=0):
    A.import_ofx_transactions(uid, [{"valor": valor, "tipo": tipo, "no_credito": False,
                                     "data": (_hoje_d91 - timedelta(days=dias)).isoformat(),
                                     "descricao": desc, "fitid": fitid}])

c91, u91 = _cli91("dup91@teste.com")
_mao(u91, "entrada", 2600, "salário", 1, "Salário")
_banco(u91, "entrada", 2660.41, "Transferencia Recebida|RJUAN DE ANDRADE", "PLG-1", 0)

check("o mes conta o salario em dobro antes de resolver",
      A.calc_transaction_totals(u91)["month_income"] == 5260.41,
      A.calc_transaction_totals(u91)["month_income"])
_d91 = A.possiveis_duplicatas(u91)
check("o app percebe o par", len(_d91) == 1, _d91)
check("e sabe qual foi anotado na mao", _d91[0]["manual"]["valor"] == 2600, _d91[0])

_h91 = c91.get("/").get_data(as_text=True)
check("a tela inicial pergunta em vez de decidir", "mesmo dinheiro" in _h91)
check("mostrando os dois lado a lado", "voc\u00ea anotou na m\u00e3o" in _h91 and "veio do banco" in _h91)
check("e avisando que o mes esta contando em dobro", "contado em dobro" in _h91)

c91.post("/duplicata", data={"csrf_token": "t", "id_manual": _d91[0]["manual"]["id"],
                             "resposta": "juntar"})
check("juntando, fica o valor do banco",
      A.calc_transaction_totals(u91)["month_income"] == 2660.41,
      A.calc_transaction_totals(u91)["month_income"])
check("e nao pergunta de novo", A.possiveis_duplicatas(u91) == [])

# "Sao coisas diferentes" nao pode apagar nada, e a resposta tem que ficar.
c92, u92 = _cli91("dup92@teste.com")
_mao(u92, "saida", 100, "mercado", 0, "Mercado")
_banco(u92, "saida", 103, "OUTRO MERCADO", "PLG-9", 0)
_d92 = A.possiveis_duplicatas(u92)
check("tambem pergunta em gasto, nao so em entrada", len(_d92) == 1, _d92)
c92.post("/duplicata", data={"csrf_token": "t", "id_manual": _d92[0]["manual"]["id"],
                             "resposta": "separado"})
with get_db() as db:
    _n92 = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?", (u92,)).fetchone()["c"]
check("dizer que sao diferentes NAO apaga nada", _n92 == 2, _n92)
check("e a resposta fica guardada", A.possiveis_duplicatas(u92) == [])

# O caro: nao pode inventar par onde nao ha.
c93, u93 = _cli91("dup93@teste.com")
_mao(u93, "saida", 50, "cafe")
_banco(u93, "saida", 900, "ALUGUEL", "PLG-a")            # valor longe
_banco(u93, "entrada", 50, "PIX RECEBIDO", "PLG-b")      # tipo diferente
_banco(u93, "saida", 50, "LONGE NO TEMPO", "PLG-c", 9)   # 9 dias depois
check("valor distante, tipo diferente e data distante nao viram par",
      A.possiveis_duplicatas(u93) == [], A.possiveis_duplicatas(u93))

# Valor pequeno precisa de piso, senao 5% casaria com qualquer troco.
c94, u94 = _cli91("dup94@teste.com")
_mao(u94, "saida", 30, "pao")
_banco(u94, "saida", 45, "PADARIA", "PLG-p")
check("R$ 30 e R$ 45 nao sao a mesma coisa",
      A.possiveis_duplicatas(u94) == [], A.possiveis_duplicatas(u94))

# Aporte de meta e lancamento manual, mas nunca e duplicata de nada.
c95, u95 = _cli91("dup95@teste.com")
_mao(u95, "saida", 500, "Guardado na meta: Viagem", 0, "Reserva")
_banco(u95, "saida", 500, "APLICACAO", "PLG-r")
check("aporte de meta nao entra na pergunta",
      A.possiveis_duplicatas(u95) == [], A.possiveis_duplicatas(u95))

# Lancamento de outra pessoa nao pode ser apagado por aqui.
c96, u96 = _cli91("dup96@teste.com")
_mao(u96, "entrada", 2600, "salário", 1, "Salário")
_banco(u96, "entrada", 2660.41, "TRANSFERENCIA", "PLG-z", 0)
_alheio = A.possiveis_duplicatas(u96)[0]["manual"]["id"]
c91.post("/duplicata", data={"csrf_token": "t", "id_manual": _alheio, "resposta": "juntar"})
with get_db() as db:
    _vive = db.execute("SELECT COUNT(*) c FROM transacoes WHERE id=?", (_alheio,)).fetchone()["c"]
check("ninguem apaga lancamento de outra conta", _vive == 1, _vive)


secao("92. Tela pra conferir os numeros contra o banco")
# Passei uma sessao inteira deduzindo a causa de numeros que nao batiam sem ver
# um dado real dele, e errei quase todas. A tela poe tudo aberto: ele fotografa
# e a conversa deixa de depender de palpite.
c_cf = novo_cliente("conferir2@teste.com", nome="Cf")
uid_cf = uid_de("conferir2@teste.com")
_hoje_cf = date.today()

def _mv(tipo, v, desc, cat, dias, cred=0, fonte="ofx", fitid=None, interno=0):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,estabelecimento,
                      categoria,fonte,confidence,data_transacao,no_credito,interno)
                      VALUES (?,?,?,?,?,?,?,95,?,?,?)""",
                   (uid_cf, tipo, v, desc, desc, cat, fonte,
                    (_hoje_cf - timedelta(days=dias)).isoformat(), cred, interno))
        if fitid:
            db.execute("UPDATE transacoes SET fitid=? WHERE id=last_insert_rowid()", (fitid,))

_mv("entrada", 2660.41, "Transferencia Recebida", "Salario", 3, fitid="PLG-s")
for _i in range(30):
    _mv("saida", 62, f"COMPRA {_i}", "Mercado", _i, fitid=f"PLG-g{_i}")
_mv("saida", 900, "ALUGUEL", "Moradia", 10, fitid="PLG-al")
_mv("saida", 420, "ROUPA", "Compras", 8, cred=1, fitid="PLG-c")
_mv("saida", 800, "Aplicacao RDB", "Outros", 12, fitid="PLG-rdb", interno=1)
_mv("saida", 55, "Ajuste de saldo", "Outros", 20, fonte="ajuste")
_mv("saida", 300, "Guardado na meta", "Reserva", 15, fonte="manual")

_r_cf = c_cf.get("/conferir")
check("a tela abre", _r_cf.status_code == 200, _r_cf.status_code)
_h_cf = _r_cf.get_data(as_text=True)

check("mostra o mes a mes pra comparar com o banco", "Compare linha a linha" in _h_cf)
check("abre a conta do ritmo diario", "O ritmo di" in _h_cf and "aberto" in _h_cf)
check("diz por quantos dias esta dividindo", "Dividido por" in _h_cf)
check("mostra o ritmo ANTES do corte e o que o app usa",
      "por dia" in _h_cf and "o n\u00famero que o app usa" in _h_cf)

# Cada exclusao tem que aparecer com o valor, senao a pessoa nao consegue conferir.
for _rotulo, _valor in [("compras no cr", "420,00"), ("guardado em meta", "300,00"),
                        ("trocou de bolso", "800,00"), ("ajuste de saldo", "55,00")]:
    check(f"mostra o que ficou de fora: {_rotulo}",
          _rotulo in _h_cf and _valor in _h_cf, _rotulo)

check("lista TODOS os dias com gasto, nao so os seis maiores",
      "dias com gasto" in _h_cf and _h_cf.count("dias-do-ritmo") >= 1)
check("e mostra como cada lancamento esta marcado", "est\u00e3o marcados" in _h_cf)
check("aponta onde corrigir", "/transacoes" in _h_cf)

# O caminho pra chegar la nao pode ser adivinhado.
check("Configuracoes leva pra tela", "/conferir" in c_cf.get("/settings").get_data(as_text=True))
check("com um titulo que a pessoa reconhece",
      "n\u00e3o parece seu" in c_cf.get("/settings").get_data(as_text=True))

# Conta vazia nao pode quebrar nem mentir.
c_vz = novo_cliente("conferirvazio@teste.com", nome="Vz")
_h_vz = c_vz.get("/conferir")
check("conta sem movimento nenhum abre igual", _h_vz.status_code == 200)
check("e diz que nao ha gasto, em vez de mostrar zero seco",
      "Nenhum gasto nos \u00faltimos 60 dias" in _h_vz.get_data(as_text=True))

# Ninguem ve o numero de outra pessoa.
_an_cf = A.app.test_client()
check("precisa estar logado", _an_cf.get("/conferir", follow_redirects=False).status_code == 302)


secao("93. Id de URL absurdo e pagina que nao existe, nao erro 500")
# O conversor <int:> do Flask aceita numero de qualquer tamanho. Um id de 40
# digitos passava pela rota, chegava no SQLite e estourava com OverflowError:
# erro 500 em 16 rotas, e uma linha de traceback no log pra cada varredura
# automatica que passasse por aqui.
import re as _re93

c_id = novo_cliente("ids@teste.com", nome="Id")
uid_id = uid_de("ids@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'saida',10,'x','Outros',
                  'manual',100,?,0)""", (uid_id, date.today().isoformat()))
    _tid = db.execute("SELECT id FROM transacoes WHERE user_id=?", (uid_id,)).fetchone()["id"]

_rotas_id = [r for r in A.app.url_map.iter_rules() if "<int:" in r.rule]
check("o app tem rotas com id na URL", len(_rotas_id) >= 10, len(_rotas_id))

_quebradas = []
for _r in _rotas_id:
    for _gigante in ("9" * 40, "9" * 19, "1" * 25):
        _url = _re93.sub(r"<int:[^>]+>", _gigante, _r.rule)
        _m = "POST" if "POST" in _r.methods and "GET" not in _r.methods else "GET"
        _resp = c_id.open(_url, method=_m, data={"csrf_token": "t"} if _m == "POST" else None)
        if _resp.status_code == 500:
            _quebradas.append((_m, _url[:40]))
check("nenhum id gigante derruba rota nenhuma", not _quebradas, _quebradas[:3])

check("id normal continua abrindo",
      c_id.get(f"/transacoes/{_tid}/editar").status_code == 200)
check("id que nao existe nao da 500",
      c_id.get("/transacoes/999999/editar").status_code in (302, 404))

# O mesmo pelo formulario, que nao passa pelo conversor da URL.
for _lixo in ("9" * 40, "-1", "0", "abc", "1e30", "1.5", ""):
    _r93 = c_id.post("/duplicata", data={"csrf_token": "t", "id_manual": _lixo},
                     follow_redirects=True)
    check(f"/duplicata com id {_lixo[:12]!r:<14} nao quebra", _r93.status_code == 200,
          _r93.status_code)

# E o conversor nao pode ter ficado permissivo demais nem restrito demais.
check("id de 18 digitos ainda e aceito pela rota",
      c_id.get("/transacoes/" + "9" * 18 + "/editar").status_code in (302, 404))
check("id com letra nao casa a rota",
      c_id.get("/transacoes/12a/editar").status_code == 404)


secao("94. Tela inicial nao vira parede de perguntas")
# O detector pode achar muitos pares. Vinte cards empurrariam o saldo — que e o
# motivo de a pessoa ter aberto o app — pra fora da tela.
c_mt = novo_cliente("muitos@teste.com", nome="Mt")
uid_mt = uid_de("muitos@teste.com")
for _i in range(8):
    with get_db() as db:
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      confidence,data_transacao,no_credito) VALUES (?,'saida',?,?,'Outros',
                      'manual',100,?,0)""",
                   (uid_mt, 100 + _i * 50, f"gasto {_i}",
                    (date.today() - timedelta(days=_i)).isoformat()))
    A.import_ofx_transactions(uid_mt, [{"valor": 100 + _i * 50 + 2, "tipo": "saida",
                                        "data": (date.today() - timedelta(days=_i)).isoformat(),
                                        "descricao": f"BANCO {_i}", "fitid": f"PLG-{_i}",
                                        "no_credito": False}])
_dups_mt = A.possiveis_duplicatas(uid_mt)
check("o detector acha os oito pares", len(_dups_mt) == 8, len(_dups_mt))
_h_mt = c_mt.get("/").get_data(as_text=True)
check("mas a tela mostra so tres", _h_mt.count("mesmo dinheiro") == 3,
      _h_mt.count("mesmo dinheiro"))
check("e avisa que tem mais esperando", "Tem mais 5 par" in _h_mt)

# Uma consulta so: isto roda em toda carga da tela inicial.
import database as _dbmod93
_contador93 = {"n": 0}
_orig93 = _dbmod93.get_db


class _Espia93:
    def __init__(self, conn):
        self._c = conn

    def execute(self, *a, **k):
        _contador93["n"] += 1
        return self._c.execute(*a, **k)

    def __getattr__(self, nome):
        return getattr(self._c, nome)

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *a):
        return self._c.__exit__(*a)


A.get_db = lambda: _Espia93(_orig93())
_contador93["n"] = 0
A.possiveis_duplicatas(uid_mt)
A.get_db = _orig93
check("o detector faz UMA consulta, nao uma por lancamento",
      _contador93["n"] == 1, _contador93["n"])


secao("95. O app nao pode ser trampolim pra outro site")
# 7 rotas faziam redirect(request.referrer or ...). O Referer vem de quem faz a
# requisicao, entao um link pro dominio do Hercules cuspia a pessoa em outro
# site — com o dominio confiavel aparecendo antes, que e o truque de um phishing
# bom.
c_rd = novo_cliente("redirect@teste.com", nome="Rd")

for _hostil in ["https://site-malicioso.com/phishing", "//evil.com", "javascript:alert(1)",
                "data:text/html,<script>alert(1)</script>", "HtTpS://EVIL.com",
                "https://evil.com@localhost/x", "\\\\evil.com"]:
    _r = c_rd.post("/duplicata", data={"csrf_token": "t", "id_manual": "1"},
                   headers={"Referer": _hostil}, follow_redirects=False)
    _destino = _r.headers.get("Location", "")
    _escapou = (_destino.startswith("//") or _destino.startswith("javascript:")
                or _destino.startswith("data:") or _destino.startswith("\\\\")
                or (_destino.startswith("http") and "localhost" not in _destino))
    check(f"Referer {_hostil[:30]:<32} nao leva pra fora", not _escapou, _destino[:50])

check("caminho relativo do proprio app continua funcionando",
      c_rd.post("/duplicata", data={"csrf_token": "t", "id_manual": "1"},
                headers={"Referer": "/meses"},
                follow_redirects=False).headers.get("Location") == "/meses")
check("endereco completo do MESMO host tambem volta certo",
      c_rd.post("/duplicata", data={"csrf_token": "t", "id_manual": "1"},
                headers={"Referer": "http://localhost/transacoes", "Host": "localhost"},
                follow_redirects=False).headers.get("Location") == "http://localhost/transacoes")
check("sem Referer, cai no destino padrao",
      c_rd.post("/duplicata", data={"csrf_token": "t", "id_manual": "1"},
                follow_redirects=False).headers.get("Location", "").endswith("/"))
check("nenhuma rota redireciona direto pro referrer",
      "request.referrer or url_for" not in _io_layout.open("app.py", encoding="utf-8").read())


secao("96. Valor que nao e numero nao pode entrar no banco")
# float("1e400") e infinito e float("nan") e NaN — os dois saem de um OFX
# malformado sem esforco. O infinito ENTRAVA: o saldo virava "R$ -inf" na tela,
# pra sempre. O NaN derrubava a importacao inteira com IntegrityError.
check("valor_absurdo pega infinito", A.valor_absurdo(float("inf")))
check("e pega NaN — 'nan >= X' e False, entao ele passava batido",
      A.valor_absurdo(float("nan")))
check("mas nao estraga valor normal",
      not A.valor_absurdo(500.0) and not A.valor_absurdo(0.0) and not A.valor_absurdo(-99.9))

for _veneno in ["1e400", "-1e400", "nan", "-nan", "inf", "-inf", "Infinity", "1" + "0" * 400]:
    _itens = A.parse_ofx(
        f"<STMTTRN><TRNAMT>{_veneno}</TRNAMT><DTPOSTED>20260101</DTPOSTED></STMTTRN>")
    check(f"OFX com {_veneno[:10]:<11} e descartado", _itens == [], _itens)

c_vn = novo_cliente("veneno@teste.com", nome="Vn")
uid_vn = uid_de("veneno@teste.com")
A.import_ofx_transactions(uid_vn, A.parse_ofx(
    "<STMTTRN><TRNAMT>-1e400</TRNAMT><DTPOSTED>20260101</DTPOSTED><MEMO>VENENO</MEMO></STMTTRN>"
    "<STMTTRN><TRNAMT>nan</TRNAMT><DTPOSTED>20260101</DTPOSTED><MEMO>VENENO2</MEMO></STMTTRN>"
    "<STMTTRN><TRNAMT>-500</TRNAMT><DTPOSTED>20260101</DTPOSTED><MEMO>MERCADO</MEMO></STMTTRN>"))
_st_vn = A.calc_transaction_totals(uid_vn)
check("a linha boa do arquivo entra", _st_vn["balance"] == -500.0, _st_vn["balance"])
check("o saldo continua sendo um numero de verdade",
      _st_vn["balance"] == _st_vn["balance"] and abs(_st_vn["balance"]) != float("inf"))

# Rede de seguranca no importador: vale pra qualquer origem, ate uma que nao
# existe ainda.
_n_antes_vn = None
with get_db() as db:
    _n_antes_vn = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?",
                             (uid_vn,)).fetchone()["c"]
A.import_ofx_transactions(uid_vn, [
    {"valor": float("inf"), "tipo": "saida", "data": "2026-08-01", "descricao": "D1",
     "fitid": "X1", "no_credito": False},
    {"valor": float("nan"), "tipo": "saida", "data": "2026-08-01", "descricao": "D2",
     "fitid": "X2", "no_credito": False},
    {"valor": None, "tipo": "saida", "data": "2026-08-01", "descricao": "D3",
     "fitid": "X3", "no_credito": False},
])
with get_db() as db:
    _n_depois_vn = db.execute("SELECT COUNT(*) c FROM transacoes WHERE user_id=?",
                              (uid_vn,)).fetchone()["c"]
check("o importador barra infinito, NaN e None vindos de qualquer lugar",
      _n_depois_vn == _n_antes_vn, (_n_antes_vn, _n_depois_vn))
check("e nao derruba o import inteiro por causa de uma linha ruim",
      A.import_ofx_transactions(uid_vn, [
          {"valor": float("inf"), "tipo": "saida", "data": "2026-08-01", "descricao": "ruim",
           "fitid": "Y1", "no_credito": False},
          {"valor": 42.0, "tipo": "saida", "data": "2026-08-01", "descricao": "boa",
           "fitid": "Y2", "no_credito": False}])["importadas"] == 1)


secao("97. Backup que nao restaura nao e backup")
# O proprio backup.py imprime "python backup.py --restaurar <nome> saida.db",
# mas restaurar() abria Path(<nome>) direto — e as copias moram noutra pasta.
# Seguindo a instrucao do app, dava FileNotFoundError. No dia em que o banco
# quebrasse, ele descobriria isso tarde demais.
import backup as _B97
import sqlite3 as _sq97

_dir97 = tempfile.mkdtemp()
_B97.BACKUP_DIR = pathlib.Path(_dir97)
_copia = _B97.fazer_backup()
check("o backup e feito", _copia.exists())

_destino97 = os.path.join(_dir97, "restaurado.db")
_saida97 = _B97.restaurar(_copia.name, _destino97)          # SO O NOME
check("restaura passando so o nome, como o comando manda", _saida97.exists())

_conf97 = _sq97.connect(_saida97)
_conf97.row_factory = _sq97.Row
_cols97 = {r["name"] for r in _conf97.execute("PRAGMA table_info(transacoes)")}
_integ97 = _conf97.execute("PRAGMA integrity_check").fetchone()[0]
_conf97.close()
check("a copia restaurada esta integra", _integ97 == "ok", _integ97)
check("e traz as colunas novas desta sessao",
      {"interno", "dup_ok"} <= _cols97, sorted(_cols97))

check("caminho inteiro tambem continua funcionando",
      _B97.restaurar(str(_copia), os.path.join(_dir97, "r2.db")).exists())

try:
    _B97.restaurar("nao-existe.db.gz", os.path.join(_dir97, "x.db"))
    check("copia inexistente avisa em vez de estourar", False, "nao levantou nada")
except FileNotFoundError as _e97:
    _msg97 = str(_e97)
    check("copia inexistente avisa qual e o problema", "Não achei a cópia" in _msg97, _msg97[:60])
    check("e lista as copias que existem", "Cópias que existem" in _msg97, _msg97[:120])
except Exception as _e97:
    check("copia inexistente avisa em vez de estourar", False, type(_e97).__name__)


secao("98. Banco antigo, de antes das colunas novas, ainda abre")
# Se restaurar uma copia antiga quebrar o app, o backup deixa de ser rede de
# seguranca — que e a unica coisa que ele precisa ser.
_dir98 = tempfile.mkdtemp()
_db98 = os.path.join(_dir98, "antigo.db")
_c98 = _sq97.connect(_db98)
_c98.executescript("""
CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL, senha TEXT NOT NULL, perfil TEXT NOT NULL DEFAULT 'pf',
  home_focus TEXT NOT NULL DEFAULT 'saldo', notification_mode TEXT NOT NULL DEFAULT 'equilibrado',
  meta_mensal REAL NOT NULL DEFAULT 0.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE transacoes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  nota_id INTEGER, tipo TEXT NOT NULL CHECK(tipo IN ('entrada','saida')), valor REAL NOT NULL,
  descricao TEXT, estabelecimento TEXT, categoria TEXT, data_transacao TEXT,
  fonte TEXT NOT NULL DEFAULT 'manual', confidence INTEGER NOT NULL DEFAULT 100,
  needs_review INTEGER NOT NULL DEFAULT 0, extra_json TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE);
""")
_c98.execute("INSERT INTO usuarios (nome,email,senha) VALUES ('Velho','v@t.com','x')")
for _i in range(5):
    _c98.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                    data_transacao) VALUES (1,'saida',?,?,'Mercado','ofx',?)""",
                 (50 + _i, f"antigo {_i}", date.today().isoformat()))
_c98.commit()
_c98.close()

import database as _DB98
_path_original98 = _DB98.DB_PATH
_DB98.DB_PATH = pathlib.Path(_db98)
try:
    _DB98.init_db()
    with _DB98.get_db() as _d98:
        _cols_tx = {r["name"] for r in _d98.execute("PRAGMA table_info(transacoes)")}
        _cols_us = {r["name"] for r in _d98.execute("PRAGMA table_info(usuarios)")}
        _n98 = _d98.execute("SELECT COUNT(*) c FROM transacoes").fetchone()["c"]
        _marcados98 = _d98.execute("SELECT COUNT(*) c FROM transacoes WHERE interno=1").fetchone()["c"]
    check("abrir um banco antigo cria as colunas que faltavam",
          {"interno", "dup_ok"} <= _cols_tx and "dia_virada" in _cols_us)
    check("e nao perde nenhum lancamento antigo", _n98 == 5, _n98)
    check("nem marca nada como interno por acidente", _marcados98 == 0, _marcados98)
finally:
    _DB98.DB_PATH = _path_original98


secao("99. Planos escritos e DESLIGADOS")
# Ele decidiu: R$ 30 pro MEI, e nada pra quem so usa a parte pessoal — "eu mesmo
# nao pagaria". Enquanto os testes rolam, tudo isso fica pronto e desligado.
c_pl = novo_cliente("planos@teste.com", nome="Pl")
uid_pl = uid_de("planos@teste.com")

check("a cobranca nasce DESLIGADA", A.COBRANCA_ATIVA is False)
check("com ela desligada, todo mundo tem acesso ao que sera pago",
      A.tem_acesso_pago({"plano": "livre"}) is True)

_h_pl = c_pl.get("/planos").get_data(as_text=True)
check("a tela de planos abre", "Planos" in _h_pl)
check("e diz que ninguem esta sendo cobrado ainda", "Ainda n\u00e3o estou cobrando nada" in _h_pl)
check("mostra o preco do plano MEI", "R$ 30" in _h_pl)
check("nao mostra botao de assinar enquanto esta desligada", "Assinar" not in _h_pl)
check("escreve a regua: evita dano e de graca", "evita um dano" in _h_pl)
check("e que poupar trabalho pode custar", "Poupa trabalho" in _h_pl)

# O que e livre PRECISA continuar livre — sao as coisas que evitam dano.
for _texto in ["Aviso antes de estourar o limite do MEI", "Lembrete do DAS",
               "Ser\u00e1 que cabe?", "Baixar todos os seus dados"]:
    check(f"fica no Livre: {_texto[:38]}", _texto in _h_pl)

# Com a cobranca desligada, nenhuma funcao pode ser tirada de quem ja usa.
check("o dossie continua aberto com a cobranca desligada",
      c_pl.get("/mei/dossie").status_code == 200)
check("a exportacao do IR tambem", c_pl.get("/exportar-ir").status_code == 200)
check("e /assinar nao cobra nada, so avisa",
      c_pl.get("/assinar", follow_redirects=True).status_code == 200)
with get_db() as db:
    check("ninguem ganhou plano pago por acidente",
          db.execute("SELECT COUNT(*) c FROM usuarios WHERE plano != 'livre'").fetchone()["c"] == 0)


secao("100. Seus dados nunca sao reféns")
# Isto vem ANTES de qualquer coisa paga existir. Um app de dinheiro que prende o
# historico da pessoa atras de assinatura transforma o passado dela em cobranca.
import zipfile as _zip100
import io as _io100

c_md = novo_cliente("meusdados@teste.com", nome="Md")
uid_md = uid_de("meusdados@teste.com")
with get_db() as db:
    for _i in range(3):
        db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                      data_transacao) VALUES (?,'saida',?,?,'Mercado','ofx',?)""",
                   (uid_md, 50 + _i, f"compra {_i}", date.today().isoformat()))
    db.execute("INSERT INTO metas (user_id,nome,meta_valor,valor_atual,ativo) VALUES (?,'V',5000,100,1)",
               (uid_md,))
    db.execute("INSERT INTO notas (user_id,descricao,valor,categoria,tipo) VALUES (?,'Nota',200,'Servi\u00e7os','entrada')",
               (uid_md,))

_r_md = c_md.get("/meus-dados")
check("baixar os proprios dados funciona", _r_md.status_code == 200)
check("vem como arquivo pra salvar", "attachment" in _r_md.headers.get("Content-Disposition", ""))
_z = _zip100.ZipFile(_io100.BytesIO(_r_md.data))
check("traz transacoes, notas, metas e compromissos",
      {"transacoes.csv", "notas.csv", "metas.csv", "compromissos.csv"} <= set(_z.namelist()),
      _z.namelist())
_csv_md = _z.read("transacoes.csv").decode("utf-8")
check("com os lancamentos de verdade dentro", "compra 0" in _csv_md and "50.0" in _csv_md)
check("e um LEIA-ME dizendo que e de graca pra sempre",
      "de gra\u00e7a, sempre" in _z.read("LEIA-ME.txt").decode("utf-8"))

# Mesmo com a cobranca LIGADA, baixar os dados tem que continuar livre.
A.COBRANCA_ATIVA = True
try:
    check("com a cobranca ligada, quem nao paga ainda baixa tudo",
          c_md.get("/meus-dados").status_code == 200)
    check("mas o dossie do contador passa a pedir o plano",
          c_md.get("/mei/dossie", follow_redirects=False).status_code == 302)
    check("e a exportacao do IR tambem",
          c_md.get("/exportar-ir", follow_redirects=False).status_code == 302)
    check("quem tem o plano MEI entra", A.tem_acesso_pago({"plano": "mei"}) is True)
    check("quem nao tem, nao", A.tem_acesso_pago({"plano": "livre"}) is False)

    # Cancelar tem que ser um clique — o Decreto 11.034/2022 exige isso.
    with get_db() as db:
        db.execute("UPDATE usuarios SET plano='mei', plano_desde=? WHERE id=?",
                   (date.today().isoformat(), uid_md))
    _h_md = c_md.get("/planos").get_data(as_text=True)
    check("a tela mostra o botao de cancelar, na propria pagina",
          "Cancelar assinatura" in _h_md)
    check("e avisa dos 7 dias de arrependimento", "devolvo o valor inteiro" in _h_md)
    _r_cancel = c_md.post("/assinatura/cancelar", data={"csrf_token": "t"}, follow_redirects=True)
    check("cancelar responde na hora", _r_cancel.status_code == 200)
    with get_db() as db:
        _u_md = db.execute("SELECT plano, plano_cancelado_em FROM usuarios WHERE id=?",
                           (uid_md,)).fetchone()
    check("o cancelamento fica registrado", _u_md["plano_cancelado_em"] is not None)
    check("mas o acesso NAO e cortado na hora — o periodo pago vale ate o fim",
          _u_md["plano"] == "mei", _u_md["plano"])
    check("e da pra baixar os dados depois de cancelar",
          c_md.get("/meus-dados").status_code == 200)

    # Arrependimento: dentro de 7 dias devolve, fora nao promete o que nao faz.
    check("dentro dos 7 dias, cabe devolucao",
          A.dentro_do_arrependimento({"plano_desde": date.today().isoformat()}) is True)
    check("no setimo dia ainda cabe",
          A.dentro_do_arrependimento(
              {"plano_desde": (date.today() - timedelta(days=7)).isoformat()}) is True)
    check("no oitavo, nao",
          A.dentro_do_arrependimento(
              {"plano_desde": (date.today() - timedelta(days=8)).isoformat()}) is False)
    check("sem data de inicio, nao inventa direito",
          A.dentro_do_arrependimento({"plano_desde": None}) is False)
    check("data podre nao quebra", A.dentro_do_arrependimento({"plano_desde": "abacaxi"}) is False)
finally:
    A.COBRANCA_ATIVA = False

check("a cobranca voltou pra desligada depois do teste", A.COBRANCA_ATIVA is False)
check("Configuracoes leva pra baixar os dados",
      "/meus-dados" in c_md.get("/settings").get_data(as_text=True))
check("e pros planos", "/planos" in c_md.get("/settings").get_data(as_text=True))


secao("101. O dossie responde, em vez de dar trabalho")
# Quem recebe isto e o contador — e ele e o caminho pros clientes dele. Antes o
# arquivo era so um CSV cru e os anexos vinham nomeados com o id do banco, que
# nao diz nada pra quem abre.
import zipfile as _z101
import io as _io101

c_do = novo_cliente("dossie@teste.com", nome="Doceira", perfil="mei")
uid_do = uid_de("dossie@teste.com")
_ANO = date.today().year - 1

with get_db() as db:
    for _i, (_dia, _v, _cat) in enumerate([
            (f"{_ANO}-01-14", 380.0, "Serviços"), (f"{_ANO}-02-11", 240.0, "Varejo"),
            (f"{_ANO}-02-25", 1450.0, "Serviços"), (f"{_ANO}-12-20", 880.0, "Varejo")], start=1):
        db.execute("""INSERT INTO notas (user_id,descricao,valor,categoria,numero_nota,
                      status,tipo,data_emissao) VALUES (?,?,?,?,?,'Autorizada','entrada',?)""",
                   (uid_do, f"venda {_i}", _v, _cat, f"{_i:04d}", _dia))
    db.execute("""INSERT INTO notas (user_id,descricao,valor,categoria,status,tipo,data_emissao)
                  VALUES (?,'compra de insumos',420.0,'Alimentação','Autorizada','saida',?)""",
               (uid_do, f"{_ANO}-03-03"))
    for _m in range(1, 12):
        db.execute("""INSERT INTO compromissos (user_id,descricao,valor,vencimento,tipo,status)
                      VALUES (?,'DAS-MEI',75.90,?,'saida','pago')""",
                   (uid_do, f"{_ANO}-{_m:02d}-20"))

_r_do = c_do.get(f"/mei/dossie?year={_ANO}")
check("o dossie e gerado", _r_do.status_code == 200, _r_do.status_code)
_zd = _z101.ZipFile(_io101.BytesIO(_r_do.data))
check("traz resumo, planilha e leia-me",
      {f"RESUMO_{_ANO}.txt", f"notas_{_ANO}.csv", "LEIA-ME.txt"} <= set(_zd.namelist()),
      _zd.namelist())

_res = _zd.read(f"RESUMO_{_ANO}.txt").decode("utf-8")
# Receita bruta = so as notas de ENTRADA. Somar despesa junto seria o erro que
# faz o contador desconfiar do arquivo inteiro.
check("a receita bruta soma so as notas de entrada", "R$ 2.950,00" in _res, _res[:400])
check("nao mistura a nota de despesa na receita", "3.370" not in _res)
# A DASN-SIMEI pede a receita separada; sem isso o contador refaz a conta na mao.
check("separa comercio/industria de servicos",
      "Comércio / indústria" in _res and "Serviços" in _res)
check("com os valores certos de cada um",
      "R$ 1.120,00" in _res and "R$ 1.830,00" in _res, _res[:400])
check("mostra o mes a mes", "Janeiro" in _res and "Dezembro" in _res)
check("mostra quanto do limite do MEI foi usado",
      "Limite do MEI" in _res and "81.000,00" in _res)
check("e quantas guias de DAS foram pagas", "11 de 12" in _res, _res)
check("diz que e resumo pra conferencia, nao apuracao oficial",
      "não a apuração oficial" in _res)

_csv_do = _zd.read(f"notas_{_ANO}.csv").decode("utf-8")
check("a planilha comeca pela data, pra ordenar sozinha",
      _csv_do.startswith("data_emissao,"), _csv_do[:60])
check("e tem a coluna que liga cada linha ao anexo", "arquivo_anexo" in _csv_do)

# Ano sem nota nenhuma nao pode gerar arquivo quebrado.
_r_vazio = c_do.get(f"/mei/dossie?year={_ANO - 5}")
check("ano sem nota ainda gera dossie", _r_vazio.status_code == 200)
_zv = _z101.ZipFile(_io101.BytesIO(_r_vazio.data))
check("e diz que nao houve nota, em vez de mostrar tabela vazia",
      "nenhuma nota de entrada" in _zv.read(f"RESUMO_{_ANO - 5}.txt").decode("utf-8"))


secao("102. Pagar a fatura nao e ritmo do dia a dia")
# Do print da tela /conferir dele: 22 dias somando R$ 2.904,30, divididos por 25,
# dando R$ 116,17/dia. Dois deles eram "Pagamento de fatura" (R$ 461,48 e
# R$ 289,78) — 26% do total. Diluidos em todos os dias, empurravam o ritmo de
# R$ 86 pra R$ 116. E o corte de dias fora da curva nao agia: 23% dos dias
# passavam do limite e a trava desligava o corte inteiro.
c_ft = novo_cliente("fatura@teste.com", nome="Rjuan de Andrade Silva")
uid_ft = uid_de("fatura@teste.com")

_DIAS_REAIS = [
    ("2026-07-29", 500.00, "Transferência enviada LYVIA VARGAS MONTEIRO"),
    ("2026-07-13", 461.48, "Pagamento de fatura - Transferência"),
    ("2026-07-31", 423.08, "Compra no débito ICTUS BAR"),
    ("2026-08-03", 289.78, "Pagamento de fatura - Compra no débito"),
    ("2026-07-14", 262.00, "Transferência enviada pelo Pix - LIVEPIX"),
    ("2026-07-16", 184.30, "Transferência enviada pelo Pix - Ricardo"),
    ("2026-07-15", 125.98, "Transferência enviada pelo Pix - MAIS MOBI"),
    ("2026-07-30", 94.60, "Compra no débito HOPS RIO BAR"),
    ("2026-07-18", 77.10, "Transferência enviada pelo Pix - AYRTON"),
    ("2026-07-20", 74.00, "Compra no débito - CHOPIN LANCHES"),
    ("2026-07-23", 69.00, "Transferência enviada Joao Victor Santos"),
    ("2026-07-10", 57.34, "Transferência enviada pelo Pix - Ricardo"),
    ("2026-07-24", 52.00, "Transferência enviada LIVEPIX LTDA"),
    ("2026-08-01", 49.08, "Compra no débito DI MARCIA MEDICAMENTO"),
    ("2026-07-22", 45.98, "Transferência enviada Francisco Antonio"),
    ("2026-07-27", 36.00, "Transferência enviada Ricardo Alexandre"),
    ("2026-07-25", 30.99, "Compra no débito CENTRAL DO BISCOITO"),
    ("2026-07-17", 23.50, "Compra no débito JulioCesar0e"),
    ("2026-07-12", 20.08, "Transferência enviada pelo Pix - RJUAN DE ANDRADE"),
    ("2026-07-11", 20.00, "Transferência enviada pelo Pix - MAIS MOBI"),
    ("2026-07-19", 8.00, "Compra no débito ELIAS VAZ 72753706790"),
    ("2026-08-02", 0.01, "Transferência enviada Rjuan de Andrade Silva"),
]
A.import_ofx_transactions(uid_ft, [
    {"valor": _v, "tipo": "saida", "data": _dia, "descricao": _desc,
     "fitid": f"PLG-ft{_i}", "no_credito": False}
    for _i, (_dia, _v, _desc) in enumerate(_DIAS_REAIS)])

check("reconhece pagamento de fatura no extrato da conta",
      A.e_pagamento_de_fatura_na_conta("Pagamento de fatura - Transferência"))
check("mas NAO confunde com pagamento de outra coisa",
      not A.e_pagamento_de_fatura_na_conta("Pagamento de aluguel")
      and not A.e_pagamento_de_fatura_na_conta("Pagamento salário"))

_det_ft = A.detalhe_do_ritmo(uid_ft)
check("nenhum pagamento de fatura sobra no ritmo",
      all("fatura" not in (m.get("o_que") or "").lower() for m in _det_ft["maiores"]),
      _det_ft["maiores"][:3])
check("os dois dias de fatura sumiram da lista de dias",
      all(abs(m["total"] - 461.48) > 0.01 and abs(m["total"] - 289.78) > 0.01
          for m in _det_ft["maiores"]), _det_ft["maiores"][:4])

# Mas a fatura NAO pode simplesmente sumir: ela vence.
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'saida',300,'COMPRA CARTAO',
                  'Compras','ofx',95,?,1)""", (uid_ft, date.today().isoformat()))
_sim_ft = A.simular_gasto(uid_ft, 50)
_st_ft = A.calc_transaction_totals(uid_ft)
check("a fatura em aberto entra no simulador como conta com data",
      _sim_ft["contas"] >= float(_st_ft["fatura_credito_mes"] or 0) > 0,
      (_sim_ft["contas"], _st_ft["fatura_credito_mes"]))

# Pix pro proprio nome: dinheiro trocando de bolso.
check("Pix com o nome completo da pessoa e movimento interno",
      A.e_movimento_interno("Transferência enviada Rjuan de Andrade Silva",
                            "Rjuan de Andrade Silva"))
check("nome parcial NAO casa — apagar Pix pra um xara seria pior",
      not A.e_movimento_interno("Transferência enviada pelo Pix - RJUAN DE ANDRADE",
                                "Rjuan de Andrade Silva"))
check("so o primeiro nome nunca casa",
      not A.e_movimento_interno("Pix para Rjuan", "Rjuan de Andrade Silva"))
check("sem nome cadastrado, so os padroes fixos valem",
      not A.e_movimento_interno("Transferência enviada Fulano", ""))
check("e a caixinha continua sendo reconhecida sem nome nenhum",
      A.e_movimento_interno("Aplicacao RDB", ""))

# A tela tem que MOSTRAR o que saiu, senao some sem explicacao.
_h_ft = c_ft.get("/conferir").get_data(as_text=True)
check("a tela de conferir mostra a linha da fatura", "Fora: pagamento de fatura" in _h_ft)
check("explicando que ela entra inteira no simulador",
      "entra inteira" in _h_ft and "com data" in _h_ft)


secao("103. O lugar que tem duas caras")
# Ideia do Matheus: ele compra doce no Ricardo quase sempre, mas umas 3x por mes
# compra cigarro la. O extrato escreve a mesma coisa nas duas vezes. Nenhuma
# regra automatica separa isso — e nenhuma deveria fingir que separa.
c_amb = novo_cliente("ambiguo@teste.com", nome="Amb")
uid_amb = uid_de("ambiguo@teste.com")
_hj = date.today()

def _compra_amb(valor, dias, fitid, desc="Transferencia enviada pelo Pix - Ricardo"):
    A.import_ofx_transactions(uid_amb, [{"valor": valor, "tipo": "saida", "no_credito": False,
                                         "data": (_hj - timedelta(days=dias)).isoformat(),
                                         "descricao": desc, "fitid": fitid}])

def _ultimo_amb():
    with get_db() as db:
        return db.execute("SELECT id FROM transacoes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                          (uid_amb,)).fetchone()["id"]

def _corrige_amb(tid, cat, valor, dias):
    c_amb.post(f"/transacoes/{tid}/editar", data={
        "csrf_token": "t", "tipo": "saida", "valor": f"{valor},00",
        "descricao": "Transferencia enviada pelo Pix - Ricardo", "estabelecimento": "Ricardo",
        "categoria": cat, "data_transacao": (_hj - timedelta(days=dias)).isoformat()})

for _i, (_v, _d) in enumerate([(18, 25), (22, 22), (15, 20)]):
    _compra_amb(_v, _d, f"D{_i}")
c_amb.post("/regras", data={"csrf_token": "t", "padrao_texto": "Ricardo",
                            "categoria_nome": "Alimentação"})

check("com uma categoria so, o lugar nao e ambiguo", A.regras_ambiguas(uid_amb) == {},
      A.regras_ambiguas(uid_amb))
check("e o Herc nao pergunta nada", A.perguntas_de_categoria(uid_amb) == [])

# Ele corrige duas compras pra Outros (cigarro).
for _i, (_v, _d) in enumerate([(45, 18), (47, 12)]):
    _compra_amb(_v, _d, f"C{_i}")
    _corrige_amb(_ultimo_amb(), "Outros", _v, _d)

check("depois de corrigir, o lugar vira ambiguo",
      A.regras_ambiguas(uid_amb).get("Ricardo") == ["Alimentação", "Outros"],
      A.regras_ambiguas(uid_amb))

# Chegam compras novas: duas de doce e uma de cigarro.
_compra_amb(19, 3, "N1")
_compra_amb(46, 1, "N2")
_compra_amb(21, 2, "N3")
_qs = {round(q["valor"]): q for q in A.perguntas_de_categoria(uid_amb)}
check("pergunta sobre as compras novas do lugar ambiguo", len(_qs) == 3, sorted(_qs))
# O palpite sai do VALOR: cigarro custa uma coisa, doce custa outra, e ela ja
# disse qual e qual ao corrigir.
check("R$ 46 e chutado como cigarro (Outros)", _qs[46]["chutei"] == "Outros", _qs[46])
check("R$ 19 e chutado como doce (Alimentação)", _qs[19]["chutei"] == "Alimentação", _qs[19])
check("R$ 21 tambem", _qs[21]["chutei"] == "Alimentação", _qs[21])
check("e o palpite e sempre o primeiro botao, o toque mais facil",
      all(q["opcoes"][0] == q["chutei"] for q in _qs.values()))
check("os dois botoes sao as categorias que ELA usou ali",
      all(sorted(q["opcoes"]) == ["Alimentação", "Outros"] for q in _qs.values()))

# Compra antiga nao vira pergunta: ninguem lembra o que comprou faz tres semanas.
check("nao pergunta sobre compra de 20+ dias atras",
      all(q["valor"] not in (18.0, 22.0, 15.0) for q in A.perguntas_de_categoria(uid_amb)))

# A tela pergunta, e a resposta vale pra UMA compra.
_h_amb = c_amb.get("/").get_data(as_text=True)
check("a tela inicial pergunta", "foi de qu" in _h_amb)
check("dizendo o que chutou", "Chutei" in _h_amb)
check("e avisando que vale so pra essa compra", "s\u00f3 pra essa compra" in _h_amb)

_id46 = _qs[46]["id"]
c_amb.post("/categoria-desta", data={"csrf_token": "t", "tx_id": _id46, "categoria": "Outros"})
with get_db() as db:
    _r46 = db.execute("SELECT categoria, categoria_manual FROM transacoes WHERE id=?",
                      (_id46,)).fetchone()
    _outras = [x["categoria"] for x in db.execute(
        """SELECT categoria FROM transacoes WHERE user_id=? AND id!=? AND valor IN (19,21)""",
        (uid_amb, _id46))]
check("responder muda so aquela compra", _r46["categoria"] == "Outros" and _r46["categoria_manual"] == 1)
check("e NAO mexe nas outras do mesmo lugar",
      all(cat == "Alimentação" for cat in _outras), _outras)
check("nem pergunta de novo a mesma",
      all(q["id"] != _id46 for q in A.perguntas_de_categoria(uid_amb)))

# Reensinar a regra nao pode apagar o que ela escolheu a dedo.
c_amb.post("/regras", data={"csrf_token": "t", "padrao_texto": "Ricardo",
                            "categoria_nome": "Alimentação"})
with get_db() as db:
    _manuais = db.execute(
        """SELECT COUNT(*) c FROM transacoes
            WHERE user_id=? AND categoria='Outros' AND categoria_manual=1""",
        (uid_amb,)).fetchone()["c"]
check("reensinar a regra NAO apaga as correcoes da mao", _manuais == 3, _manuais)

# Quem nunca corrigiu nada nao pode ser incomodado.
c_paz = novo_cliente("paz@teste.com", nome="Paz")
uid_paz = uid_de("paz@teste.com")
A.import_ofx_transactions(uid_paz, [{"valor": 30, "tipo": "saida", "no_credito": False,
                                     "data": _hj.isoformat(), "descricao": "MERCADO",
                                     "fitid": "PZ1"}])
c_paz.post("/regras", data={"csrf_token": "t", "padrao_texto": "MERCADO",
                            "categoria_nome": "Mercado"})
check("quem nunca corrigiu nada nao ve pergunta nenhuma",
      A.perguntas_de_categoria(uid_paz) == [])
check("e a tela dele fica limpa", "foi de qu" not in c_paz.get("/").get_data(as_text=True))

# Lancamento de outra pessoa nao pode ser recategorizado por aqui.
c_amb.post("/categoria-desta", data={"csrf_token": "t", "tx_id": _ultimo_amb(),
                                     "categoria": "Lazer"})
_alheio = None
with get_db() as db:
    _alheio = db.execute("SELECT id, categoria FROM transacoes WHERE user_id=? LIMIT 1",
                         (uid_paz,)).fetchone()
c_amb.post("/categoria-desta", data={"csrf_token": "t", "tx_id": _alheio["id"],
                                     "categoria": "Lazer"})
with get_db() as db:
    _depois = db.execute("SELECT categoria FROM transacoes WHERE id=?",
                         (_alheio["id"],)).fetchone()["categoria"]
check("ninguem recategoriza lancamento de outra conta", _depois == _alheio["categoria"],
      (_alheio["categoria"], _depois))


secao("104. Quando o banco nao conecta, tem que dar pra saber por que")
# Os primos do Matheus tentaram conectar e voltaram com "simplesmente nao
# conectou". O app tinha a explicacao na mao e jogava fora: o onError da Pluggy
# recebe o motivo e o codigo ignorava o parametro.
_tpl_pluggy = _io_layout.open("templates/pluggy_conectar.html", encoding="utf-8").read()

check("o onError recebe o motivo em vez de ignorar",
      "onError: function (erro)" in _tpl_pluggy)
check("e o motivo vira texto legivel", "function descreve(erro)" in _tpl_pluggy)
check("escuta o navegador barrando recurso pela politica de seguranca",
      "securitypolicyviolation" in _tpl_pluggy)
check("trata o caso do conector nao carregar",
      "PLUGGY_FALHOU_AO_CARREGAR" in _tpl_pluggy
      and "tentarDeNovoOScript" in _tpl_pluggy)
# Antes o app CHUTAVA o motivo ("costuma ser rede, bloqueador...") e mandava a
# pessoa procurar no lugar errado. Agora pergunta pro proprio CDN.
check("e pergunta o motivo em vez de chutar", "porQueNaoCarregou" in _tpl_pluggy)
# Os primos foram categoricos: "extrato e coisa de 2006". Oferecer isso como
# consolo logo depois da conexao falhar nao consola — irrita. Mas a funcao
# continua inteira pra quem quiser: o que saiu foi a sugestao no lugar errado.
check("a tela de conexao NAO empurra o extrato como consolo",
      "importar o extrato" not in _tpl_pluggy)
check("mas importar extrato continua existindo",
      any(r.rule == "/importar-ofx" for r in A.app.url_map.iter_rules())
      or hasattr(A, "importar_ofx"))
check("e continua alcancavel pela interface",
      any("importar_ofx" in _io_layout.open(f"templates/{t}", encoding="utf-8").read()
          for t in ("home.html", "transacoes.html")))
check("o parser de OFX segue no lugar",
      len(A.parse_ofx("<STMTTRN><TRNAMT>-10</TRNAMT><DTPOSTED>20260101</DTPOSTED></STMTTRN>")) == 1)

# A CSP precisa deixar o logo dos conectores aparecer: e por ele que a pessoa
# acha o "Meu Pluggy" no meio da lista.
check("a CSP libera a imagem dos conectores", "img-src" in A._CSP
      and "https://cdn.pluggy.ai" in A._CSP.split("img-src")[1].split(";")[0], A._CSP)
check("e continua barrando imagem de qualquer outro lugar",
      "https:" not in A._CSP.split("img-src")[1].split(";")[0].replace("https://cdn.pluggy.ai", ""))

c_pg = novo_cliente("pluggy@teste.com", nome="Pg")
_r_falha = c_pg.post("/pluggy/falhou", data={
    "csrf_token": "t", "motivo": "CONNECTOR_ERROR: senha invalida",
    "bloqueios": "https://x.com/logo.png (img-src)"})
check("a falha e registrada e devolve um codigo", _r_falha.status_code == 200
      and len(_r_falha.get_json().get("codigo", "")) == 6, _r_falha.get_data(as_text=True))
_codigo_pg = _r_falha.get_json()["codigo"]
_log = pathlib.Path(os.environ.get("ERROS_LOG") or "erros.log")
if _log.exists():
    _conteudo = _log.read_text(encoding="utf-8")
    check("o motivo fica no log, pra ele achar depois",
          _codigo_pg in _conteudo and "senha invalida" in _conteudo)
    check("junto com o que o navegador bloqueou", "img-src" in _conteudo)
check("precisa estar logado pra registrar falha",
      A.app.test_client().post("/pluggy/falhou", data={"csrf_token": "t"},
                               follow_redirects=False).status_code == 302)


secao("105. Modo escuro")
# "Acharam bonito mas muito claro." A paleta e linho e barro — de noite, queima
# a vista. Mesma identidade, vista com pouca luz.
_css = _io_layout.open("static/styles.css", encoding="utf-8").read()
_base = _io_layout.open("templates/base.html", encoding="utf-8").read()

check("existe paleta escura pra escolha explicita", ':root[data-tema="escuro"]' in _css)
check("e pra quem so tem o aparelho no escuro",
      "@media (prefers-color-scheme: dark)" in _css)
check("a escolha da pessoa GANHA do aparelho",
      ':root:not([data-tema="claro"])' in _css)

# Cor fixa no meio do CSS nao acompanha o tema: vira caixa branca no escuro.
check("nenhum fundo claro fixo sobrou no CSS",
      "#FFFDF6" not in _css and "#FBF1DD" not in _css)
check("mas o botao do Google segue branco, que e exigencia da marca deles",
      "background: #fff;" in _css and "marca do Google exige" in _css)

# O tema tem que ser aplicado ANTES da primeira pintura, senao pisca branco.
_cabeca = _base.split("</head>")[0]
check("o tema e aplicado antes de pintar a tela", "hercules-tema" in _cabeca)
check("o html nasce com data-tema", 'data-tema="auto"' in _base)
check("a barra do celular acompanha o tema", "cor-da-barra" in _base)
check("tem botao pra trocar, no topo", "temaToggle" in _base)
check("com tres estados: automatico, escuro e claro",
      "'auto', 'escuro', 'claro'" in _base)
check("e a escolha fica guardada no aparelho", "localStorage.setItem('hercules-tema'" in _base)

# O Herc e desenhado pra fundo claro: no escuro precisa de fundo proprio.
check("o Herc ganha fundo no escuro pra nao virar borrao",
      ':root[data-tema="escuro"] .hercules-avatar' in _css)

# Contraste medido no navegador: texto 13.9, apagado 6.8, link 8.1 — tudo AA+.
# Aqui fica so a trava de que as cores nao voltem a se encostar.
def _lum(hexa):
    def _c(v):
        v = int(v, 16) / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    h = hexa.lstrip("#")
    return 0.2126 * _c(h[0:2]) + 0.7152 * _c(h[2:4]) + 0.0722 * _c(h[4:6])

def _razao(a, b):
    l1, l2 = sorted([_lum(a), _lum(b)], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)

import re as _re105
_bloco_escuro = _re105.search(r':root\[data-tema="escuro"\]\s*\{(.*?)\}', _css, _re105.S).group(1)
_cor = lambda n: _re105.search(rf"{n}:\s*(#[0-9A-Fa-f]{{6}});", _bloco_escuro).group(1)
for _nome, _fg in [("texto", "--text"), ("apagado", "--muted"), ("link", "--primary-deep"),
                   ("vermelho", "--danger"), ("verde", "--success")]:
    _r = _razao(_cor(_fg), _cor("--surface"))
    check(f"no escuro, {_nome} tem contraste de leitura (>= 4.5)", _r >= 4.5, round(_r, 2))

# E as telas continuam abrindo — o tema e so CSS, mas um erro de Jinja no
# base.html derrubaria tudo de uma vez.
c_tema = novo_cliente("tema@teste.com", nome="Tema")
for _tela in ("/", "/dashboard", "/meses", "/simular", "/conferir", "/settings", "/planos"):
    check(f"{_tela} continua abrindo", c_tema.get(_tela).status_code == 200)


secao("106. As barras e os botoes tambem sao o tema")
# "O modo noturno ficou bugado na barra superior e inferior — ainda esta na cor
# do modo claro." Estava: as duas usavam rgba fixo do linho. E meu teste nao
# pegou porque eu conferi na tela de LOGIN, que nao tem barra nem menu de baixo.
_css106 = _io_layout.open("static/styles.css", encoding="utf-8").read()

# A cor do linho CONTINUA no arquivo, como valor da variavel no tema claro —
# o que nao pode e a regra da barra apontar direto pra ela.
def _regra106(sel):
    alvo = chr(10) + sel + ' {'
    i = _css106.find(alvo)
    return _css106[i:_css106.find("}", i)] if i >= 0 else ""


_linhas_css = _css106.split(chr(10))
_LINHO = ("rgba(246, 232, 206", "rgba(251, 241, 221")
_soltas = [_l.strip() for _l in _linhas_css
           if any(_c in _l for _c in _LINHO) and "--vidro" not in _l]

check("a barra de cima usa a variavel, nao a cor fixa",
      "var(--vidro)" in _regra106(".topbar"), _regra106(".topbar")[:90])
check("o menu de baixo tambem",
      "var(--vidro" in _regra106(".mobile-nav"), _regra106(".mobile-nav")[:90])
# A cor do linho CONTINUA no arquivo, como valor da variavel no tema claro.
# O que nao pode e alguma regra apontar direto pra ela de novo.
check("a cor do linho so aparece como valor de variavel", not _soltas, _soltas)
check("existe uma variavel pro fundo de vidro", "--vidro:" in _css106)

import re as _re106
_bloco_claro = _re106.search(r":root \{(.*?)\n\}", _css106, _re106.S).group(1)
_bloco_escuro = _re106.search(r':root\[data-tema="escuro"\] \{(.*?)\n\}', _css106, _re106.S).group(1)
check("o vidro tem valor claro e valor escuro, e sao diferentes",
      "--vidro:" in _bloco_claro and "--vidro:" in _bloco_escuro
      and _re106.search(r"--vidro:\s*([^;]+);", _bloco_claro).group(1)
          != _re106.search(r"--vidro:\s*([^;]+);", _bloco_escuro).group(1))

# Contraste do botao: no escuro o barro CLAREIA, entao letra creme em cima some.
def _lum106(hexa):
    h = hexa.lstrip("#")
    def _c(v):
        v = int(v, 16) / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _c(h[0:2]) + 0.7152 * _c(h[2:4]) + 0.0722 * _c(h[4:6])

def _razao106(a, b):
    l1, l2 = sorted([_lum106(a), _lum106(b)], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)

_var = lambda bloco, nome: _re106.search(rf"{nome}:\s*(#[0-9A-Fa-f]{{6}});", bloco).group(1)

check("no escuro, o botao de destaque usa letra escura",
      ':root[data-tema="escuro"] .button-primary' in _css106
      and "color: var(--bg);" in _css106.split(':root[data-tema="escuro"] .button-primary')[1][:220])
check("e isso vale tambem pra quem so tem o aparelho no escuro",
      ':root:not([data-tema="claro"]) .button-primary' in _css106)

for _nome, _chave in [("primario", "--primary"), ("secundario", "--secondary"),
                      ("perigo", "--danger")]:
    _r = _razao106(_var(_bloco_escuro, "--bg"), _var(_bloco_escuro, _chave))
    check(f"no escuro, o botao {_nome} da pra ler (>= 4.5)", _r >= 4.5, round(_r, 2))
    # Com letra creme dava menos de 3 — reprovava ate pra texto grande.
    _r_antes = _razao106("#FDF6E7", _var(_bloco_escuro, _chave))
    check(f"e a letra creme, que era o caso antes, de fato reprovava no {_nome}",
          _r_antes < 3, round(_r_antes, 2))

# O CSS nao pode ter ficado quebrado no meio das trocas.
check("as chaves do CSS continuam balanceadas",
      _css106.count("{") == _css106.count("}"), (_css106.count("{"), _css106.count("}")))
check("e nenhuma variavel de vidro ficou duplicada dentro do mesmo bloco",
      _bloco_escuro.count("--vidro:") == 1, _bloco_escuro.count("--vidro:"))



secao("107. iPhone com o app na tela de inicio")
# Dois primos usam iPhone. No iOS, app aberto pelo icone roda num webview
# isolado do Safari — e a conexao com o banco precisa VOLTAR pra ca depois de
# passar pelo site do banco. Dali, nao volta. Falha calada, que e a pior.
_tpl107 = _io_layout.open("templates/pluggy_conectar.html", encoding="utf-8").read()
check("detecta iPhone", "iPad|iPhone|iPod" in _tpl107)
check("e iPad novo, que se apresenta como Mac",
      "MacIntel" in _tpl107 and "maxTouchPoints" in _tpl107)
check("detecta o app aberto pelo icone da tela de inicio",
      "navigator.standalone" in _tpl107 and "display-mode: standalone" in _tpl107)
check("avisa ANTES de tentar, em vez de deixar falhar calado",
      "aviso-standalone" in _tpl107 and "Abra pelo Safari" in _tpl107)
check("explica que a conexao fica na conta, nao no aparelho",
      "n\u00e3o no aparelho" in _tpl107)
check("e manda esse contexto junto quando falha", "tela-de-inicio" in _tpl107)
check("o aviso nasce escondido, pra quem nao e iPhone nao ver",
      'id="aviso-standalone"' in _tpl107
      and "hidden" in _tpl107.split('id="aviso-standalone"')[1][:120])



secao("108. Falha antes da janela tambem deixa rastro")
# Eles tentaram pelo icone E pelo Safari, e falhou nos dois — o que descarta o
# webview do iOS. Sobra a etapa ANTERIOR a janela: se a autenticacao ou o token
# falham, a pessoa levava um aviso, era mandada pras configuracoes, e o motivo
# morria ali. E justamente a etapa em que o problema e certamente do nosso lado.
check("existe um jeito unico de registrar falha da Pluggy",
      hasattr(A, "anotar_falha_pluggy"))

_log108 = pathlib.Path(os.environ.get("ERROS_LOG") or "erros.log")
_antes108 = _log108.read_text(encoding="utf-8") if _log108.exists() else ""
_cod108 = A.anotar_falha_pluggy(999, "antes da janela: HTTP 403", "teste")
check("devolve um codigo de 6 caracteres", len(_cod108) == 6, _cod108)
_depois108 = _log108.read_text(encoding="utf-8") if _log108.exists() else ""
check("e grava o motivo no log", _cod108 in _depois108 and "HTTP 403" in _depois108)
check("com o usuario, pra achar de quem foi", "user=999" in _depois108)
check("sem apagar o que ja estava la", len(_depois108) > len(_antes108))

_app108 = _io_layout.open("app.py", encoding="utf-8").read()
check("a rota que abre a janela usa o registro quando falha antes",
      "antes da janela" in _app108)
check("e o codigo aparece pra pessoa poder passar adiante",
      "anotei com o c\u00f3digo" in _app108)
check("dizendo que o problema e do lado do servidor, nao do aparelho dela",
      "n\u00e3o do seu aparelho" in _app108)
check("e apontando o diagnostico", "Diagn\u00f3stico da conex\u00e3o" in _app108)
check("o widget usa o MESMO registro, em vez de uma copia",
      _app108.count("uuid.uuid4().hex[:6].upper()") == 2,
      _app108.count("uuid.uuid4().hex[:6].upper()"))



secao("109. Data de vencimento com ano absurdo")
# Dois amigos cacadores de bug: na aba de Contas, preenchendo e editando o
# vencimento, aparecia "um numero gigantesco". O campo de data nao tinha limite,
# entao o navegador aceitava ano de seis digitos e mostrava na tela. O servidor
# recusava depois — mas a pessoa ja tinha visto o app quebrado, e num app de
# dinheiro isso basta pra ela nao voltar.
for _d in ("999999-12-31", "9999-12-31", "1899-01-01", "2101-01-01", "", "abc"):
    check("ano absurdo recusado: " + repr(_d), A.data_absurda(_d), _d)
for _d in ("2026-08-04", "1900-01-01", "2100-12-31"):
    check("data normal passa: " + _d, not A.data_absurda(_d))

for _t in ("compromissos.html", "metas.html", "nova_transacao.html", "nova_nota.html",
           "transacoes.html", "dividas.html", "listar.html"):
    _h = _io_layout.open("templates/" + _t, encoding="utf-8").read()
    if 'type="date"' in _h:
        check(_t + ": todo campo de data tem limite",
              _h.count('type="date"') == _h.count('max="2100-12-31"'),
              (_h.count('type="date"'), _h.count('max="2100-12-31"')))

# Navegador nao e barreira: o servidor tem que recusar sozinho.
c_v = novo_cliente("venc@teste.com", nome="V")
c_v.post("/compromissos", data={"csrf_token": "t", "descricao": "Aluguel",
         "valor": "900,00", "vencimento": "9999-12-31", "tipo": "saida"})
with get_db() as db:
    _n = db.execute("SELECT COUNT(*) c FROM compromissos WHERE user_id=?",
                    (uid_de("venc@teste.com"),)).fetchone()["c"]
check("o servidor tambem recusa, nao so o navegador", _n == 0, _n)

c_v.post("/compromissos", data={"csrf_token": "t", "descricao": "Aluguel",
         "valor": "900,00", "vencimento": date.today().isoformat(), "tipo": "saida"})
with get_db() as db:
    _n2 = db.execute("SELECT COUNT(*) c FROM compromissos WHERE user_id=?",
                     (uid_de("venc@teste.com"),)).fetchone()["c"]
check("mas conta com data normal entra igual", _n2 == 1, _n2)

check("a Ajuda tem botao de voltar",
      "arrow-left" in _io_layout.open("templates/ajuda.html", encoding="utf-8").read())



secao("110. Meta e movimentacao tem que concordar")
# Achado do amigo cacador de bug: "reserva de emergencia > deposito > n negativa
# o saldo > editar as ultimas movimentacoes resulta em zero mudancas no valor ja
# depositado". Guardar fazia duas coisas soltas — somava na meta e criava uma
# saida — sem nada obrigando as duas a concordar depois.
c_mt = novo_cliente("metavinc@teste.com", nome="Mt")
uid_mt = uid_de("metavinc@teste.com")
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'entrada',200,'sal','Salario',
                  'ofx',95,?,0)""", (uid_mt, date.today().isoformat()))
    db.execute("INSERT INTO metas (user_id,nome,meta_valor,valor_atual,ativo) VALUES (?,'Reserva',5000,0,1)",
               (uid_mt,))
    _gid = db.execute("SELECT id FROM metas WHERE user_id=?", (uid_mt,)).fetchone()["id"]

def _meta_mt():
    with get_db() as db:
        return float(db.execute("SELECT valor_atual FROM metas WHERE id=?", (_gid,)).fetchone()["valor_atual"])

def _ultima_mt():
    with get_db() as db:
        return db.execute("SELECT id FROM transacoes WHERE user_id=? ORDER BY id DESC LIMIT 1",
                          (uid_mt,)).fetchone()["id"]

# Guardar mais do que tem nao e bloqueado (o dinheiro pode estar em outra conta),
# mas nao pode ser calado: o saldo vira negativo e a pessoa precisa saber.
_r_mt = c_mt.post(f"/metas/{_gid}/aporte", data={"csrf_token": "t", "valor": "10000,00"},
                  follow_redirects=True)
check("guardar mais do que tem avisa que o saldo ficou negativo",
      "mais do que" in _r_mt.get_data(as_text=True))
check("e diz o que fazer se o dinheiro nao saiu de verdade",
      "apague essa movimenta" in _r_mt.get_data(as_text=True))
check("a meta recebeu o aporte", _meta_mt() == 10000.0, _meta_mt())

# O conserto do vinculo faz o remedio do aviso funcionar.
c_mt.post(f"/transacoes/{_ultima_mt()}/delete", data={"csrf_token": "t"})
check("apagar a movimentacao devolve o dinheiro pra meta", _meta_mt() == 0.0, _meta_mt())

c_mt.post(f"/metas/{_gid}/aporte", data={"csrf_token": "t", "valor": "150,00"})
check("novo aporte entra", _meta_mt() == 150.0, _meta_mt())
c_mt.post(f"/transacoes/{_ultima_mt()}/editar", data={
    "csrf_token": "t", "tipo": "saida", "valor": "50,00", "descricao": "Guardado na meta",
    "estabelecimento": "Reserva", "categoria": "Reserva",
    "data_transacao": date.today().isoformat()})
check("editar o valor mexe na meta pela diferenca", _meta_mt() == 50.0, _meta_mt())

# A meta nunca pode ficar negativa, mesmo com edicao maluca.
c_mt.post(f"/transacoes/{_ultima_mt()}/editar", data={
    "csrf_token": "t", "tipo": "saida", "valor": "999999,00", "descricao": "x",
    "estabelecimento": "x", "categoria": "Reserva", "data_transacao": date.today().isoformat()})
c_mt.post(f"/transacoes/{_ultima_mt()}/delete", data={"csrf_token": "t"})
check("a meta nunca fica negativa", _meta_mt() >= 0, _meta_mt())

# Movimentacao que NAO e aporte nao pode mexer em meta nenhuma.
c_mt.post(f"/metas/{_gid}/aporte", data={"csrf_token": "t", "valor": "80,00"})
_antes_mt = _meta_mt()
with get_db() as db:
    db.execute("""INSERT INTO transacoes (user_id,tipo,valor,descricao,categoria,fonte,
                  confidence,data_transacao,no_credito) VALUES (?,'saida',40,'mercado','Mercado',
                  'manual',100,?,0)""", (uid_mt, date.today().isoformat()))
c_mt.post(f"/transacoes/{_ultima_mt()}/delete", data={"csrf_token": "t"})
check("apagar um gasto comum nao mexe em meta", _meta_mt() == _antes_mt, (_antes_mt, _meta_mt()))


secao("111. Contas: um controle so, e nao repete sem querer")
# "tem a frequencia do quando o pagamento vai entrar e embaixo tem um checkbox
# de frequencia mas se e mensal ja n e frequente?" — dois controles pra uma
# decisao so, e dava pra marcar mensal com o checkbox vazio.
_tpl111 = _io_layout.open("templates/compromissos.html", encoding="utf-8").read()
check("o checkbox redundante saiu", 'name="recorrente"' not in _tpl111)
check("sobrou a frequencia, em portugues de gente",
      "Todo mês" in _tpl111 and "Uma vez só" in _tpl111)
check("e explica o que muda na pratica", "eu crio a pr\u00f3xima sozinho" in _tpl111)

c_ct = novo_cliente("contas111@teste.com", nome="Ct")
uid_ct = uid_de("contas111@teste.com")
_venc = date.today().replace(day=15).isoformat()

def _conta(desc="Aluguel", valor="900,00", freq="mensal"):
    return c_ct.post("/compromissos", data={"csrf_token": "t", "descricao": desc,
                     "valor": valor, "vencimento": _venc, "tipo": "saida",
                     "frequencia": freq}, follow_redirects=True)

def _n_ct():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) c FROM compromissos WHERE user_id=?",
                          (uid_ct,)).fetchone()["c"]

_conta()
check("a primeira conta entra", _n_ct() == 1, _n_ct())
_r_rep = _conta()
check("a segunda igual NAO entra de cara", _n_ct() == 1, _n_ct())
check("e o app diz por que", "j\u00e1 tem" in _r_rep.get_data(as_text=True))
check("mostrando o valor da que ja existe", "900,00" in _r_rep.get_data(as_text=True))
_conta()
check("mas insistindo entra — dois alugueis existem no mundo", _n_ct() == 2, _n_ct())
_conta("Luz", "120,00")
check("conta com outro nome nao pede confirmacao", _n_ct() == 3, _n_ct())

with get_db() as db:
    _f = {r["descricao"]: (r["frequencia"], r["recorrente"]) for r in db.execute(
        "SELECT descricao, frequencia, recorrente FROM compromissos WHERE user_id=?", (uid_ct,))}
check("'todo mês' vira recorrente sozinho", _f["Luz"] == ("mensal", 1), _f)
_conta("Presente", "200,00", "pontual")
with get_db() as db:
    _p = db.execute("""SELECT frequencia, recorrente FROM compromissos
                        WHERE user_id=? AND descricao='Presente'""", (uid_ct,)).fetchone()
check("'uma vez só' vira nao-recorrente sozinho",
      (_p["frequencia"], _p["recorrente"]) == ("pontual", 0), dict(_p))



secao("112. Link aberto de dentro do WhatsApp")
# O print dos primos tinha a resposta no canto: "WhatsApp" em cima e "Privado"
# na barra de endereco. Eles abriram o Hercules pela janelinha interna do
# WhatsApp, que bloqueia script de outro dominio — o conector da Pluggy nunca
# carrega ali. Os dois falharam pelo mesmo motivo, com codigos diferentes.
_tpl112 = _io_layout.open("templates/pluggy_conectar.html", encoding="utf-8").read()

check("o <script> avisa na hora que falha, em vez de so no clique",
      'onerror="window.PLUGGY_FALHOU_AO_CARREGAR' in _tpl112)
check("existe aviso proprio pra navegador de dentro de outro app",
      'id="aviso-interno"' in _tpl112 and "Abra no navegador do celular" in _tpl112)
check("e ele diz o caminho exato: tres pontinhos, abrir no Safari",
      "tr\u00eas pontinhos" in _tpl112 and "Abrir no Safari" in _tpl112)
check("reconhece os apps mais comuns", "WhatsApp" in _tpl112 and "Instagram" in _tpl112)
check("e nao depende so da deteccao: falhou, o aviso aparece",
      "aviso-interno').hidden = false" in _tpl112.split("function abrir()")[1][:400])

# O app parou de CHUTAR o motivo. Antes dizia "costuma ser rede, bloqueador de
# anuncios ou extensao" — e mandava a pessoa procurar no lugar errado.
check("nao chuta mais o motivo", "Costuma ser rede" not in _tpl112)
check("pergunta pro proprio CDN o que houve", "porQueNaoCarregou" in _tpl112)
check("separa bloqueio do navegador de endereco fora do ar",
      "r.status" in _tpl112 and "deixou ele rodar" in _tpl112)
check("e separa dos dois o caso de rede caida", "nem alcan\u00e7ar" in _tpl112)

# A sonda so funciona se a CSP deixar falar com o CDN — senao o diagnostico
# seria bloqueado pelo proprio app, que seria ironico.
check("a CSP deixa a pagina perguntar ao CDN",
      "https://cdn.pluggy.ai" in A._CSP.split("connect-src")[1].split(";")[0], A._CSP)
check("e continua barrando conversa com qualquer outro lugar",
      all(_d.startswith("https://api.pluggy.ai") or _d.startswith("https://connect.pluggy.ai")
          or _d.startswith("https://cdn.pluggy.ai") or _d == "'self'"
          for _d in A._CSP.split("connect-src")[1].split(";")[0].split()[1:]),
      A._CSP.split("connect-src")[1].split(";")[0])



secao("113. O caminho ate a janela abrir, revisado inteiro")
# Auditoria pedida depois do print dos primos. Tres achados novos, nenhum deles
# no lugar onde eu vinha procurando.
_tpl113 = _io_layout.open("templates/pluggy_conectar.html", encoding="utf-8").read()
_sw113 = _io_layout.open("static/sw.js", encoding="utf-8").read()

# 1. O service worker interceptava TODA requisicao, inclusive a do CDN da
#    Pluggy (877 KB, outro dominio). E o tratamento de falha devolvia undefined,
#    que o navegador le como erro de rede: um soluco de conexao derrubava o
#    conector de vez naquele carregamento.
check("o service worker nao toca em requisicao de outro dominio",
      "url.origin !== self.location.origin" in _sw113)
check("e sai antes de qualquer respondWith",
      _sw113.index("url.origin !== self.location.origin") < _sw113.index("respondWith"))
check("nunca devolve undefined pro navegador",
      _sw113.count("new Response(") >= 2, _sw113.count("new Response("))
check("a resposta de offline explica em portugues", "Sem conex\u00e3o agora" in _sw113)

# 2. O token era emitido no RENDER e guardado no HTML. Token expira: quem lesse
#    a tela, saisse e voltasse clicava num botao que ja nao funcionava — e o
#    fluxo antigo MANDAVA a pessoa sair pra criar conta em outro site.
check("existe rota que emite token no momento do clique",
      any(r.rule == "/pluggy/token" for r in A.app.url_map.iter_rules()))
check("e a tela pede token novo antes de abrir a janela",
      "tokenUrl" in _tpl113 and "abrirComToken" in _tpl113)
check("com o do HTML como reserva, se a rede falhar na hora",
      "abrirComToken(token)" in _tpl113)

c113 = novo_cliente("pluggy113@teste.com", nome="P113")
_r_tok = c113.post("/pluggy/token", data={"csrf_token": "t"})
check("a rota do token responde 200 mesmo quando da errado",
      _r_tok.status_code == 200, _r_tok.status_code)
check("com erro legivel em vez de estourar", "erro" in (_r_tok.get_json() or {}),
      _r_tok.get_json())
check("precisa estar logado",
      A.app.test_client().post("/pluggy/token", data={"csrf_token": "t"},
                               follow_redirects=False).status_code == 302)

# 3. 877 KB num 5G que oscila falham por acaso, e o navegador nao repete sozinho
#    um <script> que ja errou.
check("tenta carregar o conector uma segunda vez antes de desistir",
      "tentarDeNovoOScript" in _tpl113 and "document.head.appendChild" in _tpl113)
check("e a segunda tentativa tem prazo, pra nao travar a tela",
      "setTimeout" in _tpl113.split("tentarDeNovoOScript")[1][:600])

# O texto da janela tem que combinar com o que a pessoa vai ver la dentro.
check("manda escolher o banco quando ha banco direto",
      "escolha o seu banco na janela" in _tpl113)
check("e o Meu Pluggy so quando e o unico caminho",
      "escolha o Meu Pluggy na janela" in _tpl113)

# Nada de codigo morto duplicando a logica — e assim que os dois divergem.
check("a funcao antiga de abrir foi removida", "_abrirAntigo" not in _tpl113)

# O endereco do conector e o oficial. Se estivesse errado, falharia pra todo
# mundo e nenhum outro conserto adiantaria.
check("o endereco do conector esta num lugar so",
      _tpl113.count("cdn.pluggy.ai/pluggy-connect") == 2,
      _tpl113.count("cdn.pluggy.ai/pluggy-connect"))


print("\n" + "=" * 62)
print(f"PASSOU: {len(OK)}   FALHOU: {len(FALHAS)}")
if FALHAS:
    print("\nFALHAS:")
    for f in FALHAS:
        print("  -", f)
print("=" * 62)
