import httpx
import pytest
from pathlib import Path

from src.ui_client import DemoAPIError, api_request, wait_until_ready


class Clock:
    def __init__(self):
        self.time = 0
    def __call__(self):
        return self.time
    def sleep(self, seconds):
        self.time += seconds


def test_readiness_retries_only_get_until_ready():
    clock = Clock()
    responses = iter([httpx.Response(503, text="<html>Starting</html>"),
                      httpx.ReadTimeout("sleeping"),
                      httpx.Response(200, json={"status": "ready"})])
    def send(method, url, **kwargs):
        assert method == "GET" and url.endswith("/readyz")
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response
    assert wait_until_ready("https://api", send=send, clock=clock, sleep=clock.sleep)
    assert clock.time == 4


def test_readiness_stops_after_120_seconds():
    clock = Clock()
    assert not wait_until_ready("https://api", clock=clock, sleep=clock.sleep,
                                send=lambda *a, **k: httpx.Response(503, text="starting"))
    assert clock.time == 120


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/action-proposals/P1/confirm"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("private"),
                                     httpx.Response(502, text="<html>private</html>"),
                                     httpx.Response(429, headers={"Retry-After": "45"})])
def test_application_post_never_retried(path, failure):
    calls = []
    payload = {"idempotency_key": "same-key-forever"}
    def send(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(failure, Exception):
            raise failure
        return failure
    with pytest.raises(DemoAPIError) as caught:
        api_request("POST", "https://api", path, "secret", send=send, json=payload)
    assert len(calls) == 1
    assert calls[0][1]["json"] == payload
    assert "private" not in str(caught.value)
    if isinstance(failure, httpx.Response) and failure.status_code == 429:
        assert "45" in str(caught.value)


def test_degraded_cache_is_usable():
    assert wait_until_ready("https://api", send=lambda *a, **k:
                            httpx.Response(200, json={"status": "degraded"}))


def test_readiness_rejects_unexpected_json():
    clock = Clock()
    assert not wait_until_ready("https://api", timeout=2, clock=clock, sleep=clock.sleep,
                                send=lambda *a, **k: httpx.Response(200, json=[]))


def test_ui_wake_and_uncertain_confirmation_keeps_key(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.ui_client as module
    monkeypatch.setattr(module, "wait_until_ready", lambda *args: True)
    sent = []
    def uncertain(method, base, path, token, **kwargs):
        sent.append((method, path, kwargs))
        raise DemoAPIError("Hasil permintaan belum diketahui.")
    monkeypatch.setattr(module, "api_request", uncertain)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    app.run()
    assert not app.exception
    assert not app.text_area  # Cannot submit while backend is asleep.
    app.sidebar.button[0].click().run()
    assert app.text_area
    app.session_state["last_chat"] = {
        "answer": "Prepared", "outcome": "action_proposed", "trace_id": "trace-demo",
        "citations": [], "action_proposal": {"proposal_id": "P1"},
    }
    app.run()
    key = app.session_state["idem_P1"]
    app.button(key="P1").click().run()
    assert not app.exception
    assert len(sent) == 1
    assert sent[0][2]["json"]["idempotency_key"] == key
    assert app.session_state["idem_P1"] == key
    app.run()  # Rerendering never replays the uncertain confirmation.
    assert len(sent) == 1
    app.button(key="P1").click().run()
    assert len(sent) == 2
    assert sent[1][2]["json"]["idempotency_key"] == key
