"""Bateria completa do Hércules.

Roda contra um banco temporário e isolado — não toca nos seus dados.
Use sempre que mexer no código:  python testes.py
"""
import os, sys, json, tempfile, traceback
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

def novo_cliente(email, senha="senha123", nome="Fulano", perfil="pf"):
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
                               "senha": "outra123", "perfil": "pf"}, follow_redirects=True)
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
check("avisa pra escolher Meu Pluggy, nao o banco", "e não o seu banco direto" in h_aj)
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
check("o botao do olhinho aparece", 'id="eyeToggle"' in h_home)
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
check("deslogado nao tem olhinho", 'id="eyeToggle"' not in _an.get("/login").get_data(as_text=True))

print("\n" + "=" * 62)
print(f"PASSOU: {len(OK)}   FALHOU: {len(FALHAS)}")
if FALHAS:
    print("\nFALHAS:")
    for f in FALHAS:
        print("  -", f)
print("=" * 62)
