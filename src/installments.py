"""结案后分期缴纳计划规则：申请校验、复核决定、实缴登记与催缴计算。"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .domain import Conflict, ValidationError, integer, optional_text, text


MIN_INSTALLMENTS = 2
MAX_INSTALLMENTS = 6
FIRST_INSTALLMENT_MAX_DAYS = 15
MAX_INTERVAL_DAYS = 30

PLAN_PENDING = "pending"
PLAN_APPROVED = "approved"
PLAN_RETURNED = "returned"

APPLY_ROLES = {"taxpayer_rep"}
REVIEW_ROLES = {"reviewer"}
PAYMENT_ROLES = {"reviewer"}


def parse_day(value: Any, key: str = "due_date") -> date:
    if not isinstance(value, str):
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc


def _money(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s必须是数字" % key)
    amount = round(float(value), 2)
    if amount <= 0:
        raise ValidationError("%s必须大于0" % key)
    return amount


class InstallmentRules:
    """分期缴纳的纯规则，不依赖存储与HTTP层。"""

    def role_can_apply(self, role: str) -> bool:
        return role == "admin" or role in APPLY_ROLES

    def role_can_review(self, role: str) -> bool:
        return role == "admin" or role in REVIEW_ROLES

    def role_can_pay(self, role: str) -> bool:
        return role == "admin" or role in PAYMENT_ROLES

    def unpaid_amount(self, record: Dict[str, Any], paid_total: float) -> float:
        return round(float(record["payload"].get("total_due", 0.0)) - paid_total, 2)

    def validate_schedule(self, data: Dict[str, Any], unpaid: float, today: date) -> List[Dict[str, Any]]:
        if unpaid <= 0:
            raise ValidationError("没有未缴金额，无需分期")
        items = data.get("installments")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValidationError("installments必须是对象列表")
        if not MIN_INSTALLMENTS <= len(items) <= MAX_INSTALLMENTS:
            raise ValidationError("分期期数必须在%s到%s期之间" % (MIN_INSTALLMENTS, MAX_INSTALLMENTS))
        schedule: List[Dict[str, Any]] = []
        previous: Optional[date] = None
        total = 0.0
        for index, item in enumerate(items, start=1):
            due = parse_day(item.get("due_date"), "installments[%d].due_date" % index)
            amount = _money(item.get("amount"), "installments[%d].amount" % index)
            if previous is None:
                if due < today:
                    raise ValidationError("首期缴款日不能早于申请日")
                if (due - today).days > FIRST_INSTALLMENT_MAX_DAYS:
                    raise ValidationError("首期缴款日不能晚于申请日后%s天" % FIRST_INSTALLMENT_MAX_DAYS)
            else:
                gap = (due - previous).days
                if gap <= 0:
                    raise ValidationError("缴款日期必须逐期递增")
                if gap > MAX_INTERVAL_DAYS:
                    raise ValidationError("相邻两期间隔不能超过%s天" % MAX_INTERVAL_DAYS)
            previous = due
            total = round(total + amount, 2)
            schedule.append({"seq": index, "due_date": due.isoformat(), "amount": amount})
        if total != round(unpaid, 2):
            raise ValidationError("分期总额%s必须等于未缴金额%s" % (total, round(unpaid, 2)))
        return schedule

    def check_can_apply(self, record: Dict[str, Any], latest_plan: Optional[Dict[str, Any]], paid_total: float, today: date) -> None:
        if record["state"] != "closed":
            raise Conflict("案件结案后才能申请分期缴纳")
        if latest_plan is None:
            return
        if latest_plan["status"] == PLAN_PENDING:
            raise Conflict("已有待复核的分期申请")
        if latest_plan["status"] == PLAN_APPROVED and self.unpaid_amount(record, paid_total) > 0:
            if self.reminders(latest_plan["installments"], latest_plan["payments"], today):
                raise Conflict("存在逾期未结的分期计划，请先结清催缴后再申请")
            raise Conflict("已有正在执行的分期计划")

    def validate_review(self, plan: Dict[str, Any], data: Dict[str, Any]) -> Any:
        if plan["status"] != PLAN_PENDING:
            raise Conflict("该分期申请已复核")
        decision = text(data, "decision")
        if decision not in {"approve", "return"}:
            raise ValidationError("decision只能是approve/return")
        note = optional_text(data, "note")
        if decision == "return" and not note:
            raise ValidationError("退回必须说明原因")
        return (PLAN_APPROVED if decision == "approve" else PLAN_RETURNED), note

    def paid_by_seq(self, payments: List[Dict[str, Any]]) -> Dict[int, float]:
        totals: Dict[int, float] = {}
        for payment in payments:
            seq = int(payment["seq"])
            totals[seq] = round(totals.get(seq, 0.0) + float(payment["amount"]), 2)
        return totals

    def schedule_status(self, schedule: List[Dict[str, Any]], payments: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
        totals = self.paid_by_seq(payments)
        result = []
        for item in schedule:
            paid = totals.get(int(item["seq"]), 0.0)
            remaining = max(0.0, round(float(item["amount"]) - paid, 2))
            if remaining <= 0:
                status = "paid"
            elif parse_day(item["due_date"]) < today:
                status = "overdue"
            else:
                status = "pending"
            result.append({"seq": int(item["seq"]), "due_date": item["due_date"], "amount": float(item["amount"]), "paid": paid, "remaining": remaining, "status": status})
        return result

    def reminders(self, schedule: List[Dict[str, Any]], payments: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
        notes = []
        for item in self.schedule_status(schedule, payments, today):
            if item["status"] == "overdue":
                days = (today - parse_day(item["due_date"])).days
                notes.append({
                    "seq": item["seq"],
                    "due_date": item["due_date"],
                    "amount_due": item["remaining"],
                    "days_overdue": days,
                    "message": "第%d期已逾期%d天，未缴%s元，请催缴" % (item["seq"], days, item["remaining"]),
                })
        return notes

    def validate_payment(self, plan: Dict[str, Any], data: Dict[str, Any], today: date) -> Dict[str, Any]:
        if plan["status"] != PLAN_APPROVED:
            raise Conflict("分期计划未批准，不能登记实缴")
        seq = integer(data, "seq", 1)
        amount = _money(data.get("amount"), "amount")
        raw_date = data.get("paid_date")
        paid_date = parse_day(raw_date, "paid_date") if raw_date else today
        for item in self.schedule_status(plan["installments"], plan["payments"], today):
            if item["seq"] == seq:
                if item["remaining"] <= 0:
                    raise Conflict("第%d期已缴清" % seq)
                if amount > item["remaining"]:
                    raise ValidationError("实缴金额超过第%d期剩余应缴%s元" % (seq, item["remaining"]))
                return {"seq": seq, "amount": amount, "paid_date": paid_date.isoformat()}
        raise ValidationError("第%d期不存在" % seq)

    def ensure_certificate_allowed(self, record: Dict[str, Any], paid_total: float) -> None:
        if record["state"] != "closed":
            raise Conflict("案件未结案，不能开具结案证明")
        if self.unpaid_amount(record, paid_total) > 0:
            raise Conflict("税款未缴清，不能开具结案证明")
