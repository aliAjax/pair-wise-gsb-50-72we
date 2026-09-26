import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
FLOW = [('investigate', 'inspector', {'plan': '核对账簿'}, 'investigating'), ('propose', 'inspector', {'proposal': '补税并处罚'}, 'proposed'), ('review', 'reviewer', {'outcome': 'accepted', 'review_note': '证据充分'}, 'reviewed'), ('close', 'reviewer', {'final_decision': '维持处理'}, 'closed')]
TOTAL_DUE = 323700.0
REP = Actor("rep-1", "taxpayer_rep")
REVIEWER = Actor("rev-1", "reviewer")
STAFF = Actor("staff-1", "inspector")


class InstallmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.today = date(2026, 9, 26)
        self.service = build_service(self.db_path)
        self.service.clock = lambda: self.today
        self.record = self._closed_record(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def _closed_record(self, service, reference="TAX-26001"):
        record = service.create(Actor("creator", "inspector"), reference, CREATE_DATA)
        for action, role, data, _ in FLOW:
            record = service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def _items(self, count, start=5, step=10):
        base = round(TOTAL_DUE / count, 2)
        items = []
        total = 0.0
        for index in range(count):
            amount = base if index < count - 1 else round(TOTAL_DUE - total, 2)
            total = round(total + amount, 2)
            items.append({"date": (self.today + timedelta(days=start + step * index)).isoformat(), "amount": amount})
        return items

    def _apply(self, items=None, reason="资金周转困难"):
        return self.service.apply_installment_plan(REP, self.record["id"], {"reason": reason, "items": items if items is not None else self._items(3)})

    def _approved_plan(self, items=None):
        plan = self._apply(items)
        return self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "approved", "note": "同意分期"})

    def test_full_flow_to_certificate(self):
        plan = self._approved_plan()
        self.assertEqual(plan["status"], "approved")
        self.assertEqual(len(plan["items"]), 3)
        for seq in (1, 2, 3):
            plan = self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": seq, "amount": 107900.0, "paid_on": self.today.isoformat()})
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["unpaid_total"], 0.0)
        self.assertEqual(len(plan["payments"]), 3)
        view = self.service.list_installment_plans(REP, self.record["id"])
        self.assertEqual(view["unpaid_amount"], 0.0)
        record = self.service.issue_closure_certificate(REVIEWER, self.record["id"], self.record["version"])
        self.assertIn("closure_certificate", record["payload"])
        timeline = self.service.timeline(REP, self.record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("installment_apply", actions)
        self.assertIn("installment_review", actions)
        self.assertEqual(actions.count("installment_payment"), 3)
        self.assertIn("issue_certificate", actions)
        with self.assertRaises(Conflict):
            self._apply()
        with self.assertRaises(Conflict):
            self.service.issue_closure_certificate(REVIEWER, self.record["id"], record["version"])

    def test_application_validation(self):
        with self.assertRaises(ValidationError):
            self._apply(items=self._items(1))
        with self.assertRaises(ValidationError):
            self._apply(items=self._items(7))
        with self.assertRaises(ValidationError):
            self._apply(items=self._items(2, start=16))
        with self.assertRaises(ValidationError):
            self._apply(items=self._items(2, start=-1))
        items = [{"date": (self.today + timedelta(days=5)).isoformat(), "amount": 100000.0}, {"date": (self.today + timedelta(days=36)).isoformat(), "amount": 223700.0}]
        with self.assertRaises(ValidationError):
            self._apply(items=items)
        items = [{"date": (self.today + timedelta(days=5)).isoformat(), "amount": 107900.0}, {"date": (self.today + timedelta(days=5)).isoformat(), "amount": 215800.0}]
        with self.assertRaises(ValidationError):
            self._apply(items=items)
        items = self._items(3)
        items[0]["amount"] = 1.0
        with self.assertRaises(ValidationError):
            self._apply(items=items)
        with self.assertRaises(ValidationError):
            self._apply(reason="")
        items = self._items(2)
        items[0]["amount"] = 0
        items[1]["amount"] = TOTAL_DUE
        with self.assertRaises(ValidationError):
            self._apply(items=items)
        items = self._items(2)
        items[0]["date"] = "2026-13-01"
        with self.assertRaises(ValidationError):
            self._apply(items=items)

    def test_apply_requires_closed_case(self):
        record = self.service.create(Actor("creator", "inspector"), "TAX-26002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.apply_installment_plan(REP, record["id"], {"reason": "困难", "items": self._items(2)})

    def test_active_plan_blocks_new_application(self):
        self._apply()
        with self.assertRaises(Conflict):
            self._apply()
        plan = self.service.list_installment_plans(REP, self.record["id"])["items"][0]
        self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "approved"})
        with self.assertRaises(Conflict):
            self._apply()

    def test_returned_plan_allows_reapply(self):
        plan = self._apply()
        with self.assertRaises(ValidationError):
            self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "returned"})
        returned = self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "returned", "note": "期数过少"})
        self.assertEqual(returned["status"], "returned")
        again = self._apply()
        self.assertEqual(again["status"], "pending")
        with self.assertRaises(Conflict):
            self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "approved"})

    def test_overdue_blocks_application_and_shows_dunning(self):
        plan = self._approved_plan()
        self.today = self.today + timedelta(days=6)
        with self.assertRaises(Conflict) as ctx:
            self._apply()
        self.assertIn("逾期未结", str(ctx.exception))
        with self.assertRaises(Conflict) as ctx:
            self.service.apply_installment_plan(REP, self.record["id"], {"reason": "困难", "items": [{"date": "bad", "amount": 1}]})
        self.assertIn("逾期未结", str(ctx.exception))
        view = self.service.list_installment_plans(REP, self.record["id"])
        first = view["items"][0]["items"][0]
        self.assertEqual(first["status"], "overdue")
        self.assertEqual(first["days_overdue"], 1)
        self.assertEqual(len(view["items"][0]["dunning"]), 1)
        dunning = self.service.list_dunning(REVIEWER)["items"]
        self.assertEqual(len(dunning), 1)
        self.assertEqual(dunning[0]["seq"], 1)
        self.assertEqual(dunning[0]["taxpayer"], "Star Ltd")
        plan = self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 107900.0})
        self.assertEqual(plan["dunning"], [])
        self.assertEqual(self.service.list_dunning(REVIEWER)["items"], [])

    def test_payment_validation(self):
        plan = self._apply()
        with self.assertRaises(Conflict):
            self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 100.0})
        plan = self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "approved"})
        with self.assertRaises(ValidationError):
            self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 4, "amount": 100.0})
        with self.assertRaises(ValidationError):
            self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 0})
        with self.assertRaises(ValidationError):
            self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 107900.01})
        plan = self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 107900.0})
        with self.assertRaises(Conflict):
            self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 1.0})

    def test_certificate_requires_full_payment(self):
        with self.assertRaises(Conflict):
            self.service.issue_closure_certificate(REVIEWER, self.record["id"], self.record["version"])
        plan = self._approved_plan()
        self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 107900.0})
        with self.assertRaises(Conflict):
            self.service.issue_closure_certificate(REVIEWER, self.record["id"], self.record["version"])

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.apply_installment_plan(STAFF, self.record["id"], {"reason": "困难", "items": self._items(2)})
        plan = self._apply()
        with self.assertRaises(PermissionDenied):
            self.service.review_installment_plan(REP, self.record["id"], plan["id"], {"outcome": "approved"})
        self.service.review_installment_plan(REVIEWER, self.record["id"], plan["id"], {"outcome": "approved"})
        with self.assertRaises(PermissionDenied):
            self.service.record_installment_payment(REP, self.record["id"], plan["id"], {"seq": 1, "amount": 1.0})
        with self.assertRaises(PermissionDenied):
            self.service.issue_closure_certificate(STAFF, self.record["id"], self.record["version"])

    def test_persistence_across_restart(self):
        plan = self._approved_plan()
        self.service.record_installment_payment(STAFF, self.record["id"], plan["id"], {"seq": 1, "amount": 107900.0, "paid_on": self.today.isoformat()})
        self.today = self.today + timedelta(days=20)
        restarted = build_service(self.db_path)
        restarted.clock = lambda: self.today
        view = restarted.list_installment_plans(REP, self.record["id"])
        self.assertEqual(len(view["items"]), 1)
        reloaded = view["items"][0]
        self.assertEqual(reloaded["status"], "approved")
        self.assertEqual(len(reloaded["payments"]), 1)
        self.assertEqual(reloaded["payments"][0]["amount"], 107900.0)
        self.assertEqual(reloaded["items"][0]["status"], "paid")
        self.assertEqual(reloaded["items"][1]["status"], "overdue")
        self.assertEqual(reloaded["items"][1]["days_overdue"], 5)
        dunning = restarted.list_dunning(REVIEWER)["items"]
        self.assertEqual(len(dunning), 1)
        self.assertEqual(dunning[0]["seq"], 2)


if __name__ == "__main__":
    unittest.main()
