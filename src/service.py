"""业务用例编排、权限检查与审计。"""
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .installments import InstallmentRules
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, installments: InstallmentRules = None, today: Callable[[], date] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.installments = installments or InstallmentRules()
        self._today = today or date.today

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def apply_installments(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installments.role_can_apply(actor.role):
            raise PermissionDenied("角色无权申请分期缴纳")
        record = self.repository.get(record_id)
        today = self._today()
        paid_total = self.repository.paid_total(record_id)
        latest = self.repository.latest_plan(record_id)
        self.installments.check_can_apply(record, latest, paid_total, today)
        schedule = self.installments.validate_schedule(data or {}, self.installments.unpaid_amount(record, paid_total), today)
        plan = self.repository.create_plan(record_id, schedule, actor.user_id)
        self.audit.note(record_id, actor.user_id, "installments_applied", {"plan_id": plan["id"], "installments": schedule})
        return plan

    def review_installments(self, actor: Actor, record_id: int, plan_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installments.role_can_review(actor.role):
            raise PermissionDenied("角色无权复核分期申请")
        plan = self.repository.get_plan(record_id, plan_id)
        status, note = self.installments.validate_review(plan, data or {})
        updated = self.repository.update_plan_status(plan_id, status, actor.user_id, note)
        self.audit.note(record_id, actor.user_id, "installments_" + status, {"plan_id": plan_id, "note": note})
        return updated

    def register_payment(self, actor: Actor, record_id: int, plan_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installments.role_can_pay(actor.role):
            raise PermissionDenied("角色无权登记实缴")
        plan = self.repository.get_plan(record_id, plan_id)
        payment = self.installments.validate_payment(plan, data or {}, self._today())
        saved = self.repository.add_payment(plan_id, payment["seq"], payment["amount"], payment["paid_date"], actor.user_id)
        self.audit.note(record_id, actor.user_id, "payment_registered", {"plan_id": plan_id, "seq": payment["seq"], "amount": payment["amount"], "paid_date": payment["paid_date"]})
        return saved

    def installment_overview(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        today = self._today()
        paid_total = self.repository.paid_total(record_id)
        plan = self.repository.latest_plan(record_id)
        overview = {
            "record_id": record["id"],
            "state": record["state"],
            "total_due": round(float(record["payload"].get("total_due", 0.0)), 2),
            "paid_total": paid_total,
            "unpaid": self.installments.unpaid_amount(record, paid_total),
            "plan": None,
        }
        if plan is not None:
            overview["plan"] = {
                "id": plan["id"],
                "status": plan["status"],
                "applied_by": plan["applied_by"],
                "applied_at": plan["applied_at"],
                "decided_by": plan["decided_by"],
                "decided_at": plan["decided_at"],
                "decision_note": plan["decision_note"],
                "installments": self.installments.schedule_status(plan["installments"], plan["payments"], today),
                "payments": plan["payments"],
                "reminders": self.installments.reminders(plan["installments"], plan["payments"], today),
            }
        return overview

    def closure_certificate(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        paid_total = self.repository.paid_total(record_id)
        self.installments.ensure_certificate_allowed(record, paid_total)
        return {
            "certificate_no": "JSZM-%06d" % record["id"],
            "record_id": record["id"],
            "reference": record["reference"],
            "taxpayer": record["payload"].get("taxpayer", ""),
            "total_due": round(float(record["payload"].get("total_due", 0.0)), 2),
            "paid_total": paid_total,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
