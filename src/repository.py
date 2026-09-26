"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installment_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    total_amount REAL NOT NULL,
                    installments TEXT NOT NULL,
                    applied_by TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    decided_by TEXT NOT NULL DEFAULT '',
                    decided_at TEXT NOT NULL DEFAULT '',
                    decision_note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS installment_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    paid_date TEXT NOT NULL,
                    registered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_plans_record ON installment_plans(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_payments_plan ON installment_payments(plan_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_pending ON installment_plans(record_id) WHERE status='pending';
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    @staticmethod
    def _plan_row(row: sqlite3.Row, payments: List[sqlite3.Row]) -> Dict[str, Any]:
        item = dict(row)
        item["installments"] = json.loads(item["installments"])
        item["payments"] = [dict(payment) for payment in payments]
        return item

    def create_plan(self, record_id: int, schedule: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        total = round(sum(float(item["amount"]) for item in schedule), 2)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO installment_plans(record_id,status,total_amount,installments,applied_by,applied_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "pending", total, json.dumps(schedule, ensure_ascii=False, sort_keys=True), actor_id, now),
                )
                plan_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM installment_plans WHERE id=?", (plan_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("已有待复核的分期申请") from exc
        return self._plan_row(row, [])

    def get_plan(self, record_id: int, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM installment_plans WHERE id=? AND record_id=?", (plan_id, record_id)).fetchone()
            if row is None:
                raise NotFound("分期计划不存在")
            payments = connection.execute("SELECT * FROM installment_payments WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        return self._plan_row(row, payments)

    def latest_plan(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM installment_plans WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()
            if row is None:
                return None
            payments = connection.execute("SELECT * FROM installment_payments WHERE plan_id=? ORDER BY id", (row["id"],)).fetchall()
        return self._plan_row(row, payments)

    def update_plan_status(self, plan_id: int, status: str, actor_id: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE installment_plans SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?",
                (status, actor_id, now, note, plan_id),
            )
            if cursor.rowcount == 0:
                raise NotFound("分期计划不存在")
            row = connection.execute("SELECT * FROM installment_plans WHERE id=?", (plan_id,)).fetchone()
            payments = connection.execute("SELECT * FROM installment_payments WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        return self._plan_row(row, payments)

    def add_payment(self, plan_id: int, seq: int, amount: float, paid_date: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO installment_payments(plan_id,seq,amount,paid_date,registered_by,created_at) VALUES(?,?,?,?,?,?)",
                (plan_id, seq, amount, paid_date, actor_id, now),
            )
            row = connection.execute("SELECT * FROM installment_payments WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return dict(row)

    def paid_total(self, record_id: int) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(p.amount),0) AS total FROM installment_payments p JOIN installment_plans pl ON pl.id=p.plan_id WHERE pl.record_id=?",
                (record_id,),
            ).fetchone()
        return round(float(row["total"]), 2)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
