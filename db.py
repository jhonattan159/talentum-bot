# db.py
"""Abstracción de base de datos del chatbot de WhatsApp de TALENTUM GROUP.

Usa PostgreSQL cuando `DATABASE_URL` está configurada (Render, persistente
entre reinicios) y SQLite local (`DB_PATH` o conversations.db) como respaldo.
Mismos esquemas en ambos motores.

La conexión se abre por operación (seguro con hilos), igual que el código
anterior. Los secretos solo se leen de variables de entorno; nunca se
registran en logs.

Compatibilidad: el código existente usa la interfaz estilo sqlite3
(`conn.execute(sql, params)` con placeholders `?` y filas `row["col"]`).
En PostgreSQL se adapta automáticamente (`?` -> `%s`, filas dict).
"""

import logging
import os
import sqlite3

log = logging.getLogger("talentum.db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.db"),
)

try:
    import psycopg2
    import psycopg2.extras

    _HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover - entorno sin postgres
    psycopg2 = None
    _HAS_PSYCOPG2 = False


def is_postgres():
    """True si hay DATABASE_URL y el driver está instalado."""
    return bool(DATABASE_URL) and _HAS_PSYCOPG2


def _pg_sql(sql):
    """Traduce placeholders `?` a `%s` para psycopg2.

    El SQL del proyecto no usa `?` literales dentro de strings, por lo que
    el reemplazo directo es seguro aquí.
    """
    return sql.replace("?", "%s")


def _ddl():
    """DDL portable para ambos motores (tipos ajustados por motor)."""
    id_col = "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts_col = "DOUBLE PRECISION" if is_postgres() else "REAL"
    return (
        """
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            fails INTEGER NOT NULL DEFAULT 0,
            updated_at %s NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leads (
            id %s,
            phone TEXT NOT NULL,
            name TEXT,
            area TEXT,
            goal TEXT,
            service TEXT,
            intent TEXT,
            note TEXT,
            created_at %s NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gemini_usage (
            id %s,
            ts %s NOT NULL,
            key_index INTEGER NOT NULL,
            model TEXT NOT NULL,
            operation TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            candidates_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        );
        """
        % (ts_col, id_col, ts_col, id_col, ts_col)
    )


class _PgConn:
    """Adapta psycopg2 a la interfaz sqlite3 usada por el proyecto."""

    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        self._cur.execute(_pg_sql(sql), params or ())
        return self

    def executescript(self, sql):
        # psycopg2 acepta múltiples sentencias en un solo execute()
        self._cur.execute(sql)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._cur.close()
        finally:
            self._conn.close()


def connect():
    """Abre una conexión nueva en cada operación.

    PostgreSQL si `DATABASE_URL` está configurada (y psycopg2 instalado);
    en cualquier otro caso, SQLite local.
    """
    if DATABASE_URL and not _HAS_PSYCOPG2:
        log.warning(
            "DATABASE_URL configurada pero psycopg2 no está instalado; "
            "usando SQLite como respaldo."
        )
    if is_postgres():
        return _PgConn(psycopg2.connect(DATABASE_URL))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea las tablas si no existen. Idempotente."""
    conn = connect()
    try:
        conn.executescript(_ddl())
        conn.commit()
    finally:
        conn.close()
    log.info(
        "Base inicializada (%s).",
        "PostgreSQL" if is_postgres() else f"SQLite en {DB_PATH}",
    )
