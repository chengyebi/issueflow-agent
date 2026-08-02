import hashlib
import hmac
import json

from app.api import webhooks
from app.services.events import AcceptedIssueDelivery
from app.services.outbox import DispatchResult

SECRET = "test-webhook-secret"


def _payload(action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 7, "title": "Broken", "body": "Steps"},
        }
    ).encode()


def _headers(payload: bytes, delivery: str = "delivery-1") -> dict[str, str]:
    signature = "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


def test_missing_signature_is_rejected(client):
    response = client.post(
        "/webhooks/github",
        content=_payload(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d"},
    )
    assert response.status_code == 401


def test_invalid_signature_is_rejected(client):
    headers = _headers(_payload())
    headers["X-Hub-Signature-256"] = "sha256=invalid"
    response = client.post("/webhooks/github", content=_payload(), headers=headers)
    assert response.status_code == 401


def test_valid_signature_accepts_and_enqueues(client, monkeypatch):
    payload = _payload()
    monkeypatch.setattr(
        webhooks,
        "accept_issue_delivery",
        lambda *args: AcceptedIssueDelivery(
            True, issue_event_id=11, agent_run_id=22, outbox_event_key="agent-run:22"
        ),
    )
    monkeypatch.setattr(
        webhooks,
        "dispatch_event",
        lambda key: DispatchResult(key, "dispatched", "job-1", False),
    )

    response = client.post("/webhooks/github", content=payload, headers=_headers(payload))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["rq_job_id"] == "job-1"


def test_duplicate_delivery_does_not_enqueue(client, monkeypatch):
    payload = _payload()
    monkeypatch.setattr(
        webhooks,
        "accept_issue_delivery",
        lambda *args: AcceptedIssueDelivery(False),
    )
    monkeypatch.setattr(
        webhooks,
        "dispatch_event",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    response = client.post("/webhooks/github", content=payload, headers=_headers(payload))
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"


def test_unsupported_action_is_stored_but_not_enqueued(client, monkeypatch):
    payload = _payload("labeled")
    monkeypatch.setattr(webhooks, "save_webhook_delivery", lambda *args: True)
    monkeypatch.setattr(
        webhooks,
        "dispatch_event",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    response = client.post("/webhooks/github", content=payload, headers=_headers(payload))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_pull_request_is_not_indexed_or_analyzed(client, monkeypatch):
    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "issue": {
                "number": 8,
                "title": "PR title",
                "body": "PR body",
                "pull_request": {"url": "https://api.github.test/pulls/8"},
            },
        }
    ).encode()
    monkeypatch.setattr(webhooks, "save_webhook_delivery", lambda *args: True)
    monkeypatch.setattr(
        webhooks,
        "accept_issue_delivery",
        lambda *_: (_ for _ in ()).throw(AssertionError("PR must not be indexed")),
    )
    response = client.post("/webhooks/github", content=payload, headers=_headers(payload))
    assert response.status_code == 200
    assert response.json()["reason"] == "pull_request"


def test_enqueue_failure_is_left_for_recovery(client, monkeypatch):
    payload = _payload()
    monkeypatch.setattr(
        webhooks,
        "accept_issue_delivery",
        lambda *args: AcceptedIssueDelivery(
            True, issue_event_id=11, agent_run_id=22, outbox_event_key="agent-run:22"
        ),
    )
    monkeypatch.setattr(
        webhooks,
        "dispatch_event",
        lambda key: DispatchResult(key, "pending", None, True),
    )
    response = client.post("/webhooks/github", content=payload, headers=_headers(payload))
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["rq_job_id"] is None
    assert response.json()["recovery_pending"] is True
