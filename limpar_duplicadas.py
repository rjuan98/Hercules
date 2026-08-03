"""Acha e remove lançamentos que entraram duas vezes.

Quem conectou o banco pela Pluggy E importou o extrato OFX do mesmo período
ficou com cada compra registrada duas vezes — uma com id `PLG-…` e outra com o
FITID do banco. O import já não deixa mais isso acontecer, mas as linhas que
entraram antes continuam lá, inflando saldo, gasto do mês e ritmo diário.

Este script NÃO apaga nada por padrão. Ele mostra o que encontrou e você decide.

    python limpar_duplicadas.py                 # só mostra
    python limpar_duplicadas.py --apagar        # apaga, depois de um backup

Uma duplicata aqui é: mesmo usuário, mesmo tipo, mesmo valor (até o centavo),
mesmo dia, e ids de PROVEDORES diferentes. Duas compras iguais de verdade no
mesmo dia — dois cafés de R$ 5 — têm ids do mesmo provedor e não entram na
conta. Na dúvida, o script deixa quieto: é melhor sobrar do que apagar
movimentação de verdade.
"""
import sys
from collections import defaultdict

# A regra de "de onde veio esse id" vem do app, nao de uma copia aqui: se ela
# mudar la e nao aqui, este script passa a apagar a linha errada.
from app import _provedor_do_fitid as _provedor
from database import get_db


def encontrar():
    """Grupos (usuário, tipo, valor, dia) com mais de um provedor representado."""
    with get_db() as db:
        linhas = db.execute(
            """SELECT id, user_id, tipo, valor, descricao, fitid, fonte,
                      date(COALESCE(NULLIF(data_transacao, ''), created_at)) AS dia
                 FROM transacoes
                ORDER BY user_id, dia, tipo, valor, id"""
        ).fetchall()

    grupos = defaultdict(list)
    for r in linhas:
        grupos[(r["user_id"], r["tipo"], round(float(r["valor"]), 2), r["dia"])].append(r)

    achados = []
    for chave, rs in grupos.items():
        por_provedor = defaultdict(list)
        for r in rs:
            por_provedor[_provedor(r["fitid"])].append(r)
        if len(por_provedor) < 2:
            continue
        # Quantas cópias reais existem = o maior número visto num único provedor.
        # Se a Pluggy trouxe 2 cafés e o extrato trouxe os mesmos 2, o certo é 2.
        reais = max(len(v) for v in por_provedor.values())
        sobrando = len(rs) - reais
        if sobrando <= 0:
            continue
        # Quem fica, em ordem:
        #   1. Pluggy — ela volta todo dia. Apagar essa linha seria trocar seis
        #      por meia dúzia: o próximo sync traria de volta.
        #   2. Sem id — foi anotado na mão ou capturado, então carrega a
        #      descrição e a categoria que a PESSOA escolheu. Apagar isso perde
        #      trabalho dela; e se o arquivo for reimportado, a reconciliação
        #      casa com essa linha de novo.
        #   3. Arquivo (PDF, OFX) — ninguém reimporta sem querer, e o conteúdo
        #      é o texto cru do banco.
        ordem = {"pluggy": 0, None: 1, "pdf": 2, "ofx": 3}
        rs_ord = sorted(rs, key=lambda r: (ordem[_provedor(r["fitid"])], r["id"]))
        achados.append({"chave": chave, "manter": rs_ord[:reais], "apagar": rs_ord[reais:]})
    return achados


def main():
    apagar = "--apagar" in sys.argv
    achados = encontrar()

    if not achados:
        print("Nenhum lançamento duplicado. Nada a fazer. ✓")
        return

    total = sum(len(a["apagar"]) for a in achados)
    dinheiro = defaultdict(float)
    print(f"Encontrei {total} lançamento(s) que entraram duas vezes:\n")
    for a in achados[:40]:
        uid, tipo, valor, dia = a["chave"]
        for r in a["apagar"]:
            sinal = "+" if tipo == "entrada" else "-"
            print(f"  usuário {uid}  {dia}  {sinal}R$ {valor:>10,.2f}  "
                  f"{(r['descricao'] or '')[:32]:<32} id={r['fitid']}")
            dinheiro[uid] += valor if tipo == "entrada" else -valor
    if len(achados) > 40:
        print(f"  … e mais {len(achados) - 40} grupo(s)")

    print("\nEfeito no saldo depois de limpar:")
    for uid, delta in sorted(dinheiro.items()):
        print(f"  usuário {uid}: {'+' if -delta > 0 else ''}R$ {-delta:,.2f}")

    if not apagar:
        print("\nNada foi apagado. Se os números acima fazem sentido, rode:")
        print("  python backup.py && python limpar_duplicadas.py --apagar")
        return

    ids = [r["id"] for a in achados for r in a["apagar"]]
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("DELETE FROM transacoes WHERE id = ?", [(i,) for i in ids])
        db.commit()
    print(f"\nApaguei {len(ids)} lançamento(s) repetidos. ✓")
    print("Confira o saldo na tela — ele deve bater com o do banco agora.")


if __name__ == "__main__":
    main()
