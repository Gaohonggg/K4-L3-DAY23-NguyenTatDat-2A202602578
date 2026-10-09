"""Isolated safety tests: no live services, lab state, or evidence are modified."""

import json
import pathlib

import httpx
import pytest

from dr import failover as fo
from dr import health_checker as hc
from dr import runbook as rb


def _response(path, body, status=200):
    return httpx.Response(status, json=body, request=httpx.Request("GET", f"http://test{path}"))


@pytest.mark.parametrize("status,body,ready", [
    (200, {"ready": True}, True),
    (200, {"ready": False}, False),
    (503, {"ready": False, "reasons": ["model_weights_missing"]}, False),
    (200, [], False),
])
def test_probe_requires_readiness_payload(monkeypatch, status, body, ready):
    monkeypatch.setattr(hc.httpx, "get", lambda *a, **k: _response("/readyz", body, status))
    assert hc.probe("a", 1.0)[0] is ready


def test_probe_timeout_is_unhealthy(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.1
        raise httpx.ReadTimeout("paused process")
    monkeypatch.setattr(hc.httpx, "get", timeout)
    ready, reason = hc.probe("a", 0.1)
    assert not ready
    assert "ReadTimeout" in reason


def test_transient_failures_do_not_emit_outage(monkeypatch, tmp_path):
    attempts = {"a": 0, "b": 0}
    def probe(region, timeout):
        attempts[region] += 1
        return attempts[region] % 2 == 1, "intermittent"
    monkeypatch.setattr(hc, "probe", probe)
    out = tmp_path / "health.jsonl"
    hc.run(0.01, 0.01, 3, 0.1, out)
    assert not out.read_text().strip()


def test_restore_failure_does_not_publish_cutover(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    active = pathlib.Path("edge/active_region")
    active.parent.mkdir()
    active.write_text("a")
    monkeypatch.setattr(fo, "LOG", tmp_path / "failover.jsonl")
    monkeypatch.setattr(fo, "state_of", lambda region: {"region": region, "count": 0})
    def no_snapshot(*args, **kwargs):
        raise SystemExit("missing snapshot")
    monkeypatch.setattr(fo.snapshot, "get", no_snapshot)
    result = fo.failover("b", "fs", 0.1)
    assert not result["ok"]
    assert result["failed_step"] == "2_restore_snapshot"
    assert active.read_text() == "a"
    assert "5_dns_cutover" not in fo.LOG.read_text()


def _runbook_setup(monkeypatch, tmp_path):
    active = tmp_path / "active_region"
    active.write_text("a")
    monkeypatch.setattr(fo, "ACTIVE", active)
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(rb, "_outage", lambda primary: None)
    monkeypatch.setattr(hc, "probe", lambda region, timeout: (False, "not_ready"))
    monkeypatch.setattr(rb.httpx, "get", lambda *a, **k: _response("/healthz", {"alive": True}))
    monkeypatch.setattr(rb.time, "sleep", lambda duration: None)


def test_operator_decline_never_calls_failover(monkeypatch, tmp_path):
    _runbook_setup(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda message: "n")
    def forbidden(*args, **kwargs):
        pytest.fail("Failover called after operator declined")
    monkeypatch.setattr(fo, "failover", forbidden)
    result = rb.run("a", "b", "fs", auto=False)
    assert result["cancelled"]
    assert not result["ok"]


def test_runbook_calls_failover_once_and_checks_ten_requests(monkeypatch, tmp_path):
    _runbook_setup(monkeypatch, tmp_path)
    calls = []
    inference_calls = []
    def recover(target, backend, wait):
        calls.append((target, backend, wait))
        return {
            "ok": True, "cutover": True, "active_region": "b", "cutover_ts": 123.0,
            "replica": {"count": 200, "weights": True},
            "embed_model_version": "test", "rpo_seconds": 2.0, "docs_lost": 1,
        }
    def response(self, url):
        inference_calls.append(url)
        return _response("/v1/infer", {"region": "b", "answer": "[b] restored"})
    monkeypatch.setattr(fo, "failover", recover)
    monkeypatch.setattr(httpx.Client, "get", response)
    result = rb.run("a", "b", "fs", auto=True)
    assert result["ok"]
    assert len(calls) == 1
    assert len(inference_calls) == 10
    assert result["golden_signals"]["error_rate"] == 0
    records = [json.loads(line) for line in rb.LOG.read_text().splitlines()]
    assert [record["step"] for record in records] == list(range(1, 8))


def test_confirm_defaults_to_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda message: "")
    assert not rb.confirm(False, "Proceed?")
    assert rb.confirm(True, "Proceed?")
