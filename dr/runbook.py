"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402
from dr._events import append_event  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
REQUEST_TIMEOUT = 2.0
DETECTION_WAIT = 60.0


def step(n, name, **kw):
    """Append one completed checklist step with a real timestamp."""
    return append_event(LOG, step=n, name=name, **kw)


def confirm(auto: bool, msg: str) -> bool:
    """Only explicit affirmative input permits a manual cutover."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _records(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            # The health checker may currently be appending its last line.
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _outage(primary: str) -> dict | None:
    events = [event for event in _records(CHAOS_LOG)
              if event.get("region") == primary and event.get("action") in {"kill", "restore"}]
    return events[-1] if events and events[-1]["action"] == "kill" else None


def _await_detection(primary: str, outage: dict) -> dict:
    """For a lab drill, require the independent checker to raise the alert."""
    deadline = time.monotonic() + DETECTION_WAIT
    while time.monotonic() < deadline:
        events = [event for event in _records(HEALTH_LOG)
                  if event.get("event") == "state_change"
                  and event.get("region") == primary
                  and event.get("ts", 0) >= outage["ts"]]
        if events and events[-1].get("to") == "UNHEALTHY":
            return events[-1]
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("No current UNHEALTHY alert; start the health checker before the drill")


def _golden_signals(target: str) -> dict:
    """Ten real target requests; nearest-rank p95 includes failed requests."""
    samples = []
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for index in range(10):
            started = time.monotonic()
            sample = {"request": index + 1, "ts": time.time(), "ok": False}
            try:
                response = client.get(f"{URL[target]}/v1/infer")
                body = response.json()
                sample.update(
                    status=response.status_code,
                    served_by=body.get("region") if isinstance(body, dict) else None,
                )
                sample["ok"] = (
                    response.status_code == 200 and sample["served_by"] == target
                    and isinstance(body, dict) and bool(body.get("answer"))
                    and not body.get("error")
                )
            except (httpx.HTTPError, ValueError) as exc:
                sample["error"] = f"{type(exc).__name__}: {exc}"
            sample["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            samples.append(sample)
    latencies = sorted(sample["latency_ms"] for sample in samples)
    failed = sum(not sample["ok"] for sample in samples)
    return {
        "requests": len(samples), "requests_failed": failed,
        "error_rate": failed / len(samples),
        "p95_latency_ms": latencies[math.ceil(0.95 * len(samples)) - 1],
        "p95_method": "nearest_rank", "samples": samples,
        "scope": "target_direct; edge recovery is measured by loadgen",
        "ok": failed == 0,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute one semi-automated incident response without automatic failback."""
    started = time.monotonic()
    result = {"ok": False, "primary": primary, "target": target, "auto": auto}
    current_step, current_name = 1, "xac_nhan_outage"
    try:
        if primary not in URL or target not in URL or primary == target:
            raise ValueError("Primary and target must be distinct known regions")
        if backend not in {"fs", "minio"}:
            raise ValueError("Unknown snapshot backend")
        active = fo.ACTIVE.read_text().strip() if fo.ACTIVE.exists() else "a"
        if active != primary:
            raise ValueError("Primary differs from the current active region")
        outage = _outage(primary)
        if outage and (outage.get("forced_both") or not outage.get("other_alive")):
            raise ValueError("Refusing an invalid double-outage drill")
        probes = []
        for index in range(3):
            primary_ready, reason = hc.probe(primary, REQUEST_TIMEOUT)
            target_ready, target_reason = hc.probe(target, REQUEST_TIMEOUT)
            alive = httpx.get(f"{URL[target]}/healthz", timeout=REQUEST_TIMEOUT)
            alive.raise_for_status()
            alive_body = alive.json()
            if not isinstance(alive_body, dict) or alive_body.get("alive") is not True:
                raise ValueError("Target liveness check failed")
            probes.append({
                "ts": time.time(), "primary_ready": primary_ready, "reason": reason,
                "target_ready": target_ready, "target_reason": target_reason,
                "target_alive": True,
            })
            if primary_ready:
                raise RuntimeError("Primary is ready; no confirmed outage to fail over")
            if index < 2:
                time.sleep(1.0)
        detection = _await_detection(primary, outage) if outage else None
        # Recheck after waiting for the alert, before asking the operator.
        primary_ready, reason = hc.probe(primary, REQUEST_TIMEOUT)
        if primary_ready:
            raise RuntimeError("Primary recovered while waiting for the alert")
        step(1, current_name, ok=True, probes=probes, final_probe_reason=reason,
             t_outage=outage["ts"] if outage else None,
             t_detect=detection["ts"] if detection else None)

        current_step, current_name = 2, "thong_bao_incident"
        incident = step(2, current_name, ok=True, primary=primary, target=target,
                        owner="on-call", confirmation_mode="auto" if auto else "manual",
                        t_outage=outage["ts"] if outage else None,
                        t_outage_iso=outage.get("iso") if outage else None)
        result["incident_ts"] = incident["ts"]
        result["notification_delay_s"] = (
            round(incident["ts"] - outage["ts"], 6) if outage else None
        )
        if not confirm(auto, f"Fail over {primary} -> {target} using {backend}?"):
            result.update(cancelled=True, error="operator_declined")
            step(7, "post_incident", ok=False, cancelled=True,
                 elapsed_s=round(time.monotonic() - started, 6))
            return result
        operator_confirm_ts = time.time()

        current_step, current_name = 3, "scale_gpu_pool"
        recovery = fo.failover(target, backend, wait=60.0)
        result["failover"] = recovery
        step(3, current_name, ok=recovery["ok"], operator_confirm_ts=operator_confirm_ts,
             failover_result=recovery, substeps_log=str(fo.LOG))
        if not recovery["ok"]:
            result["error"] = recovery.get("error", "failover_failed")
            step(7, "post_incident", ok=False, failed_step=3, error=result["error"],
                 elapsed_s=round(time.monotonic() - started, 6))
            return result

        current_step, current_name = 4, "verify_state_replica"
        replica = recovery["replica"]
        step(4, current_name, ok=True, count=replica["count"], weights=replica["weights"],
             embed_model_version=recovery["embed_model_version"],
             rpo_seconds=recovery["rpo_seconds"], docs_lost=recovery["docs_lost"])

        current_step, current_name = 5, "dns_cutover"
        step(5, current_name, ok=recovery["cutover"], active_region=recovery["active_region"],
             cutover_ts=recovery["cutover_ts"])

        current_step, current_name = 6, "verify_golden_signals"
        signals = _golden_signals(target)
        step(6, current_name, target=target, **signals)
        result.update(ok=signals["ok"], golden_signals=signals)
        if not result["ok"]:
            result["error"] = "Golden signals failed; operator assessment required"

        current_step, current_name = 7, "post_incident"
        result["elapsed_s"] = round(time.monotonic() - started, 6)
        step(7, current_name, ok=result["ok"], elapsed_s=result["elapsed_s"],
             notification_delay_s=result["notification_delay_s"],
             rpo_seconds=recovery["rpo_seconds"], docs_lost=recovery["docs_lost"],
             measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
             note="Runbook elapsed time is not user-observed RTO; wait for loadgen evidence")
    except (Exception, SystemExit) as exc:
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}", failed_step=current_step,
                      elapsed_s=round(time.monotonic() - started, 6))
        step(current_step, current_name, ok=False, error=result["error"])
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a", choices=["a", "b"])
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
