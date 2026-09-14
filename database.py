import logging
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List, Sequence
from urllib.parse import urlsplit

from config import DB_PATH, DATABASE_URL

logger = logging.getLogger("kwork_bot.db")

# PostgreSQL, если задан DATABASE_URL (например, база на Bothost), иначе локальный файл SQLite
USE_POSTGRES = bool(DATABASE_URL)
# В PostgreSQL таблица с префиксом: одну базу можно делить с другими ботами
TABLE = "kwork_orders" if USE_POSTGRES else "orders"

COLUMNS = (
    "kwork_id", "title", "description", "budget_info", "desired_price", "is_feasible", "reasoning", "tech_stack",
    "proposal_title", "proposal_text", "duration_days", "status", "error_msg", "created_at", "updated_at", "form_price"
)

_pg_conn = None

def _pg_connection():
    """Постоянное соединение с PostgreSQL (создаётся при первом запросе и после обрыва)."""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        import psycopg
        from psycopg.rows import dict_row
        _pg_conn = psycopg.connect(
            DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=15,
            keepalives=1, keepalives_idle=60, keepalives_interval=10, keepalives_count=3
        )
    return _pg_conn

def _drop_pg_connection() -> None:
    global _pg_conn
    try:
        if _pg_conn is not None:
            _pg_conn.close()
    except Exception:
        pass
    _pg_conn = None

def _execute(sql: str, params: Sequence = (), fetch: Optional[str] = None):
    """
    Выполняет запрос в PostgreSQL или SQLite (плейсхолдеры в SQL — «?»).
    fetch: None — без результата, "one" — одна строка (dict или None), "all" — список строк.
    """
    if USE_POSTGRES:
        import psycopg
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                with _pg_connection().cursor() as cursor:
                    cursor.execute(sql, params)
                    if fetch == "one":
                        return cursor.fetchone()
                    if fetch == "all":
                        return cursor.fetchall()
                    return None
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                _drop_pg_connection()
                if attempt == 2:
                    raise
                logger.warning(f"Соединение с PostgreSQL прервано ({e}) — переподключаюсь")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            cursor = conn.execute(sql, params)
            if fetch == "one":
                row = cursor.fetchone()
                return dict(row) if row else None
            if fetch == "all":
                return [dict(row) for row in cursor.fetchall()]
            return None
    finally:
        conn.close()

def describe_database() -> str:
    """Какая база используется — для лога при запуске (без пароля)."""
    if USE_POSTGRES:
        parts = urlsplit(DATABASE_URL)
        return f"PostgreSQL {parts.hostname}:{parts.port}{parts.path}, таблица {TABLE}"
    return f"SQLite {DB_PATH}"

