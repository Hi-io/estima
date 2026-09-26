from __future__ import annotations

import json
import os
import uuid
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from estima.app import create_app
from estima.repository import CredentialUnavailable, InvalidCursor, IdempotencyConflict, _cursor_time_id, _encode_cursor, _search_cursor_key


TOKEN = "collective-test-token-with-at-least-32-bytes"
ADMIN_TOKEN = "collective-admin-token-with-at-least-32-bytes"


class CursorTests(unittest.TestCase):
    def test_case_and_search_cursors_are_typed_and_timezone_aware(self) -> None:
        case_id = str(uuid.uuid4())
        search_cursor = _encode_cursor({
            "kind": "search", "score": 0.75,
            "observed_at": "2026-08-02T09:15:00+09:00", "id": case_id,
        })
        case_cursor = _encode_cursor({
            "kind": "cases", "observed_at": "2026-08-02T09:15:00+09:00", "id": case_id,
        })

        self.assertEqual(
            _search_cursor_key(search_cursor),
            (0.75, "2026-08-02T00:15:00Z", case_id),
        )
        observed_at, parsed_id = _cursor_time_id(case_cursor, "cases")
        self.assertEqual(observed_at.isoformat(), "2026-08-02T09:15:00+09:00")
        self.assertEqual(str(parsed_id), case_id)

    def test_invalid_pagination_cursors_are_rejected(self) -> None:
        with self.assertRaises(InvalidCursor):
            _search_cursor_key("not-a-cursor")
        with self.assertRaises(InvalidCursor):
            _cursor_time_id(_encode_cursor({"kind": "search"}), "cases")


class MemoryRepository:
    def __init__(self) -> None:
        self.cases: dict[tuple[str, str, int], dict] = {}
        self.search_call: dict | None = None
        self.pattern_call: dict | None = None
        self.case_list_call: dict | None = None
        self.credentials: dict[str, dict] = {}
        self.credential_audit: list[dict] = []

    def migrate(self) -> None:
        pass

    def healthcheck(self) -> bool:
        return True

    def authenticate_token(self, secret: str) -> dict | None:
        now = datetime.now(timezone.utc)
        credential = next((item for item in self.credentials.values() if item["secret"] == secret), None)
        if credential is None or credential["revoked"]:
            return None
        if credential["valid_until"] is not None and credential["valid_until"] <= now:
            return None
        return {key: credential[key] for key in ("key_id", "role", "instance_id")}

    def create_credential(self, *, instance_id: str | None, role: str, actor_key_id: str) -> dict:
        key_id = str(uuid.uuid4())
        secret = f"credential-{uuid.uuid4()}"
        self.credentials[key_id] = {
            "key_id": key_id, "secret": secret, "role": role, "instance_id": instance_id,
            "valid_until": None, "revoked": False, "superseded_by": None,
        }
        self.credential_audit.append({"action": "credential_issued", "actor_key_id": actor_key_id, "instance_id": instance_id, "role": role})
        return {"key_id": key_id, "secret": secret, "role": role, "instance_id": instance_id}

    def rotate_credential(self, *, key_id: str, actor_key_id: str, overlap_seconds: int) -> dict:
        current = self.credentials.get(key_id)
        if current is None or current["revoked"] or current["superseded_by"]:
            raise CredentialUnavailable("Credential cannot be rotated")
        if current["role"] != "publisher" or not current["instance_id"]:
            raise CredentialUnavailable("Only a publisher credential can be rotated")
        replacement = self.create_credential(
            instance_id=current["instance_id"], role=current["role"], actor_key_id=actor_key_id
        )
        current["valid_until"] = datetime.now(timezone.utc) + timedelta(seconds=overlap_seconds)
        current["superseded_by"] = replacement["key_id"]
        self.credential_audit[-1]["action"] = "credential_rotated"
        return {**replacement, "old_credential_valid_until": current["valid_until"].isoformat().replace("+00:00", "Z")}

    def revoke_credential(self, *, key_id: str, actor_key_id: str) -> None:
        credential = self.credentials.get(key_id)
        if credential and not credential["revoked"]:
            credential["revoked"] = True
            self.credential_audit.append({"action": "credential_revoked", "actor_key_id": actor_key_id, "instance_id": credential["instance_id"], "role": credential["role"]})

    def create_case(self, case: dict) -> dict:
        key = (case["instance_id"], case["episode_id"], case["revision"])
        existing = self.cases.get(key)
        if existing:
            if any(existing[name] != value for name, value in case.items()):
                raise IdempotencyConflict("This idempotency key already has a different case payload")
            return {"case": deepcopy(existing), "created": False}
        stored = {**deepcopy(case), "id": str(uuid.uuid4())}
        self.cases[key] = stored
        return {"case": deepcopy(stored), "created": True}

    def get_case(self, case_id: str) -> dict | None:
        return next((deepcopy(case) for case in self.cases.values() if case["id"] == case_id), None)

    def stats(self) -> dict[str, int]:
        latest: dict[tuple[str, str], dict] = {}
        for case in self.cases.values():
            key = (case["instance_id"], case["episode_id"])
            if key not in latest or case["revision"] > latest[key]["revision"]:
                latest[key] = case
        patterns = {
            (observation["kind"], observation["key"], json.dumps(observation["value"], sort_keys=True), observation["unit"])
            for case in latest.values()
            for observation in case["observations"]
        }
        return {"episodes": len(latest), "revisions": len(self.cases), "patterns": len(patterns)}

    def list_cases(self, **kwargs) -> dict:
        self.case_list_call = kwargs
        return {"cases": [], "limit": kwargs["limit"], "has_more": False, "next_cursor": None}

    def search(self, **kwargs) -> dict:
        self.search_call = kwargs
        return {"cases": [], "limit": kwargs["limit"], "has_more": False, "next_cursor": None}

    def list_patterns(self, **kwargs) -> dict:
        self.pattern_call = kwargs
        return {"patterns": [], "limit": kwargs["limit"], "has_more": False}

    def get_pattern(self, pattern_id: str) -> dict | None:
        return None


