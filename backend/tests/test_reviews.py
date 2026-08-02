from app.api import reviews
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
