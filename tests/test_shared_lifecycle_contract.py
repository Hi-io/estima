from __future__ import annotations

import json
import unittest
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "shared_lifecycle_contract.json"
REQUIRED_SCENARIOS = {
    "publisher_identity_binding",
    "organization_read_scope",
    "withdrawal_and_outbox_resurrection_defense",
    "withdrawal_authorization",
    "credential_rotation_and_retry",
}


class SharedLifecycleContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.scenarios = {item["id"]: item["steps"] for item in cls.contract["scenarios"]}

    def test_fixture_covers_identity_reads_withdrawal_rotation_and_replay(self) -> None:
        self.assertEqual(set(self.scenarios), REQUIRED_SCENARIOS)

    def test_every_step_uses_a_declared_principal_and_expected_status(self) -> None:
        principals = set(self.contract["principals"])
        for scenario_id, steps in self.scenarios.items():
            with self.subTest(scenario=scenario_id):
                self.assertTrue(steps)
                for step in steps:
                    self.assertIn(step["principal"], principals | {"writer_alpha_new", "writer_alpha_old"})
                    self.assertIn(step["expect_status"], {200, 201, 204, 400, 401, 403, 404, 409, 410})

    def test_withdrawal_tombstone_dominates_delayed_outbox_publish(self) -> None:
        steps = self.scenarios["withdrawal_and_outbox_resurrection_defense"]
        self.assertEqual([step["expect_status"] for step in steps], [204, 204, 410, 404, 201])
        self.assertEqual(steps[0]["episode_id"], steps[2]["episode_id"])
        self.assertEqual(steps[1]["episode_id"], steps[2]["episode_id"])
        self.assertNotEqual(steps[2]["episode_id"], steps[4]["episode_id"])
        self.assertFalse(steps[3]["visible"])
        self.assertFalse(steps[3]["pattern_member"])

    def test_cross_instance_read_and_withdrawal_authority_are_explicit(self) -> None:
        reads = self.scenarios["organization_read_scope"]
        self.assertEqual(reads[1]["visible_instances"], ["site-alpha", "site-bravo"])
        self.assertEqual(reads[2]["visible_instances"], [])

        withdrawals = self.scenarios["withdrawal_authorization"]
        self.assertEqual([step["expect_status"] for step in withdrawals], [403, 403, 204])
        self.assertEqual(withdrawals[2]["principal"], "admin_main")
        self.assertTrue(withdrawals[2]["reason"])
        self.assertEqual(withdrawals[2]["audit_actor"], "admin_main")

    def test_publish_binding_keeps_replay_idempotency_and_conflict(self) -> None:
        steps = self.scenarios["publisher_identity_binding"]
        self.assertEqual([step["expect_status"] for step in steps], [201, 403, 200, 409])
        self.assertEqual(steps[0]["event_id"], steps[2]["event_id"])
        self.assertEqual(steps[0]["episode_id"], steps[3]["episode_id"])
        self.assertEqual(steps[0]["revision"], steps[3]["revision"])
        self.assertEqual(steps[3]["payload_variant"], "changed")

    def test_rotation_preserves_identity_and_outbox_idempotency(self) -> None:
        steps = self.scenarios["credential_rotation_and_retry"]
        self.assertEqual(steps[0]["new_credential_principal"], "writer_alpha")
        self.assertTrue(steps[0]["old_credential_valid_during_overlap"])
        self.assertEqual(steps[1]["event_id"], steps[2]["event_id"])
        self.assertEqual([steps[1]["expect_status"], steps[2]["expect_status"]], [201, 200])
        self.assertTrue(steps[3]["after_overlap"])
        self.assertTrue(steps[4]["after_overlap"])


if __name__ == "__main__":
    unittest.main()
