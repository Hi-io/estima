from __future__ import annotations

import hashlib
import os
import unittest
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from estima.app import create_app
from estima.repository import PostgresEstimaRepository


DSN = (
    os.environ.get("COLLECTIVE_TEST_DATABASE_URL")
    or os.environ.get("ESTIMA_TEST_DATABASE_URL")
    or os.environ.get("ATLAS_TEST_DATABASE_URL")
)
ADMIN_TOKEN = "collective-postgres-admin-token-at-least-32-bytes"


def payload(instance_id: str, episode_id: str, cluster: str, pattern_key: str, revision: int = 1) -> dict:
    return {
        "schema_version": 1,
        "normalization_version": "fp-v1",
        "instance_id": instance_id,
        "episode_id": episode_id,
        "revision": revision,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"environment": "test", "cluster": cluster},
        "summary": f"Shared access control evidence for {episode_id}",
        "observations": [{"kind": "metric", "key": pattern_key, "value": 7, "unit": "count"}],
        "hypotheses": [],
    }


@unittest.skipUnless(DSN, "set COLLECTIVE_TEST_DATABASE_URL to a disposable PostgreSQL database")
class PostgresAccessControlAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresEstimaRepository(DSN)
        self.repository.migrate()
        self.prefix = f"access-test-{uuid.uuid4().hex}"
        self.cluster = f"cluster-{uuid.uuid4().hex}"
        self.pattern_key = f"pattern-{uuid.uuid4().hex[:12]}"
        self.admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        self.context = TestClient(create_app(
            repository=self.repository,
            admin_token=ADMIN_TOKEN,
            credential_overlap_seconds=3600,
        ))
        self.client = self.context.__enter__()
        self.credential_ids: list[str] = []

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        with self.repository._connect() as conn:
            for key_id in self.credential_ids:
                conn.execute("DELETE FROM atlas_api_credentials WHERE key_id = %s", (key_id,))
                conn.execute(
                    "DELETE FROM atlas_credential_audit WHERE subject_key_id = %s OR related_key_id = %s",
                    (key_id, key_id),
                )
            conn.execute("DELETE FROM atlas_cases WHERE instance_id LIKE %s", (f"{self.prefix}%",))
            conn.execute("DELETE FROM atlas_episode_tombstones WHERE instance_id LIKE %s", (f"{self.prefix}%",))
            conn.execute("DELETE FROM atlas_case_lifecycle_audit WHERE instance_id LIKE %s", (f"{self.prefix}%",))
            conn.execute("DELETE FROM atlas_credential_audit WHERE instance_id LIKE %s", (f"{self.prefix}%",))

    def issue_publisher(self, instance_id: str) -> dict:
        response = self.client.post(
            f"/v1/admin/instances/{instance_id}/publisher-credentials",
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        credential = response.json()
        self.credential_ids.append(credential["key_id"])
        return credential

    def issue_reader(self) -> dict:
        response = self.client.post("/v1/admin/reader-credentials", headers=self.admin_headers)
        self.assertEqual(response.status_code, 201, response.text)
        credential = response.json()
        self.credential_ids.append(credential["key_id"])
        return credential

    @staticmethod
    def headers(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}

    def test_roles_bind_publish_identity_and_keep_shared_reads_org_wide(self) -> None:
        alpha = f"{self.prefix}-alpha"
        bravo = f"{self.prefix}-bravo"
        alpha_credential = self.issue_publisher(alpha)
        bravo_credential = self.issue_publisher(bravo)
        reader_credential = self.issue_reader()
        alpha_headers = self.headers(alpha_credential["secret"])
        bravo_headers = self.headers(bravo_credential["secret"])
        reader_headers = self.headers(reader_credential["secret"])

        denied = self.client.post(
            "/v1/cases",
            json=payload(alpha, "reader-write", self.cluster, self.pattern_key),
            headers=reader_headers,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post("/v1/admin/reader-credentials", headers=reader_headers).status_code, 403)

        alpha_case = payload(alpha, "alpha-episode", self.cluster, self.pattern_key)
        bravo_case = payload(bravo, "bravo-episode", self.cluster, self.pattern_key)
        alpha_write = self.client.post("/v1/cases", json=alpha_case, headers=alpha_headers)
        bravo_write = self.client.post("/v1/cases", json=bravo_case, headers=bravo_headers)
        self.assertEqual(alpha_write.status_code, 201, alpha_write.text)
        self.assertEqual(bravo_write.status_code, 201, bravo_write.text)

        overwritten = self.client.post(
            "/v1/cases",
            json=payload(bravo, "spoofed-episode", self.cluster, self.pattern_key),
            headers=alpha_headers,
        )
        self.assertEqual(overwritten.status_code, 403)
        admin_write = self.client.post(
            "/v1/cases",
            json=payload("arbitrary-instance", "admin-episode", self.cluster, self.pattern_key),
            headers=self.admin_headers,
        )
        self.assertEqual(admin_write.status_code, 403)

        for headers in (alpha_headers, reader_headers):
            listed = self.client.get("/v1/cases", params={"cluster": self.cluster}, headers=headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual({case["instance_id"] for case in listed.json()["cases"]}, {alpha, bravo})
            self.assertEqual(
                {case["episode_id"] for case in listed.json()["cases"]},
                {"alpha-episode", "bravo-episode"},
            )
            detail = self.client.get(f"/v1/cases/{bravo_write.json()['case']['id']}", headers=headers)
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["case"]["instance_id"], bravo)

        search = self.client.post(
            "/v1/search",
            json={"instance_id": bravo, "scope": {"cluster": self.cluster}},
            headers=alpha_headers,
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual([case["instance_id"] for case in search.json()["cases"]], [bravo])

        patterns = self.client.get("/v1/patterns", params={"cluster": self.cluster}, headers=reader_headers)
        self.assertEqual(patterns.status_code, 200, patterns.text)
        shared_pattern = next(item for item in patterns.json()["patterns"] if item["key"] == self.pattern_key.replace("-", "_"))
        self.assertEqual(shared_pattern["case_count"], 2)

    def test_rotation_overlap_retry_revocation_and_audit_metadata(self) -> None:
        instance_id = f"{self.prefix}-rotate"
        old = self.issue_publisher(instance_id)
        old_headers = self.headers(old["secret"])
        case = payload(instance_id, "rotation-episode", self.cluster, self.pattern_key)
        first = self.client.post("/v1/cases", json=case, headers=old_headers)
        self.assertEqual(first.status_code, 201, first.text)

        rotated = self.client.post("/v1/credentials/rotate", headers=old_headers)
        self.assertEqual(rotated.status_code, 201, rotated.text)
        replacement = rotated.json()
        self.credential_ids.append(replacement["key_id"])
        new_headers = self.headers(replacement["secret"])
        self.assertEqual(self.client.get("/v1/stats", headers=old_headers).status_code, 200)
        retry = self.client.post("/v1/cases", json=case, headers=new_headers)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertFalse(retry.json()["created"])

        with self.repository._connect() as conn:
            conn.execute(
                "UPDATE atlas_api_credentials SET valid_until = clock_timestamp() - interval '1 second' WHERE key_id = %s",
                (old["key_id"],),
            )
        self.assertEqual(self.client.get("/v1/stats", headers=old_headers).status_code, 401)
        self.assertEqual(self.client.get("/v1/stats", headers=new_headers).status_code, 200)

        revoked = self.client.delete(
            f"/v1/admin/credentials/{replacement['key_id']}",
            headers=self.admin_headers,
        )
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.client.get("/v1/stats", headers=new_headers).status_code, 401)

        with self.repository._connect() as conn:
            stored_hash = conn.execute(
                "SELECT secret_hash FROM atlas_api_credentials WHERE key_id = %s",
                (replacement["key_id"],),
            ).fetchone()["secret_hash"]
            audit = conn.execute(
                """SELECT actor_key_id, action, subject_key_id, related_key_id, instance_id, role, overlap_until
                   FROM atlas_credential_audit WHERE instance_id = %s ORDER BY occurred_at""",
                (instance_id,),
            ).fetchall()
        by_action = {row["action"]: row for row in audit}
        self.assertEqual(set(by_action), {"credential_issued", "credential_rotated", "credential_revoked"})
        self.assertEqual(by_action["credential_issued"]["actor_key_id"], "environment-admin")
        self.assertEqual(by_action["credential_rotated"]["actor_key_id"], old["key_id"])
        self.assertEqual(by_action["credential_rotated"]["related_key_id"], uuid.UUID(old["key_id"]))
        self.assertIsNotNone(by_action["credential_rotated"]["overlap_until"])
        self.assertEqual({row["role"] for row in audit}, {"publisher"})
        self.assertEqual(stored_hash, hashlib.sha256(replacement["secret"].encode("utf-8")).digest())
        self.assertNotIn(old["secret"], str(audit))
        self.assertNotIn(replacement["secret"], str(audit))

    def test_publisher_withdrawal_is_instance_bound_and_excludes_shared_retrieval(self) -> None:
        alpha = f"{self.prefix}-withdraw-alpha"
        bravo = f"{self.prefix}-withdraw-bravo"
        charlie = f"{self.prefix}-withdraw-charlie"
        alpha_credential = self.issue_publisher(alpha)
        bravo_credential = self.issue_publisher(bravo)
        charlie_credential = self.issue_publisher(charlie)
        reader_credential = self.issue_reader()
        alpha_headers = self.headers(alpha_credential["secret"])
        bravo_headers = self.headers(bravo_credential["secret"])
        charlie_headers = self.headers(charlie_credential["secret"])
        reader_headers = self.headers(reader_credential["secret"])
        shared_episode = "same-episode-id"

        alpha_first = self.client.post(
            "/v1/cases", json=payload(alpha, shared_episode, self.cluster, self.pattern_key), headers=alpha_headers
        )
        alpha_second = self.client.post(
            "/v1/cases", json=payload(alpha, shared_episode, self.cluster, self.pattern_key, revision=2), headers=alpha_headers
        )
        bravo_case = self.client.post(
            "/v1/cases", json=payload(bravo, shared_episode, self.cluster, self.pattern_key), headers=bravo_headers
        )
        charlie_case = self.client.post(
            "/v1/cases", json=payload(charlie, shared_episode, self.cluster, self.pattern_key), headers=charlie_headers
        )
        for response in (alpha_first, alpha_second, bravo_case, charlie_case):
            self.assertEqual(response.status_code, 201, response.text)

        denied = self.client.delete(f"/v1/episodes/{shared_episode}", headers=reader_headers)
        self.assertEqual(denied.status_code, 403)
        withdrawal = self.client.delete(f"/v1/episodes/{shared_episode}", headers=alpha_headers)
        repeated = self.client.delete(f"/v1/episodes/{shared_episode}", headers=alpha_headers)
        self.assertEqual(withdrawal.status_code, 204)
        self.assertEqual(repeated.status_code, 204)
        retry = self.client.post(
            "/v1/cases", json=payload(alpha, shared_episode, self.cluster, self.pattern_key), headers=alpha_headers
        )
        self.assertEqual(retry.status_code, 410)
        self.assertEqual(
            self.client.get(f"/v1/cases/{alpha_first.json()['case']['id']}", headers=reader_headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/v1/cases/{alpha_second.json()['case']['id']}", headers=reader_headers).status_code,
            404,
        )

        search = self.client.post(
            "/v1/search", json={"instance_id": alpha, "scope": {"cluster": self.cluster}}, headers=reader_headers
        )
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["cases"], [])
        pattern = self.client.get(
            "/v1/patterns", params={"cluster": self.cluster}, headers=reader_headers
        )
        self.assertEqual(pattern.status_code, 200)
        shared_pattern = next(item for item in pattern.json()["patterns"] if item["key"] == self.pattern_key.replace("-", "_"))
        self.assertEqual(shared_pattern["case_count"], 2)
        detail = self.client.get(f"/v1/patterns/{shared_pattern['id']}", headers=reader_headers)
        self.assertEqual(
            {case["instance_id"] for case in detail.json()["cases"]},
            {bravo, charlie},
        )
        self.assertEqual(
            self.client.get(f"/v1/cases/{bravo_case.json()['case']['id']}", headers=reader_headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/v1/cases/{charlie_case.json()['case']['id']}", headers=reader_headers).status_code,
            200,
        )

        with self.repository._connect() as conn:
            tombstone = conn.execute(
                "SELECT reason, actor_key_id FROM atlas_episode_tombstones "
                "WHERE instance_id = %s AND episode_id = %s",
                (alpha, shared_episode),
            ).fetchone()
            audit = conn.execute(
                "SELECT actor_key_id, action, deleted_case_count FROM atlas_case_lifecycle_audit "
                "WHERE instance_id = %s AND episode_id = %s",
                (alpha, shared_episode),
            ).fetchall()
        self.assertEqual(tombstone["reason"], "publisher")
        self.assertEqual(tombstone["actor_key_id"], alpha_credential["key_id"])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["actor_key_id"], alpha_credential["key_id"])
        self.assertEqual(audit[0]["action"], "episode_withdrawn")
        self.assertEqual(audit[0]["deleted_case_count"], 2)

    def test_unbound_legacy_token_is_read_only_and_remains_org_reader(self) -> None:
        instance_id = f"{self.prefix}-legacy"
        publisher = self.issue_publisher(instance_id)
        published = self.client.post(
            "/v1/cases",
            json=payload(instance_id, "legacy-episode", self.cluster, self.pattern_key),
            headers=self.headers(publisher["secret"]),
        )
        self.assertEqual(published.status_code, 201)

        legacy_token = "collective-legacy-token-at-least-32-bytes"
        with TestClient(create_app(repository=self.repository, token=legacy_token)) as legacy_client:
            legacy_headers = self.headers(legacy_token)
            self.assertEqual(legacy_client.get("/v1/stats", headers=legacy_headers).status_code, 200)
            self.assertEqual(
                legacy_client.get(f"/v1/cases/{published.json()['case']['id']}", headers=legacy_headers).status_code,
                200,
            )
            denied = legacy_client.post(
                "/v1/cases",
                json=payload("claimed-instance", "spoof", self.cluster, self.pattern_key),
                headers=legacy_headers,
            )
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
