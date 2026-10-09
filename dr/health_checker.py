"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import sys
import time
from dataclasses import dataclass

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dr._events import append_event, positive  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Probe readiness with a bounded timeout, including for a paused process."""
    if region not in URL:
        raise ValueError(f"Unknown region: {region}")
    positive("timeout", timeout)
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        body = response.json()
        if not isinstance(body, dict):
            return False, "invalid_readiness_payload"
        if response.status_code == 200 and body.get("ready") is True:
            return True, "ready"
        return False, f"http_{response.status_code}: {body.get('reasons', 'not_ready')}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except ValueError:
        return False, "invalid_readiness_json"


@dataclass
class RegionHealth:
    # A successful initial probe is a baseline, not a state transition.
    state: str = "HEALTHY"
    consecutive_fails: int = 0
    first_failure_at: float | None = None


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll on a monotonic schedule and emit only state transitions.

    In addition to consecutive failures, require an observation window of
    interval * threshold before declaring an outage. This conservative policy
    makes the lab's required detection floor explicit; a recovery resets it.
    Probe overruns skip missed ticks rather than issuing a burst of probes.
    """
    for name, value in (("interval", interval), ("timeout", timeout), ("duration", duration)):
        positive(name, value)
    if not isinstance(threshold, int) or threshold < 1:
        raise ValueError("threshold must be a positive integer")
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch(exist_ok=True)
    regions = {region: RegionHealth() for region in URL}
    deadline = time.monotonic() + duration
    next_poll = time.monotonic()
    floor = interval * threshold
    while time.monotonic() < deadline:
        for region, health in regions.items():
            if time.monotonic() >= deadline:
                break
            started = time.monotonic()
            ready, reason = probe(region, timeout)
            observed = time.monotonic()
            if ready:
                health.consecutive_fails = 0
                health.first_failure_at = None
                new_state = "HEALTHY"
            else:
                health.consecutive_fails += 1
                if health.first_failure_at is None:
                    health.first_failure_at = started
                eligible = (
                    health.consecutive_fails >= threshold
                    and observed - health.first_failure_at >= floor
                )
                new_state = "UNHEALTHY" if eligible else health.state
            if new_state != health.state:
                append_event(
                    out, event="state_change", region=region,
                    **{"from": health.state, "to": new_state},
                    reason=reason, consecutive_fails=health.consecutive_fails,
                    interval_s=interval, threshold=threshold, timeout_s=timeout,
                    detect_floor_s=floor,
                    detection_policy="consecutive_failures_with_observation_window",
                )
                health.state = new_state
        next_poll += interval
        now = time.monotonic()
        if next_poll <= now:
            next_poll += (int((now - next_poll) / interval) + 1) * interval
        time.sleep(max(0.0, min(next_poll, deadline) - time.monotonic()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
