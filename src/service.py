"""业务用例编排、权限检查与审计。"""
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules, InstallmentRules, PLAN_ACTIVE_STATUSES, PLAN_APPROVED, PLAN_COMPLETED, PLAN_PENDING


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, installment_rules: InstallmentRules = None, clock: Callable[[], date] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.installment_rules = installment_rules or InstallmentRules()
        self.clock = clock or (lambda: datetime.now(timezone.utc).date())

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

    def _unpaid_amount(self, record: Dict[str, Any]) -> float:
        total_due = float(record["payload"].get("total_due", 0.0))
        return round(total_due - self.repository.total_paid(record["id"]), 2)

    def _get_plan_for_record(self, record_id: int, plan_id: int) -> Dict[str, Any]:
        self.repository.get(record_id)
        plan = self.repository.get_installment_plan(plan_id)
        if int(plan["record_id"]) != int(record_id):
            raise NotFound("分期计划不存在")
        return plan

    def apply_installment_plan(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installment_rules.role_can_apply(actor.role):
            raise PermissionDenied("角色无权申请分期缴纳")
        record = self.repository.get(record_id)
        if record["state"] != "closed":
            raise Conflict("案件结案后才能申请分期缴纳")
        unpaid = self._unpaid_amount(record)
        if unpaid <= 0:
            raise Conflict("案件没有未缴金额，无需分期")
        today = self.clock()
        plans = self.repository.list_installment_plans(record_id)
        self.installment_rules.check_application_conflicts(plans, today)
        prepared = self.installment_rules.validate_application(data or {}, unpaid, today)
        plan = self.repository.create_installment_plan(record_id, PLAN_PENDING, prepared["reason"], prepared["total_amount"], prepared["items"], actor.user_id, list(PLAN_ACTIVE_STATUSES))
        self.audit.note(record_id, actor.user_id, "installment_apply", {"plan_id": plan["id"], "periods": len(prepared["items"]), "total_amount": prepared["total_amount"], "reason": prepared["reason"]})
        return self.installment_rules.decorate_plan(plan, today)

    def review_installment_plan(self, actor: Actor, record_id: int, plan_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installment_rules.role_can_review(actor.role):
            raise PermissionDenied("角色无权复核分期计划")
        self._get_plan_for_record(record_id, plan_id)
        outcome, note = self.installment_rules.validate_review(data or {})
        plan = self.repository.review_installment_plan(plan_id, PLAN_PENDING, outcome, note, actor.user_id)
        self.audit.note(record_id, actor.user_id, "installment_review", {"plan_id": plan_id, "outcome": outcome, "note": note})
        return self.installment_rules.decorate_plan(plan, self.clock())

    def record_installment_payment(self, actor: Actor, record_id: int, plan_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installment_rules.role_can_pay(actor.role):
            raise PermissionDenied("角色无权登记实缴")
        plan = self._get_plan_for_record(record_id, plan_id)
        today = self.clock()
        payment = self.installment_rules.validate_payment(data or {}, plan, today)
        plan = self.repository.record_installment_payment(plan_id, payment["installment_id"], payment["amount"], payment["paid_on"], actor.user_id, PLAN_APPROVED, PLAN_COMPLETED)
        self.audit.note(record_id, actor.user_id, "installment_payment", {"plan_id": plan_id, "seq": payment["seq"], "amount": payment["amount"], "paid_on": payment["paid_on"]})
        return self.installment_rules.decorate_plan(plan, today)

    def list_installment_plans(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        today = self.clock()
        plans = [self.installment_rules.decorate_plan(plan, today) for plan in self.repository.list_installment_plans(record_id)]
        return {"record_id": record_id, "total_due": round(float(record["payload"].get("total_due", 0.0)), 2), "unpaid_amount": self._unpaid_amount(record), "items": plans}

    def list_dunning(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        today = self.clock()
        entries = []
        for plan in self.repository.list_installment_plans_by_status(PLAN_APPROVED):
            decorated = self.installment_rules.decorate_plan(plan, today)
            for entry in decorated["dunning"]:
                entry["reference"] = plan.get("reference", "")
                entry["taxpayer"] = plan.get("taxpayer", "")
                entries.append(entry)
        entries.sort(key=lambda item: (-item["days_overdue"], item["record_id"], item["seq"]))
        return {"items": entries}

    def issue_closure_certificate(self, actor: Actor, record_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.installment_rules.role_can_certify(actor.role):
            raise PermissionDenied("角色无权开具结案证明")
        record = self.repository.get(record_id)
        unpaid = self._unpaid_amount(record)
        self.installment_rules.ensure_certifiable(record, unpaid)
        payload = dict(record["payload"])
        certificate_no = "JZ-%06d" % int(record_id)
        payload["closure_certificate"] = {"certificate_no": certificate_no, "issued_by": actor.user_id, "issued_at": datetime.now(timezone.utc).isoformat()}
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=record["state"],
            payload=payload,
            actor_id=actor.user_id,
            action="issue_certificate",
            details={"summary": "结案证明已开具", "certificate_no": certificate_no},
        )
