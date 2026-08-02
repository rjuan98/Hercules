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
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from database import DB_PATH

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or DB_PATH.parent / "backups")
MANTER_DIAS = 14

_lock = threading.Lock()


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
        origem = sqlite3.connect(DB_PATH)
        try:
            copia = sqlite3.connect(tmp)
            try:
                origem.backup(copia)   # retrato consistente, mesmo com o app escrevendo
            finally:
                copia.close()
        finally:
            origem.close()

        # grava com nome temporário e só então renomeia: se cair no meio,
        # o backup de ontem continua inteiro em vez de virar um arquivo pela metade
        parcial = destino.with_suffix(".parcial")
        with open(tmp, "rb") as f_in, gzip.open(parcial, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
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


if __name__ == "__main__":
    caminho = fazer_backup()
    tam = caminho.stat().st_size / 1024
    print(f"Backup salvo em {caminho} ({tam:.0f} KB)")
    print(f"Cópias guardadas: {len(listar_backups())} (mantendo as {MANTER_DIAS} mais recentes)")
