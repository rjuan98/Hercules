import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Em produção aponte DATABASE_PATH para um disco persistente (ex.: /var/data/database.db)
DB_PATH = Path(os.environ.get("DATABASE_PATH") or BASE_DIR / "database.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(conn, table_name, column_name):
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return any(row["name"] == column_name for row in cursor.fetchall())


def _add_column_if_missing(conn, table_name, column_name, column_type):
    if not _column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            perfil TEXT NOT NULL DEFAULT 'pf',
            home_focus TEXT NOT NULL DEFAULT 'saldo',
            notification_mode TEXT NOT NULL DEFAULT 'equilibrado',
            meta_mensal REAL NOT NULL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            descricao TEXT NOT NULL,
            valor REAL NOT NULL,
            arquivo TEXT,
            data_emissao TEXT,
            categoria TEXT NOT NULL DEFAULT 'Outros',
            cliente TEXT,
            cnpj_emitente TEXT,
            numero_nota TEXT,
            status TEXT NOT NULL DEFAULT 'Autorizada',
            tipo TEXT NOT NULL DEFAULT 'saida',
            data_upload TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nota_id INTEGER,
            tipo TEXT NOT NULL CHECK(tipo IN ('entrada', 'saida')),
            valor REAL NOT NULL,
            descricao TEXT,
            estabelecimento TEXT,
            categoria TEXT,
            data_transacao TEXT,
            fonte TEXT NOT NULL DEFAULT 'manual',
            confidence INTEGER NOT NULL DEFAULT 100,
            needs_review INTEGER NOT NULL DEFAULT 0,
            extra_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE,
            FOREIGN KEY (nota_id) REFERENCES notas (id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS capturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            origem TEXT NOT NULL DEFAULT 'manual',
            conteudo TEXT,
            arquivo TEXT,
            status TEXT NOT NULL DEFAULT 'pendente',
            dados_extraidos TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            meta_valor REAL NOT NULL DEFAULT 0.0,
            valor_atual REAL NOT NULL DEFAULT 0.0,
            ativo INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compromissos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            descricao TEXT NOT NULL,
            valor REAL NOT NULL,
            vencimento TEXT NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'saida',
            status TEXT NOT NULL DEFAULT 'pendente',
            recorrente INTEGER NOT NULL DEFAULT 0,
            frequencia TEXT NOT NULL DEFAULT 'mensal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            icone TEXT,
            cor TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regras_categorizacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            padrao_texto TEXT NOT NULL,
            categoria_nome TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dependentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            cpf TEXT,
            parentesco TEXT,
            data_nascimento TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            documento TEXT,
            email TEXT,
            telefone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            valor_padrao REAL NOT NULL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
        """
    )

    # Useful indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notas_user_date ON notas(user_id, data_upload DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notas_user_tipo ON notas(user_id, tipo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transacoes_user_date ON transacoes(user_id, data_transacao DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_compromissos_user_venc ON compromissos(user_id, vencimento)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metas_user_ativo ON metas(user_id, ativo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_capturas_user_status ON capturas(user_id, status)")

    # Migrations for evolving copies
    _add_column_if_missing(conn, "usuarios", "perfil", "TEXT NOT NULL DEFAULT 'pf'")
    _add_column_if_missing(conn, "usuarios", "home_focus", "TEXT NOT NULL DEFAULT 'saldo'")
    _add_column_if_missing(conn, "usuarios", "notification_mode", "TEXT NOT NULL DEFAULT 'equilibrado'")
    _add_column_if_missing(conn, "usuarios", "meta_mensal", "REAL NOT NULL DEFAULT 0.0")

    _add_column_if_missing(conn, "transacoes", "nota_id", "INTEGER")
    _add_column_if_missing(conn, "transacoes", "fonte", "TEXT NOT NULL DEFAULT 'manual'")
    _add_column_if_missing(conn, "transacoes", "confidence", "INTEGER NOT NULL DEFAULT 100")
    _add_column_if_missing(conn, "transacoes", "needs_review", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "transacoes", "extra_json", "TEXT")

    _add_column_if_missing(conn, "usuarios", "view_mode", "TEXT NOT NULL DEFAULT 'completo'")
    _add_column_if_missing(conn, "categorias", "limite_mensal", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing(conn, "categorias", "created_at", "TIMESTAMP")
    # Cópias antigas do banco tinham categoria_id e nenhum timestamp nas regras
    _add_column_if_missing(conn, "regras_categorizacao", "categoria_nome", "TEXT")
    _add_column_if_missing(conn, "regras_categorizacao", "created_at", "TIMESTAMP")

    _add_column_if_missing(conn, "metas", "valor_atual", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing(conn, "metas", "ativo", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "metas", "prazo", "TEXT")

    _add_column_if_missing(conn, "compromissos", "recorrente", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "compromissos", "frequencia", "TEXT NOT NULL DEFAULT 'mensal'")
    _add_column_if_missing(conn, "compromissos", "status", "TEXT NOT NULL DEFAULT 'pendente'")

    conn.commit()
    conn.close()
