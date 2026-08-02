from app.core.tracing import TraceSession, trace_node, use_trace


class Recorder:
    def __init__(self):
        self.started = []
        self.finished = []
        self.failed = []

    def start_node(self, name, input_summary):
        self.started.append((name, input_summary))
        return 1

    def finish_node(self, *args):
        self.finished.append(args)

    def fail_node(self, *args):
        self.failed.append(args)


def test_node_trace_records_safe_summaries_and_usage():
    recorder = Recorder()
    trace = TraceSession("trace-1", recorder=recorder)

    @trace_node("demo")
    def demo(state):
        trace.add_usage(12, 4)
        return {"category": "bug", "summary": "private text"}

    with use_trace(trace):
        result = demo({"title": "secret title", "body": "secret body", "repo": "o/r"})

    assert result["category"] == "bug"
    assert recorder.started[0][0] == "demo"
    assert recorder.started[0][1]["title_chars"] == 12
    assert "secret title" not in str(recorder.started)
    assert recorder.finished[0][3:] == (12, 4)
    assert "private text" not in str(recorder.finished)


def test_node_trace_sanitizes_failure():
    recorder = Recorder()
    trace = TraceSession("trace-2", recorder=recorder)

    @trace_node("failure")
    def failure(_):
        raise RuntimeError("Authorization: Bearer super-secret")

    try:
        with use_trace(trace):
            failure({})
    except RuntimeError:
        pass
    assert recorder.failed
    assert "super-secret" not in str(recorder.failed)
