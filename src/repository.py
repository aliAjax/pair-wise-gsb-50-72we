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
                    reason TEXT NOT NULL,
                    total_amount REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT
                );
                CREATE TABLE IF NOT EXISTS installments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    amount REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installment_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
                    installment_id INTEGER NOT NULL REFERENCES installments(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    paid_on TEXT NOT NULL,
                    recorded_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_plans_record ON installment_plans(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_installments_plan ON installments(plan_id, seq);
                CREATE INDEX IF NOT EXISTS idx_payments_plan ON installment_payments(plan_id, id);
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

    def create_installment_plan(self, record_id: int, status: str, reason: str, total_amount: float, items: List[Dict[str, Any]], actor_id: str, active_statuses: List[str]) -> Dict[str, Any]:
        now = _now()
        placeholders = ",".join("?" for _ in active_statuses)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM installment_plans WHERE record_id=? AND status IN (%s)" % placeholders,
                (record_id, *active_statuses),
            ).fetchone()
            if int(row["total"]) > 0:
                connection.rollback()
                raise Conflict("已有进行中的分期计划")
            cursor = connection.execute(
                "INSERT INTO installment_plans(record_id,status,reason,total_amount,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, status, reason, total_amount, actor_id, now),
            )
            plan_id = int(cursor.lastrowid)
            for item in items:
                connection.execute(
                    "INSERT INTO installments(plan_id,seq,due_date,amount) VALUES(?,?,?,?)",
                    (plan_id, int(item["seq"]), item["due_date"], float(item["amount"])),
                )
            connection.commit()
        return self.get_installment_plan(plan_id)

    def _plan_with_items(self, connection: sqlite3.Connection, plan_id: int, row: sqlite3.Row) -> Dict[str, Any]:
        plan = dict(row)
        plan["total_amount"] = round(float(plan["total_amount"]), 2)
        item_rows = connection.execute(
            """
            SELECT i.id, i.plan_id, i.seq, i.due_date, i.amount,
                   COALESCE((SELECT SUM(p.amount) FROM installment_payments p WHERE p.installment_id=i.id), 0) AS paid_amount
            FROM installments i WHERE i.plan_id=? ORDER BY i.seq
            """,
            (plan_id,),
        ).fetchall()
        items = []
        for item_row in item_rows:
            item = dict(item_row)
            item["amount"] = round(float(item["amount"]), 2)
            item["paid_amount"] = round(float(item["paid_amount"]), 2)
            items.append(item)
        plan["items"] = items
        payment_rows = connection.execute(
            """
            SELECT p.id, p.plan_id, p.installment_id, i.seq, p.amount, p.paid_on, p.recorded_by, p.created_at
            FROM installment_payments p JOIN installments i ON i.id=p.installment_id
            WHERE p.plan_id=? ORDER BY p.id
            """,
            (plan_id,),
        ).fetchall()
        payments = []
        for payment_row in payment_rows:
            payment = dict(payment_row)
            payment["amount"] = round(float(payment["amount"]), 2)
            payments.append(payment)
        plan["payments"] = payments
        return plan

    def get_installment_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM installment_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFound("分期计划不存在")
            return self._plan_with_items(connection, plan_id, row)

    def list_installment_plans(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM installment_plans WHERE record_id=? ORDER BY id DESC", (record_id,)).fetchall()
            return [self._plan_with_items(connection, int(row["id"]), row) for row in rows]

    def list_installment_plans_by_status(self, status: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.*, r.reference, r.payload AS record_payload FROM installment_plans p JOIN records r ON r.id=p.record_id WHERE p.status=? ORDER BY p.id",
                (status,),
            ).fetchall()
            plans = []
            for row in rows:
                plan = self._plan_with_items(connection, int(row["id"]), row)
                payload = json.loads(plan.pop("record_payload"))
                plan["taxpayer"] = payload.get("taxpayer", "")
                plans.append(plan)
            return plans

    def review_installment_plan(self, plan_id: int, from_status: str, to_status: str, note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE installment_plans SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND status=?",
                (to_status, actor_id, now, note, plan_id, from_status),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                row = connection.execute("SELECT id FROM installment_plans WHERE id=?", (plan_id,)).fetchone()
                if row is None:
                    raise NotFound("分期计划不存在")
                raise Conflict("分期计划已复核，请刷新后重试")
            connection.commit()
        return self.get_installment_plan(plan_id)

    def record_installment_payment(self, plan_id: int, installment_id: int, amount: float, paid_on: str, actor_id: str, active_status: str, completed_status: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan_row = connection.execute("SELECT status FROM installment_plans WHERE id=?", (plan_id,)).fetchone()
            if plan_row is None:
                connection.rollback()
                raise NotFound("分期计划不存在")
            if plan_row["status"] != active_status:
                connection.rollback()
                raise Conflict("分期计划未在缴纳执行中")
            item_row = connection.execute(
                "SELECT i.amount, COALESCE((SELECT SUM(p.amount) FROM installment_payments p WHERE p.installment_id=i.id), 0) AS paid FROM installments i WHERE i.id=? AND i.plan_id=?",
                (installment_id, plan_id),
            ).fetchone()
            if item_row is None:
                connection.rollback()
                raise NotFound("分期明细不存在")
            remaining = round(float(item_row["amount"]) - float(item_row["paid"]), 2)
            if amount > remaining:
                connection.rollback()
                raise Conflict("该期剩余应缴不足，请刷新后重试")
            connection.execute(
                "INSERT INTO installment_payments(plan_id,installment_id,amount,paid_on,recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                (plan_id, installment_id, amount, paid_on, actor_id, now),
            )
            unpaid_row = connection.execute(
                "SELECT COUNT(*) AS total FROM installments i WHERE i.plan_id=? AND ROUND(i.amount - COALESCE((SELECT SUM(p.amount) FROM installment_payments p WHERE p.installment_id=i.id), 0), 2) > 0",
                (plan_id,),
            ).fetchone()
            if int(unpaid_row["total"]) == 0:
                connection.execute("UPDATE installment_plans SET status=? WHERE id=?", (completed_status, plan_id))
            connection.commit()
        return self.get_installment_plan(plan_id)

    def total_paid(self, record_id: int) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount), 0) AS paid FROM installment_payments WHERE plan_id IN (SELECT id FROM installment_plans WHERE record_id=?)",
                (record_id,),
            ).fetchone()
        return round(float(row["paid"]), 2)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
