"""Marca o que é dinheiro seu trocando de bolso, não receita nem gasto.

Guardar na caixinha do Nubank aparece no extrato como "Aplicação RDB"; tirar de
lá, como "Resgate RDB". Sem reconhecer isso, o app soma o seu próprio dinheiro
como se fosse salário quando volta, e como se fosse gasto quando sai — e quem
usa caixinha mexe nela todo mês.

O import já marca sozinho daqui pra frente. Este script cuida do que entrou
antes. Ele NÃO altera nada por padrão: mostra o que encontrou e você decide.

    python marcar_internos.py seu@email.com              # só mostra
    python marcar_internos.py seu@email.com --aplicar    # marca

O saldo não muda: o dinheiro trocou de conta de verdade. O que muda é "entrou",
"saiu" e o ritmo diário, que passam a contar só o que entrou e saiu da sua vida.
"""
import sys

from app import e_movimento_interno
from database import get_db

COL = "date(COALESCE(NULLIF(data_transacao, ''), created_at))"


def reais(v):
    return f"R$ {v:>11,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    email = sys.argv[1].strip().lower()
    aplicar = "--aplicar" in sys.argv

    with get_db() as db:
        u = db.execute("SELECT id, nome FROM usuarios WHERE LOWER(email) = ?", (email,)).fetchone()
        if not u:
            print(f"Não achei ninguém com o email {email}.")
            raise SystemExit(1)

        linhas = db.execute(
            f"""SELECT id, tipo, valor, descricao, {COL} AS dia
                  FROM transacoes
                 WHERE user_id = ? AND interno = 0
                 ORDER BY {COL} DESC""",
            (u["id"],),
        ).fetchall()

    achados = [r for r in linhas if e_movimento_interno(r["descricao"])]
    if not achados:
        print(f"\n{u['nome']}: nenhum movimento entre contas suas encontrado.")
        print("Se você usa caixinha e mesmo assim não apareceu nada, me mostre uma")
        print("linha do extrato como ela aparece — o padrão pode ter outro nome.\n")
        return

    entrou = sum(float(r["valor"]) for r in achados if r["tipo"] == "entrada")
    saiu = sum(float(r["valor"]) for r in achados if r["tipo"] == "saida")

    print(f"\n{u['nome']}: {len(achados)} lançamento(s) que são dinheiro seu mudando de lugar\n")
    for r in achados[:30]:
        sinal = "+" if r["tipo"] == "entrada" else "−"
        print(f"  {r['dia']}  {sinal}{reais(float(r['valor']))}  {(r['descricao'] or '')[:46]}")
    if len(achados) > 30:
        print(f"  … e mais {len(achados) - 30}")

    print(f"\nDepois de marcar, deixam de contar como receita/gasto:")
    print(f"  'entrou' cai   {reais(entrou)}")
    print(f"  'saiu' cai     {reais(saiu)}")
    print(f"  o saldo NÃO muda — esse dinheiro trocou de conta de verdade.")

    if not aplicar:
        print("\nNada foi alterado. Confira a lista acima: se TUDO ali é dinheiro seu")
        print("indo pra uma conta sua (e nenhum salário ou pagamento de cliente), rode:")
        print(f"  python backup.py && python marcar_internos.py {email} --aplicar\n")
        return

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("UPDATE transacoes SET interno = 1 WHERE id = ?",
                       [(r["id"],) for r in achados])
        db.commit()
    print(f"\nMarquei {len(achados)} lançamento(s). ✓")
    print("Abra a tela de Meses: 'entrou' e 'saiu' agora devem bater com o banco.\n")


if __name__ == "__main__":
    main()
