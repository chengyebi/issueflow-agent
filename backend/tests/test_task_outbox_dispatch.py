import app.tasks as tasks


def test_dispatch_route_outbox_dispatches_created_event(monkeypatch):
    dispatched = []

    monkeypatch.setattr(
        tasks,
        "dispatch_event",
        lambda event_key: dispatched.append(event_key),
    )

    tasks._dispatch_route_outbox("github-commands:12")

    assert dispatched == ["github-commands:12"]


def test_dispatch_route_outbox_ignores_missing_event(monkeypatch):
    dispatched = []

    monkeypatch.setattr(
        tasks,
        "dispatch_event",
        lambda event_key: dispatched.append(event_key),
    )

    tasks._dispatch_route_outbox(None)

    assert dispatched == []
