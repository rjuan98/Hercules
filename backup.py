"""Cópia de segurança do banco.

Roda de dois jeitos, de propósito:

  * sozinho, uma vez por dia, na primeira visita ao app (rede de segurança —
    funciona sem você configurar nada);
  * na mão ou por tarefa agendada:  python backup.py

Usa a API de backup online do SQLite, não `cp`. Copiar o arquivo com o app
escrevendo pode gerar uma cópia corrompida justamente no dia em que ela
importar; a API tira um retrato consistente mesmo com gente usando.

Importante saber o que isso NÃO cobre: a cópia fica no mesmo disco do
servidor. Protege contra migração ruim, apagão de tabela e corrupção do
arquivo — não protege contra perder o servidor inteiro. Pra isso é preciso
baixar uma cópia de vez em quando (Configurações → Saúde do app).

E é justamente por VIAJAR que ela precisa ir cifrada: o arquivo sai do
servidor, passa pelo seu computador e às vezes por uma nuvem qualquer.
Defina BACKUP_SENHA e ele deixa de ser legível fora daqui. Sem a variável
o backup continua funcionando em texto claro — o app avisa na tela de
Saúde, porque backup que não roda é pior que backup legível.
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from database import DB_PATH, SQLITE_TIMEOUT

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or DB_PATH.parent / "backups")
MANTER_DIAS = 14

# Senha que cifra a cópia. Fica no ambiente do servidor: quem tomar o servidor
# leva o banco de qualquer jeito, então ela não protege contra isso. O que ela
# protege é o arquivo depois que ele SAI daqui — que é pra onde ele vai.
BACKUP_SENHA = os.environ.get("BACKUP_SENHA") or ""
MAGICO = b"HERCBK1:"       # marca o arquivo cifrado e a versão do formato

_lock = threading.Lock()


def cifragem_ligada() -> bool:
    return bool(BACKUP_SENHA) and _fernet_disponivel()


def _fernet_disponivel() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _chave(salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    import base64
    bruta = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(BACKUP_SENHA.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(bruta))


def cifrar(dados: bytes) -> bytes:
    """Sal novo por arquivo: dois backups do mesmo banco não ficam idênticos."""
    salt = os.urandom(16)
    return MAGICO + salt + _chave(salt).encrypt(dados)


def decifrar(dados: bytes) -> bytes:
    if not dados.startswith(MAGICO):
        return dados                       # backup antigo, em texto claro
    if not BACKUP_SENHA:
        raise ValueError("Este backup está cifrado. Defina BACKUP_SENHA para abrir.")
    corpo = dados[len(MAGICO):]
    return _chave(corpo[:16]).decrypt(corpo[16:])


def esta_cifrado(caminho) -> bool:
    try:
        with open(caminho, "rb") as f:
            return f.read(len(MAGICO)) == MAGICO
    except OSError:
        return False


def _nome_do_dia(quando=None):
    return f"hercules-{(quando or datetime.now()).strftime('%Y-%m-%d')}.db.gz"


def listar_backups():
    """Do mais novo pro mais antigo."""
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("hercules-*.db.gz"), reverse=True)


def ultimo_backup():
    todos = listar_backups()
    if not todos:
        return None
    p = todos[0]
    return {"arquivo": p, "quando": datetime.fromtimestamp(p.stat().st_mtime),
            "tamanho": p.stat().st_size}


def _limpar_antigos():
    for velho in listar_backups()[MANTER_DIAS:]:
        try:
            velho.unlink()
        except OSError:
            pass


def fazer_backup():
    """Retorna o caminho do arquivo gerado. Levanta exceção se falhar."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destino = BACKUP_DIR / _nome_do_dia()

    fd, tmp = tempfile.mkstemp(suffix=".db", dir=str(BACKUP_DIR))
    os.close(fd)
    try:
        # Mesmo timeout do app: o backup não pode falhar só porque um sync
        # estava escrevendo naquele instante.
        origem = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
        try:
            copia = sqlite3.connect(tmp)
            try:
                origem.backup(copia)   # retrato consistente, mesmo com o app escrevendo
            finally:
                copia.close()
        finally:
            origem.close()

        with open(tmp, "rb") as f_in:
            conteudo = gzip.compress(f_in.read())
        if cifragem_ligada():
            conteudo = cifrar(conteudo)

        # grava com nome temporário e só então renomeia: se cair no meio,
        # o backup de ontem continua inteiro em vez de virar um arquivo pela metade
        parcial = destino.with_suffix(".parcial")
        parcial.write_bytes(conteudo)
        os.replace(parcial, destino)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    _limpar_antigos()
    return destino


def garantir_backup_do_dia():
    """Faz o backup se ainda não houve um nas últimas 24h. Nunca levanta exceção:
    backup que quebra a tela do usuário é pior do que backup que falha calado
    (o estado aparece em Configurações → Saúde do app)."""
    if not _lock.acquire(blocking=False):
        return False                      # outra requisição já está fazendo
    try:
        ultimo = ultimo_backup()
        if ultimo and datetime.now() - ultimo["quando"] < timedelta(hours=24):
            return False
        fazer_backup()
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False
    finally:
        _lock.release()


def restaurar(arquivo, destino) -> Path:
    """Decifra e descompacta uma cópia, devolvendo um .db pronto pra usar.

    Existe pra que a restauração seja um comando, não uma pesquisa no dia em que
    o banco quebrar. É o único momento em que o backup vale alguma coisa.

    Aceita o NOME da cópia ("hercules-2026-08-03.db.gz") ou o caminho inteiro.
    Só o nome porque é isso que o próprio comando manda copiar depois do backup —
    e ele quebrava com FileNotFoundError, já que as cópias moram em outra pasta.
    Descobrir isso no dia em que o banco quebrou seria descobrir tarde demais.
    """
    caminho = Path(arquivo)
    if not caminho.exists():
        na_pasta = BACKUP_DIR / caminho.name
        if na_pasta.exists():
            caminho = na_pasta
        else:
            disponiveis = [c.name for c in listar_backups()]
            partes = [f"Não achei a cópia '{arquivo}'."]
            if disponiveis:
                partes.append(f"Cópias que existem em {BACKUP_DIR}:")
                partes.extend("  " + nome for nome in disponiveis[:10])
            else:
                partes.append(f"Não há nenhuma cópia em {BACKUP_DIR}.")
            raise FileNotFoundError("\n".join(partes))
    bruto = caminho.read_bytes()
    conteudo = gzip.decompress(decifrar(bruto))
    destino = Path(destino)
    destino.write_bytes(conteudo)

    conferencia = sqlite3.connect(destino)
    try:
        estado = conferencia.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conferencia.close()
    if estado != "ok":
        raise ValueError(f"A cópia abriu, mas está corrompida: {estado}")
    return destino


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--restaurar":
        saida = sys.argv[3] if len(sys.argv) > 3 else "restaurado.db"
        print(f"Restaurado em {restaurar(sys.argv[2], saida)} — íntegro ✓")
        print("Confira os dados e só então ponha no lugar do database.db.")
        raise SystemExit(0)

    caminho = fazer_backup()
    tam = caminho.stat().st_size / 1024
    print(f"Backup salvo em {caminho} ({tam:.0f} KB)")
    print(f"Cifrado: {'sim' if esta_cifrado(caminho) else 'NÃO — defina BACKUP_SENHA'}")
    print(f"Cópias guardadas: {len(listar_backups())} (mantendo as {MANTER_DIAS} mais recentes)")
    print(f"\nPra restaurar:  python backup.py --restaurar {caminho.name} saida.db")