def init_db() -> None:
    """Создаёт таблицу заказов. При первом запуске с PostgreSQL переносит в неё заказы из файла SQLite."""
    _execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            kwork_id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            budget_info TEXT,
            desired_price INTEGER,
            is_feasible INTEGER,
            reasoning TEXT,
            tech_stack TEXT,
            proposal_title TEXT,
            proposal_text TEXT,
            duration_days INTEGER,
            status TEXT,
            error_msg TEXT,
            created_at TEXT,
            updated_at TEXT,
            form_price INTEGER
        )
    """)
    # Цена для формы Kwork (form_price) появилась позже — добавляется в уже существующие таблицы
    if USE_POSTGRES:
        _execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS form_price INTEGER")
        _migrate_from_sqlite()
    else:
        columns = {row["name"] for row in _execute(f"PRAGMA table_info({TABLE})", fetch="all")}
        if "form_price" not in columns:
            _execute(f"ALTER TABLE {TABLE} ADD COLUMN form_price INTEGER")

def _migrate_from_sqlite() -> None:
    """Однократный перенос заказов из файла SQLite (DB_PATH) в пустую таблицу PostgreSQL — одной транзакцией."""
    if not DB_PATH.exists() or _execute(f"SELECT COUNT(*) AS n FROM {TABLE}", fetch="one")["n"] > 0:
        return
    source = sqlite3.connect(DB_PATH)
    source.row_factory = sqlite3.Row
    try:
        if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'").fetchone():
            return
        rows = [dict(row) for row in source.execute("SELECT * FROM orders")]
    finally:
        source.close()
    if not rows:
        return

    columns = [c for c in COLUMNS if c in rows[0]]
    sql = (
        f"INSERT INTO {TABLE} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        f"ON CONFLICT (kwork_id) DO NOTHING"
    )
    conn = _pg_connection()
    with conn.transaction(), conn.cursor() as cursor:
        cursor.executemany(sql, [[row[c] for c in columns] for row in rows])
    logger.info(f"Перенесено заказов из SQLite ({DB_PATH}) в PostgreSQL: {len(rows)}")

def is_order_processed(kwork_id: str) -> bool:
    """Проверяет, обрабатывался ли заказ с данным kwork_id ранее."""
    return _execute(f"SELECT 1 AS found FROM {TABLE} WHERE kwork_id = ?", (str(kwork_id),), fetch="one") is not None

def save_order(
    kwork_id: str,
    title: str,
    description: str,
    budget_info: str,
    desired_price: Optional[int] = None,
    is_feasible: bool = False,
    reasoning: str = "",
    tech_stack: str = "",
    proposal_title: str = "",
    proposal_text: str = "",
    duration_days: int = 2,
    status: str = "PROCESSED",
    error_msg: str = "",
    form_price: Optional[int] = None
) -> None:
    """Сохраняет или обновляет информацию о заказе в БД."""
    now = datetime.now().isoformat()
    _execute(f"""
        INSERT INTO {TABLE} (
            kwork_id, title, description, budget_info, desired_price,
            is_feasible, reasoning, tech_stack, proposal_title, proposal_text,
            duration_days, status, error_msg, created_at, updated_at, form_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kwork_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            budget_info = excluded.budget_info,
            desired_price = excluded.desired_price,
            is_feasible = excluded.is_feasible,
            reasoning = excluded.reasoning,
            tech_stack = excluded.tech_stack,
            proposal_title = excluded.proposal_title,
            proposal_text = excluded.proposal_text,
            duration_days = excluded.duration_days,
            status = excluded.status,
            error_msg = excluded.error_msg,
            updated_at = excluded.updated_at,
            form_price = excluded.form_price
    """, (
        str(kwork_id), title, description, budget_info, desired_price,
        1 if is_feasible else 0, reasoning, tech_stack, proposal_title, proposal_text,
        duration_days, status, error_msg, now, now, form_price
    ))

def update_order_status(kwork_id: str, status: str, error_msg: str = "") -> None:
    """Обновляет статус выполнения заказа."""
    now = datetime.now().isoformat()
    _execute(f"""
        UPDATE {TABLE}
        SET status = ?, error_msg = ?, updated_at = ?
        WHERE kwork_id = ?
    """, (status, error_msg, now, str(kwork_id)))

def update_order_proposal(kwork_id: str, form_price: int, proposal_text: str) -> None:
    """Сохраняет цену и текст отклика, изменённые из Telegram перед отправкой."""
    now = datetime.now().isoformat()
    _execute(f"""
        UPDATE {TABLE}
        SET form_price = ?, proposal_text = ?, updated_at = ?
        WHERE kwork_id = ?
    """, (form_price, proposal_text, now, str(kwork_id)))

def get_order_by_id(kwork_id: str) -> Optional[Dict[str, Any]]:
    """Возвращает информацию о заказе по его kwork_id."""
    row = _execute(f"SELECT * FROM {TABLE} WHERE kwork_id = ?", (str(kwork_id),), fetch="one")
    return dict(row) if row else None

def get_stats() -> Dict[str, int]:
    """Возвращает общую статистику по заказам."""
    rows: List[Dict[str, Any]] = _execute(f"SELECT status, COUNT(*) AS count FROM {TABLE} GROUP BY status", fetch="all")
    return {row["status"]: row["count"] for row in rows}
