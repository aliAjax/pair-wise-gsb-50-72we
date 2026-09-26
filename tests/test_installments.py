import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
FLOW = [('investigate', 'inspector', {'plan': '核对账簿'}), ('propose', 'inspector', {'proposal': '补税并处罚'}), ('review', 'reviewer', {'outcome': 'accepted', 'review_note': '证据充分'}), ('close', 'reviewer', {'final_decision': '维持处理'})]
TOTAL_DUE = 323700.0
REP = Actor("rep", "taxpayer_rep")
REVIEWER = Actor("checker", "reviewer")


def day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


def schedule(*amounts, first_in=10, gap=20):
    return {"installments": [{"due_date": day(first_in + gap * index), "amount": amount} for index, amount in enumerate(amounts)]}


class InstallmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def closed_record(self, reference="TAX-26001"):
        record = self.service.create(Actor("creator", "inspector"), reference, CREATE_DATA)
        for action, role, data in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def approved_plan(self, record):
        plan = self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        return self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "approve"})

    def test_full_flow_until_certificate(self):
        record = self.closed_record()
        with self.assertRaises(Conflict):
            self.service.closure_certificate(REVIEWER, record["id"])
        plan = self.approved_plan(record)
        self.assertEqual(plan["status"], "approved")
        self.assertEqual(plan["total_amount"], TOTAL_DUE)
        self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 161850.0, "paid_date": day(1)})
        with self.assertRaises(Conflict):
            self.service.closure_certificate(REVIEWER, record["id"])
        self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 2, "amount": 161850.0})
        overview = self.service.installment_overview(REVIEWER, record["id"])
        self.assertEqual(overview["unpaid"], 0.0)
        self.assertEqual([item["status"] for item in overview["plan"]["installments"]], ["paid", "paid"])
        self.assertEqual(overview["plan"]["reminders"], [])
        certificate = self.service.closure_certificate(REVIEWER, record["id"])
        self.assertEqual(certificate["record_id"], record["id"])
        self.assertEqual(certificate["paid_total"], TOTAL_DUE)

    def test_schedule_validation(self):
        record = self.closed_record()
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(TOTAL_DUE))
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(*([53950.0] * 7)))
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0, first_in=16))
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0, gap=31))
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0, gap=0))
        with self.assertRaises(ValidationError):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161849.0))

    def test_apply_requires_closed_state_and_rep_role(self):
        opened = dict(CREATE_DATA, tax_period="2025-Q3")
        record = self.service.create(Actor("creator", "inspector"), "TAX-26002", opened)
        with self.assertRaises(Conflict):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        closed = self.closed_record("TAX-26003")
        with self.assertRaises(PermissionDenied):
            self.service.apply_installments(Actor("op", "inspector"), closed["id"], schedule(161850.0, 161850.0))

    def test_pending_and_active_plan_block_new_application(self):
        record = self.closed_record()
        self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        with self.assertRaises(Conflict):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        plan = self.service.review_installments(REVIEWER, record["id"], self.service.installment_overview(REVIEWER, record["id"])["plan"]["id"], {"decision": "approve"})
        with self.assertRaises(Conflict):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))

    def test_overdue_plan_blocks_application_and_shows_reminder(self):
        record = self.closed_record()
        clock = {"day": date.today()}
        self.service._today = lambda: clock["day"]
        plan = self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0, first_in=5))
        self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "approve"})
        clock["day"] = date.today() + timedelta(days=6)
        overview = self.service.installment_overview(REVIEWER, record["id"])
        self.assertEqual(len(overview["plan"]["reminders"]), 1)
        self.assertEqual(overview["plan"]["reminders"][0]["seq"], 1)
        self.assertEqual(overview["plan"]["installments"][0]["status"], "overdue")
        with self.assertRaises(Conflict):
            self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 161850.0})
        overview = self.service.installment_overview(REVIEWER, record["id"])
        self.assertEqual(overview["plan"]["reminders"], [])

    def test_returned_plan_allows_reapply(self):
        record = self.closed_record()
        plan = self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        with self.assertRaises(ValidationError):
            self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "return"})
        returned = self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "return", "note": "金额需重排"})
        self.assertEqual(returned["status"], "returned")
        again = self.service.apply_installments(REP, record["id"], schedule(107900.0, 107900.0, 107900.0))
        self.assertEqual(again["status"], "pending")

    def test_payment_rules(self):
        record = self.closed_record()
        plan = self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0))
        with self.assertRaises(Conflict):
            self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 100.0})
        self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "approve"})
        with self.assertRaises(PermissionDenied):
            self.service.register_payment(REP, record["id"], plan["id"], {"seq": 1, "amount": 100.0})
        with self.assertRaises(ValidationError):
            self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 161850.01})
        with self.assertRaises(ValidationError):
            self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 9, "amount": 100.0})
        self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 161850.0})
        with self.assertRaises(Conflict):
            self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 1, "amount": 1.0})

    def test_restart_keeps_plan_payments_and_reminders(self):
        record = self.closed_record()
        clock = {"day": date.today()}
        self.service._today = lambda: clock["day"]
        plan = self.service.apply_installments(REP, record["id"], schedule(161850.0, 161850.0, first_in=5))
        self.service.review_installments(REVIEWER, record["id"], plan["id"], {"decision": "approve"})
        self.service.register_payment(REVIEWER, record["id"], plan["id"], {"seq": 2, "amount": 161850.0})
        reopened = build_service(self.db_path)
        clock["day"] = date.today() + timedelta(days=6)
        reopened._today = lambda: clock["day"]
        overview = reopened.installment_overview(REVIEWER, record["id"])
        self.assertEqual(overview["plan"]["id"], plan["id"])
        self.assertEqual(len(overview["plan"]["payments"]), 1)
        self.assertEqual(overview["plan"]["payments"][0]["seq"], 2)
        self.assertEqual(len(overview["plan"]["reminders"]), 1)
        self.assertEqual(overview["paid_total"], 161850.0)


if __name__ == "__main__":
    unittest.main()
