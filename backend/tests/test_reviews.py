from app.api import reviews
from app.core.config import clear_settings_cache
from app.services.exceptions import ConflictError, NotFoundError
from app.services.outbox import DispatchResult


def _decision_result(status: str) -> dict:
    return {
        "review_task": {"id": 9, "status": status},
        "updated_command_ids": [3] if status == "approved" else [],
        "outbox_event_key": "review-commands:9" if status == "approved" else None,
    }


def test_approve_enqueues_commands(client, monkeypatch):
    monkeypatch.setattr(
        reviews,
        "decide_review_task",
        lambda *args: _decision_result("approved"),
    )
    monkeypatch.setattr(
        reviews,
        "dispatch_event",
        lambda key: DispatchResult(key, "dispatched", "job-cmd", False),
    )
    response = client.post(
        "/review-tasks/9/approve", json={"reviewer": "maintainer"}
    )
    assert response.status_code == 200
    assert response.json()["rq_job_id"] == "job-cmd"


def test_reject_never_enqueues(client, monkeypatch):
    monkeypatch.setattr(
        reviews,
        "decide_review_task",
        lambda *args: _decision_result("rejected"),
    )
    monkeypatch.setattr(
        reviews,
        "dispatch_event",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    response = client.post(
        "/review-tasks/9/reject", json={"reviewer": "maintainer"}
    )
    assert response.status_code == 200
    assert response.json()["review_task"]["status"] == "rejected"


def test_missing_review_returns_404(client, monkeypatch):
    def fail(*args):
        raise NotFoundError("Review task not found")

    monkeypatch.setattr(reviews, "decide_review_task", fail)
    response = client.post("/review-tasks/404/reject", json={"reviewer": "m"})
    assert response.status_code == 404


def test_decided_review_returns_409(client, monkeypatch):
    def fail(*args):
        raise ConflictError("already decided")

    monkeypatch.setattr(reviews, "decide_review_task", fail)
    response = client.post("/review-tasks/9/reject", json={"reviewer": "m"})
    assert response.status_code == 409


def test_review_endpoints_require_admin_token(unauthenticated_client):
    responses = [
        unauthenticated_client.get("/review-tasks?status=pending"),
        unauthenticated_client.post(
            "/review-tasks/9/approve",
            json={"reviewer": "maintainer"},
        ),
        unauthenticated_client.post(
            "/review-tasks/9/reject",
            json={"reviewer": "maintainer"},
        ),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid review admin token"}
        assert response.headers["www-authenticate"] == "APIKey"


def test_review_endpoint_rejects_wrong_admin_token(unauthenticated_client):
    response = unauthenticated_client.get(
        "/review-tasks?status=pending",
        headers={"X-Review-Admin-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid review admin token"}
    assert response.headers["www-authenticate"] == "APIKey"


def test_review_endpoint_accepts_valid_admin_token(
    unauthenticated_client,
    review_admin_headers,
    monkeypatch,
):
    monkeypatch.setattr(reviews, "list_review_tasks", lambda status: [])

    response = unauthenticated_client.get(
        "/review-tasks?status=pending",
        headers=review_admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_review_authentication_fails_closed_when_not_configured(
    unauthenticated_client,
    review_admin_headers,
    monkeypatch,
):
    monkeypatch.delenv("REVIEW_ADMIN_TOKEN", raising=False)
    clear_settings_cache()

    response = unauthenticated_client.get(
        "/review-tasks?status=pending",
        headers=review_admin_headers,
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Review admin authentication is not configured"
    }


def test_review_admin_security_scheme_is_in_openapi(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()

    assert schema["components"]["securitySchemes"]["ReviewAdminToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Review-Admin-Token",
        "description": (
            "Administrative token required to read and decide review tasks."
        ),
    }

    protected_operations = [
        schema["paths"]["/review-tasks"]["get"],
        schema["paths"]["/review-tasks/{review_task_id}/approve"]["post"],
        schema["paths"]["/review-tasks/{review_task_id}/reject"]["post"],
    ]

    for operation in protected_operations:
        assert {"ReviewAdminToken": []} in operation["security"]


def test_review_auth_does_not_protect_health(unauthenticated_client):
    response = unauthenticated_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
