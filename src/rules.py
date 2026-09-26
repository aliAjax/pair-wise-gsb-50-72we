"""税务稽查案件与复议流程领域规则与状态转换。"""
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {'investigate': {'inspector'}, 'propose': {'inspector'}, 'review': {'reviewer'}, 'appeal': {'taxpayer_rep'}, 'close': {'reviewer'}}
TRANSITIONS = {'investigate': {'opened': 'investigating'}, 'propose': {'investigating': 'proposed'}, 'review': {'proposed': 'reviewed'}, 'appeal': {'reviewed': 'appealed'}, 'close': {'reviewed': 'closed', 'appealed': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "taxpayer")
        text(p, "tax_period")
        number(p, "declared_tax", 0)
        number(p, "assessed_tax", 0)
        number(p, "penalty_rate", 0, 1)
        integer(p, "evidence_count", 0)
        integer(p, "days_late", 0)
        integer(p, "appeal_deadline_day", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        difference = max(0.0, float(p["assessed_tax"]) - float(p["declared_tax"]))
        interest = difference * 0.0005 * int(p["days_late"])
        penalty = difference * float(p["penalty_rate"])
        p["tax_difference"] = round(difference, 2)
        p["interest"] = round(interest, 2)
        p["penalty"] = round(penalty, 2)
        p["total_due"] = round(difference + interest + penalty, 2)
        p["refund_due"] = round(max(0.0, float(p["declared_tax"]) - float(p["assessed_tax"])), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "propose":
            if int(p["evidence_count"]) <= 0:
                raise ValidationError("没有证据不能提出处理建议")
            changes["proposal"] = text(data, "proposal")
            changes["proposed_amount"] = float(p["total_due"])
            summary = "已提出补税和处罚建议"
        elif action == "review":
            outcome = choice(data, "outcome", ["accepted", "reduced", "remanded"])
            changes["review_outcome"] = outcome
            changes["review_note"] = text(data, "review_note")
            if outcome == "reduced":
                changes["total_due"] = round(float(p["total_due"]) * float(data.get("reduction_pct", 0.5)), 2)
            summary = "复核完成"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["appeal_deadline_day"]):
                raise ValidationError("复议申请超过期限")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "复议申请已受理"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)


INSTALLMENT_MIN_PERIODS = 2
INSTALLMENT_MAX_PERIODS = 6
INSTALLMENT_FIRST_MAX_DAYS = 15
INSTALLMENT_MAX_INTERVAL_DAYS = 30

PLAN_PENDING = "pending"
PLAN_APPROVED = "approved"
PLAN_RETURNED = "returned"
PLAN_COMPLETED = "completed"
PLAN_ACTIVE_STATUSES = (PLAN_PENDING, PLAN_APPROVED)

INSTALLMENT_APPLY_ROLES = {'taxpayer_rep'}
INSTALLMENT_REVIEW_ROLES = {'reviewer'}
INSTALLMENT_PAYMENT_ROLES = {'inspector', 'reviewer'}
CERTIFICATE_ROLES = {'reviewer'}


def _parse_day(value: Any, key: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD格式的日期" % key)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD格式的有效日期" % key) from exc


class InstallmentRules:
    """结案后分期缴纳的申请、复核、实缴、催缴与结案证明规则。"""

    def role_can_apply(self, role: str) -> bool:
        return role == "admin" or role in INSTALLMENT_APPLY_ROLES

    def role_can_review(self, role: str) -> bool:
        return role == "admin" or role in INSTALLMENT_REVIEW_ROLES

    def role_can_pay(self, role: str) -> bool:
        return role == "admin" or role in INSTALLMENT_PAYMENT_ROLES

    def role_can_certify(self, role: str) -> bool:
        return role == "admin" or role in CERTIFICATE_ROLES

    def validate_application(self, data: Dict[str, Any], unpaid_amount: float, today: date) -> Dict[str, Any]:
        reason = text(data, "reason")
        items = data.get("items")
        if not isinstance(items, list):
            raise ValidationError("items必须是分期列表")
        if not INSTALLMENT_MIN_PERIODS <= len(items) <= INSTALLMENT_MAX_PERIODS:
            raise ValidationError("分期期数必须在%s到%s之间" % (INSTALLMENT_MIN_PERIODS, INSTALLMENT_MAX_PERIODS))
        normalized = []
        previous = None
        total = 0.0
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise ValidationError("第%s期必须是对象" % index)
            due = _parse_day(raw.get("date"), "第%s期日期" % index)
            amount = round(number(raw, "amount"), 2)
            if amount <= 0:
                raise ValidationError("第%s期金额必须大于0" % index)
            if previous is None:
                if due < today:
                    raise ValidationError("首期日期不能早于申请日")
                if (due - today).days > INSTALLMENT_FIRST_MAX_DAYS:
                    raise ValidationError("首期日期不能晚于申请日后%s天" % INSTALLMENT_FIRST_MAX_DAYS)
            else:
                gap = (due - previous).days
                if gap <= 0:
                    raise ValidationError("分期日期必须逐期递增")
                if gap > INSTALLMENT_MAX_INTERVAL_DAYS:
                    raise ValidationError("相邻分期间隔不能超过%s天" % INSTALLMENT_MAX_INTERVAL_DAYS)
            normalized.append({"seq": index, "due_date": due.isoformat(), "amount": amount})
            total = round(total + amount, 2)
            previous = due
        if total != round(unpaid_amount, 2):
            raise ValidationError("分期总额%s必须等于未缴金额%s" % (total, round(unpaid_amount, 2)))
        return {"reason": reason, "items": normalized, "total_amount": total}

    def check_application_conflicts(self, plans: List[Dict[str, Any]], today: date) -> None:
        for plan in plans:
            if plan["status"] == PLAN_APPROVED and self.plan_has_overdue(plan, today):
                raise Conflict("存在逾期未结的分期计划，需先结清逾期款项")
        for plan in plans:
            if plan["status"] == PLAN_PENDING:
                raise Conflict("已有待复核的分期申请")
            if plan["status"] == PLAN_APPROVED:
                raise Conflict("已有正在执行的分期计划")

    def plan_has_overdue(self, plan: Dict[str, Any], today: date) -> bool:
        for item in plan["items"]:
            if self._remaining(item) > 0 and _parse_day(item["due_date"], "due_date") < today:
                return True
        return False

    @staticmethod
    def _remaining(item: Dict[str, Any]) -> float:
        return round(float(item["amount"]) - float(item.get("paid_amount", 0.0)), 2)

    def validate_review(self, data: Dict[str, Any]) -> Tuple[str, str]:
        outcome = choice(data, "outcome", [PLAN_APPROVED, PLAN_RETURNED])
        if outcome == PLAN_RETURNED:
            note = text(data, "note")
        else:
            note = optional_text(data, "note")
        return outcome, note

    def validate_payment(self, data: Dict[str, Any], plan: Dict[str, Any], today: date) -> Dict[str, Any]:
        if plan["status"] == PLAN_COMPLETED:
            raise Conflict("分期计划已缴清")
        if plan["status"] != PLAN_APPROVED:
            raise Conflict("分期计划未批准，不能登记实缴")
        seq = integer(data, "seq", 1, len(plan["items"]))
        amount = round(number(data, "amount"), 2)
        if amount <= 0:
            raise ValidationError("实缴金额必须大于0")
        paid_on_text = optional_text(data, "paid_on")
        paid_on = _parse_day(paid_on_text, "paid_on") if paid_on_text else today
        item = None
        for candidate in plan["items"]:
            if int(candidate["seq"]) == seq:
                item = candidate
                break
        remaining = self._remaining(item)
        if remaining <= 0:
            raise Conflict("第%s期已缴清" % seq)
        if amount > remaining:
            raise ValidationError("实缴金额超过第%s期剩余应缴%s" % (seq, remaining))
        return {"installment_id": item["id"], "seq": seq, "amount": amount, "paid_on": paid_on.isoformat()}

    def decorate_plan(self, plan: Dict[str, Any], today: date) -> Dict[str, Any]:
        items = []
        dunning = []
        paid_total = 0.0
        for item in plan["items"]:
            remaining = self._remaining(item)
            paid_total = round(paid_total + float(item.get("paid_amount", 0.0)), 2)
            due = _parse_day(item["due_date"], "due_date")
            if remaining <= 0:
                status = "paid"
                days_overdue = 0
            elif due < today:
                status = "overdue"
                days_overdue = (today - due).days
            else:
                status = "pending"
                days_overdue = 0
            entry = dict(item)
            entry["remaining"] = remaining
            entry["status"] = status
            entry["days_overdue"] = days_overdue
            items.append(entry)
            if status == "overdue":
                dunning.append({
                    "record_id": plan["record_id"],
                    "plan_id": plan["id"],
                    "seq": item["seq"],
                    "due_date": item["due_date"],
                    "amount": item["amount"],
                    "paid_amount": item.get("paid_amount", 0.0),
                    "remaining": remaining,
                    "days_overdue": days_overdue,
                })
        decorated = dict(plan)
        decorated["items"] = items
        decorated["paid_total"] = paid_total
        decorated["unpaid_total"] = round(float(plan["total_amount"]) - paid_total, 2)
        decorated["dunning"] = dunning
        return decorated

    def ensure_certifiable(self, record: Dict[str, Any], unpaid_amount: float) -> None:
        if record["state"] != "closed":
            raise Conflict("案件未结案，不能开具结案证明")
        if record["payload"].get("closure_certificate"):
            raise Conflict("结案证明已开具")
        if round(unpaid_amount, 2) > 0:
            raise Conflict("未缴清税款，不能开具结案证明")
