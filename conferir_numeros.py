"""Abre a conta do ritmo diário, mês a mês, pra comparar com o extrato do banco.

Serve pra responder uma pergunta só: quando o Hércules diz que você gasta
R$ X por dia, de onde sai esse X?

    python conferir_numeros.py seu@email.com

Só imprime os números de UMA pessoa, a que você pedir. Nada sai da máquina.
"""
import sys
from collections import defaultdict
from datetime import date, timedelta

from database import get_db

COL = "date(COALESCE(NULLIF(data_transacao, ''), created_at))"


def reais(v):
    return f"R$ {v:>12,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _linha(rotulo, valor, nota=""):
    print(f"    {rotulo:<38} {reais(valor)}  {nota}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    email = sys.argv[1].strip().lower()

    with get_db() as db:
        u = db.execute("SELECT id, nome, dia_virada FROM usuarios WHERE LOWER(email) = ?",
                       (email,)).fetchone()
        if not u:
            print(f"Não achei ninguém com o email {email}.")
            raise SystemExit(1)
        uid = u["id"]

        print(f"\n=== {u['nome']} ===")
        if u["dia_virada"]:
            print(f"    (mês começa no dia {u['dia_virada']})")

        # 1. Como as movimentações estão marcadas. Se compra de cartão estiver
        #    como débito, ela conta no dia da compra E de novo na fatura.
        print("\n--- COMO SEUS LANÇAMENTOS ESTÃO MARCADOS ---")
        for r in db.execute(
            """SELECT fonte, no_credito, tipo, COUNT(*) n, COALESCE(SUM(valor),0) t
                 FROM transacoes WHERE user_id = ?
                GROUP BY fonte, no_credito, tipo ORDER BY t DESC""", (uid,)):
            marca = "CARTÃO" if r["no_credito"] else "conta "
            print(f"    {r['fonte']:<10} {marca}  {r['tipo']:<8} {r['n']:>5} lanç.  {reais(r['t'])}")

        # 2. Mês a mês, do jeito que o app conta — pra comparar com o banco.
        print("\n--- MÊS A MÊS (compare com o resumo do seu banco) ---")
        print(f"    {'mês':<10} {'entrou':>16} {'saiu da conta':>16} {'no cartão':>16}")
        for r in db.execute(
            f"""SELECT strftime('%Y-%m', {COL}) mes,
                   COALESCE(SUM(CASE WHEN tipo='entrada' AND no_credito=0
                                      AND fonte!='ajuste' THEN valor END),0) entrou,
                   COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=0
                                      AND fonte!='ajuste' THEN valor END),0) saiu,
                   COALESCE(SUM(CASE WHEN tipo='saida' AND no_credito=1 THEN valor END),0) cartao
                 FROM transacoes WHERE user_id = ?
                GROUP BY mes ORDER BY mes DESC LIMIT 6""", (uid,)):
            print(f"    {r['mes']:<10} {reais(r['entrou'])} {reais(r['saiu'])} {reais(r['cartao'])}")

        # 3. A conta do ritmo, aberta.
        hoje = date.today()
        desde = hoje - timedelta(days=60)
        print(f"\n--- O RITMO DIÁRIO, ABERTO ({desde:%d/%m} até {hoje:%d/%m}) ---")
        b = db.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN no_credito=0 AND fonte!='ajuste'
                       AND COALESCE(NULLIF(categoria,''),'')!='Reserva' THEN valor END),0) conta,
                  COALESCE(SUM(CASE WHEN no_credito=1 THEN valor END),0) cartao,
                  COALESCE(SUM(CASE WHEN fonte='ajuste' THEN valor END),0) ajuste,
                  COALESCE(SUM(CASE WHEN COALESCE(NULLIF(categoria,''),'')='Reserva'
                       AND no_credito=0 THEN valor END),0) guardado,
                  MIN({COL}) primeiro, COUNT(DISTINCT {COL}) dias
                FROM transacoes
               WHERE user_id=? AND tipo='saida' AND {COL} >= date(?)""",
            (uid, desde.isoformat())).fetchone()

        _linha("entra na conta do ritmo", b["conta"])
        _linha("fora: compras no cartão", b["cartao"], "(sai quando a fatura vence)")
        _linha("fora: guardado em meta", b["guardado"], "(é seu, não é gasto)")
        _linha("fora: ajuste de saldo", b["ajuste"], "(correção, não gasto)")

        if b["primeiro"]:
            primeiro = date.fromisoformat(b["primeiro"])
            corridos = max(1, (hoje - max(primeiro, desde)).days + 1)
            print(f"\n    dividido por {corridos} dias corridos (desde {primeiro:%d/%m})")
            print(f"    → sem cortar nada:  {reais(float(b['conta']) / corridos)} por dia")

        # 4. Os dias que puxam a média pra cima.
        print("\n--- SEUS 12 MAIORES DIAS (é aqui que a média mora) ---")
        for r in db.execute(
            f"""SELECT {COL} dia, SUM(valor) t, COUNT(*) n,
                       GROUP_CONCAT(descricao, ' | ') o_que
                  FROM transacoes
                 WHERE user_id=? AND tipo='saida' AND no_credito=0 AND fonte!='ajuste'
                   AND COALESCE(NULLIF(categoria,''),'')!='Reserva' AND {COL} >= date(?)
                 GROUP BY dia ORDER BY t DESC LIMIT 12""",
            (uid, desde.isoformat())):
            dia = date.fromisoformat(r["dia"]).strftime("%d/%m")
            print(f"    {dia}  {reais(r['t'])}  ({r['n']} lanç.)  {(r['o_que'] or '')[:52]}")

        # 5. Repetidos — o que o limpar_duplicadas.py tiraria.
        print("\n--- LANÇAMENTOS REPETIDOS ---")
        grupos = defaultdict(list)
        for r in db.execute(
            f"""SELECT id, tipo, valor, fitid, {COL} dia FROM transacoes
                 WHERE user_id=? AND {COL} >= date(?)""", (uid, desde.isoformat())):
            grupos[(r["tipo"], round(float(r["valor"]), 2), r["dia"])].append(r["fitid"])

        def prov(f):
            if not f:
                return None
            return "pluggy" if f.startswith("PLG-") else ("pdf" if f.startswith("PDF-") else "ofx")

        repetidos, dinheiro = 0, 0.0
        for (tipo, valor, dia), fitids in grupos.items():
            por = defaultdict(int)
            for f in fitids:
                por[prov(f)] += 1
            if len(por) > 1:
                sobra = len(fitids) - max(por.values())
                if sobra > 0:
                    repetidos += sobra
                    if tipo == "saida":
                        dinheiro += sobra * valor
        if repetidos:
            print(f"    {repetidos} lançamento(s) repetidos, {reais(dinheiro)} de gasto fantasma")
            print("    → rode:  python limpar_duplicadas.py")
        else:
            print("    nenhum. ✓")

        print("\nCompare a coluna 'saiu da conta' com o resumo do seu banco.")
        print("Se bater, o ritmo está certo. Se não bater, me mostre esta saída.\n")


if __name__ == "__main__":
    main()
