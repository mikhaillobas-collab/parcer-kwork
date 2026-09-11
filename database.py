import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List
from config import DB_PATH

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Инициализация таблиц базы данных SQLite."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
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
                updated_at TEXT
            )
        """)
        conn.commit()

def is_order_processed(kwork_id: str) -> bool:
    """Проверяет, обрабатывался ли заказ с данным kwork_id ранее."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM orders WHERE kwork_id = ?", (str(kwork_id),))
        return cursor.fetchone() is not None

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
    error_msg: str = ""
) -> None:
    """Сохраняет или обновляет информацию о заказе в БД."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO orders (
                kwork_id, title, description, budget_info, desired_price,
                is_feasible, reasoning, tech_stack, proposal_title, proposal_text,
                duration_days, status, error_msg, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                updated_at = excluded.updated_at
        """, (
            str(kwork_id), title, description, budget_info, desired_price,
            1 if is_feasible else 0, reasoning, tech_stack, proposal_title, proposal_text,
            duration_days, status, error_msg, now, now
        ))
        conn.commit()

def update_order_status(kwork_id: str, status: str, error_msg: str = "") -> None:
    """Обновляет статус выполнения заказа."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE orders
            SET status = ?, error_msg = ?, updated_at = ?
            WHERE kwork_id = ?
        """, (status, error_msg, now, str(kwork_id)))
        conn.commit()

def get_order_by_id(kwork_id: str) -> Optional[Dict[str, Any]]:
    """Возвращает информацию о заказе по его kwork_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE kwork_id = ?", (str(kwork_id),))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_stats() -> Dict[str, int]:
    """Возвращает общую статистику по заказам."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as count FROM orders GROUP BY status")
        rows = cursor.fetchall()
        return {row["status"]: row["count"] for row in rows}