def case_payload(**changes) -> dict:
    payload = {
        "schema_version": 1,
        "normalization_version": "fp-v1",
        "instance_id": "site-alpha",
        "episode_id": "episode-17",
        "observed_at": "2026-08-02T09:15:00+09:00",
        "scope": {
            "environment": "prod",
            "cluster": "west-1",
            "namespace": "checkout",
            "service": "cart",
            "workload": "cart-api",
            "cnfc_id": "cnfc-3",
            "vnfc_id": "vnfc-8",
        },
        "summary": "Container restart count increased",
        "observations": [
            {
                "kind": "Metric",
                "key": "Restart Count",
                "value": 3,
                "unit": "count",
                "source": "prometheus",
                "observed_at": "2026-08-02T00:10:00Z",
                "reference": "metric:pod-restarts",
            }
        ],
        "hypotheses": [{
            "statement": "Memory pressure may have contributed",
            "confidence": None,
            "supporting_refs": ["metric:pod-restarts"],
        }],
        "fingerprint": "podrestart-v1:abc123",
    }
    payload.update(changes)
    return payload


class CollectiveAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.context = TestClient(create_app(
            repository=self.repository,
            token=TOKEN,
            admin_token=ADMIN_TOKEN,
            legacy_instance_id="site-alpha",
        ))
        self.client = self.context.__enter__()
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def test_health_is_unauthenticated_and_checks_repository(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(self.client.get("/openapi.json").json()["info"]["title"], "Collective")

    def test_data_endpoints_require_bearer_token(self) -> None:
        response = self.client.post("/v1/search", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_unbound_legacy_token_is_read_only(self) -> None:
        with TestClient(create_app(repository=MemoryRepository(), token=TOKEN)) as client:
            self.assertEqual(client.get("/v1/stats", headers=self.headers).status_code, 200)
            response = client.post("/v1/cases", json=case_payload(), headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_reader_cannot_mutate_and_publisher_is_bound_but_reads_across_instances(self) -> None:
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        reader_issue = self.client.post("/v1/admin/reader-credentials", headers=admin_headers)
        publisher_issue = self.client.post(
            "/v1/admin/instances/site-beta/publisher-credentials", headers=admin_headers
        )
        self.assertEqual(reader_issue.status_code, 201)
        self.assertEqual(publisher_issue.status_code, 201)

        reader_headers = {"Authorization": f"Bearer {reader_issue.json()['secret']}"}
        beta_headers = {"Authorization": f"Bearer {publisher_issue.json()['secret']}"}
        self.assertEqual(self.client.post("/v1/cases", json=case_payload(instance_id="site-beta"), headers=reader_headers).status_code, 403)
        self.assertEqual(self.client.post("/v1/cases", json=case_payload(instance_id="site-beta"), headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/v1/cases", json=case_payload(instance_id="site-beta"), headers=admin_headers).status_code, 403)

        with TestClient(create_app(repository=self.repository, admin_token=ADMIN_TOKEN)) as beta_client:
            published = beta_client.post("/v1/cases", json=case_payload(instance_id="site-beta"), headers=beta_headers)
        self.assertEqual(published.status_code, 201)
        cross_instance_read = self.client.get(f"/v1/cases/{published.json()['case']['id']}", headers=self.headers)
        self.assertEqual(cross_instance_read.status_code, 200)
        self.assertEqual(cross_instance_read.json()["case"]["instance_id"], "site-beta")

        revoked = self.client.delete(
            f"/v1/admin/credentials/{publisher_issue.json()['key_id']}",
            headers=admin_headers,
        )
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.client.get("/v1/stats", headers=beta_headers).status_code, 401)

    def test_rotation_preserves_scope_and_revokes_old_key_after_overlap(self) -> None:
        issued = self.client.post(
            "/v1/admin/instances/site-beta/publisher-credentials",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        old_secret = issued.json()["secret"]
        old_headers = {"Authorization": f"Bearer {old_secret}"}
        with TestClient(create_app(repository=self.repository, admin_token=ADMIN_TOKEN, credential_overlap_seconds=30)) as client:
            rotated = client.post("/v1/credentials/rotate", headers=old_headers)
            self.assertEqual(rotated.status_code, 201)
            new_headers = {"Authorization": f"Bearer {rotated.json()['secret']}"}
            self.assertEqual(client.get("/v1/stats", headers=old_headers).status_code, 200)
            self.assertEqual(client.get("/v1/stats", headers=new_headers).status_code, 200)
            self.assertEqual(client.post("/v1/credentials/rotate", headers=old_headers).status_code, 409)
            self.repository.credentials[issued.json()["key_id"]]["valid_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.assertEqual(client.get("/v1/stats", headers=old_headers).status_code, 401)
            self.assertEqual(client.post("/v1/cases", json=case_payload(instance_id="site-beta"), headers=new_headers).status_code, 201)

    def test_collective_token_is_preferred_and_legacy_tokens_remain_compatible(self) -> None:
        old_token = "old-estima-token-value-long-enough"
        for values in (
            {"COLLECTIVE_API_TOKEN": TOKEN, "ESTIMA_API_TOKEN": old_token, "ATLAS_API_TOKEN": old_token},
            {"ESTIMA_API_TOKEN": TOKEN},
            {"ATLAS_API_TOKEN": TOKEN},
        ):
            with patch.dict(os.environ, values, clear=True):
                with TestClient(create_app(repository=self.repository)) as client:
                    response = client.post(
                        "/v1/search", json={}, headers={"Authorization": f"Bearer {TOKEN}"}
                    )
                    self.assertEqual(response.status_code, 200)

    def test_case_create_is_idempotent_and_returns_versioned_envelope(self) -> None:
        first = self.client.post("/v1/cases", json=case_payload(), headers=self.headers)
        self.assertEqual(first.status_code, 201)
        result = first.json()
        case = result["case"]
        self.assertTrue(result["created"])
        self.assertEqual(case["schema_version"], 1)
        self.assertEqual(case["normalization_version"], "fp-v1")
        self.assertEqual(case["revision"], 1)
        self.assertEqual(case["observed_at"], "2026-08-02T00:15:00Z")
        self.assertEqual(case["scope"]["environment"], "prod")
        self.assertEqual(case["observations"][0]["kind"], "metric")
        self.assertEqual(case["observations"][0]["key"], "restart_count")
        self.assertIsNone(case["hypotheses"][0]["confidence"])
        self.assertNotIn("statement", case["observations"][0])

        replay = self.client.post("/v1/cases", json=case_payload(), headers=self.headers)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.json()["created"])
        self.assertEqual(replay.json()["case"]["id"], case["id"])

        detail = self.client.get(f"/v1/cases/{case['id']}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["case"], case)

    def test_stats_count_episodes_revisions_and_distinct_latest_patterns(self) -> None:
        memory_observation = [{"kind": "metric", "key": "Memory", "value": 7, "unit": "count"}]
        self.client.post("/v1/cases", json=case_payload(), headers=self.headers)
        self.client.post("/v1/cases", json=case_payload(revision=2, observations=memory_observation), headers=self.headers)
        self.client.post("/v1/cases", json=case_payload(episode_id="episode-18", observations=memory_observation), headers=self.headers)

        response = self.client.get("/v1/stats", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"episodes": 2, "revisions": 3, "patterns": 1})

    def test_case_listing_passes_scope_query_and_cursor(self) -> None:
        response = self.client.get(
            "/v1/cases",
            params={
                "scope": json.dumps({"environment": "prod", "cluster": "west-1"}),
                "query": "restart count", "limit": 25, "cursor": "opaque-cursor",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "cases": [], "limit": 25, "has_more": False, "next_cursor": None,
        })
        self.assertEqual(self.repository.case_list_call["scope"], {"environment": "prod", "cluster": "west-1"})
        self.assertEqual(self.repository.case_list_call["query"], "restart count")
        self.assertEqual(self.repository.case_list_call["limit"], 25)
        self.assertEqual(self.repository.case_list_call["cursor"], "opaque-cursor")

    def test_different_payload_under_same_idempotency_key_conflicts(self) -> None:
        self.client.post("/v1/cases", json=case_payload(), headers=self.headers)
        changed = case_payload(summary="Different evidence summary")
        response = self.client.post("/v1/cases", json=changed, headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_rejects_nested_telemetry_and_secret_values_without_echoing_them(self) -> None:
        nested = case_payload(observations=[{"kind": "log", "key": "line", "value": {"raw": "too much"}}])
        response = self.client.post("/v1/cases", json=nested, headers=self.headers)
        self.assertEqual(response.status_code, 422)

        secret = "token=supersecretvalue123"
        response = self.client.post(
            "/v1/cases", json=case_payload(summary=secret), headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)

    def test_body_and_search_result_are_bounded(self) -> None:
        response = self.client.post("/v1/cases", content=b" " * (40 * 1024 + 1), headers=self.headers)
        self.assertEqual(response.status_code, 413)

        response = self.client.post("/v1/search", json={"limit": 51}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_search_supports_time_alias_and_exact_scope(self) -> None:
        response = self.client.post(
            "/v1/search",
            json={
                "query": "restart count",
                "scope": {"environment": "prod", "cluster": "west-1"},
                "before": "2026-08-03T00:00:00Z",
                "limit": 50,
                "cursor": "opaque-cursor",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cases": [], "limit": 50, "has_more": False, "next_cursor": None})
        self.assertEqual(self.repository.search_call["scope"], {"environment": "prod", "cluster": "west-1"})
        self.assertEqual(self.repository.search_call["limit"], 50)
        self.assertEqual(self.repository.search_call["cursor"], "opaque-cursor")
        self.assertEqual(self.repository.search_call["before"].isoformat(), "2026-08-03T00:00:00+00:00")

    def test_search_rejects_naive_time_and_unsafe_scope(self) -> None:
        naive = self.client.post("/v1/search", json={"before": "2026-08-03T00:00:00"}, headers=self.headers)
        self.assertEqual(naive.status_code, 422)
        secret_scope = self.client.post("/v1/search", json={"scope": {"cluster": "token=unsafevalue123"}}, headers=self.headers)
        self.assertEqual(secret_scope.status_code, 422)

    def test_pattern_list_accepts_json_scope_and_time(self) -> None:
        response = self.client.get(
            "/v1/patterns",
            params={"scope": json.dumps({"environment": "prod", "cluster": "west-1"}), "query": "restart", "observed_before": "2026-08-03T00:00:00Z", "limit": 4},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.repository.pattern_call["scope"], {"environment": "prod", "cluster": "west-1"})
        self.assertEqual(self.repository.pattern_call["limit"], 4)

    def test_pattern_detail_not_found(self) -> None:
        response = self.client.get("/v1/patterns/not-a-pattern", headers=self.headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
