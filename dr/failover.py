"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from state import snapshot  # noqa: E402
from dr._events import append_event, atomic_write, positive  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")
REQUEST_TIMEOUT = 2.0
READY_INTERVAL = 0.5


def emit(**kw):
    """Append one timestamped JSONL event and print the same record."""
    return append_event(LOG, **kw)


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    state = response.json()
    if not isinstance(state, dict) or state.get("region") != region:
        raise ValueError("Target returned an unexpected state payload")
    return state


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm up, and verify the target before publishing the cutover.

    Step records are emitted on completion, including explicit failure records.
    No failure path writes ACTIVE. The caller receives all replica metrics so
    the runbook does not repeat restore or cutover operations.
    """
    started = time.monotonic()
    result = {"ok": False, "target": target, "backend": backend, "cutover": False}
    current_step = "1_verify_target"
    step_started = started
    try:
        if target not in URL or backend not in {"fs", "minio"}:
            raise ValueError("Invalid target or snapshot backend")
        positive("wait", wait)
        previous = ACTIVE.read_text().strip() if ACTIVE.exists() else "a"
        if previous not in URL or previous == target:
            raise ValueError("Target must differ from the current active region")
        result["previous_region"] = previous
        before = state_of(target)
        emit(step=current_step, ok=True, target=target, state=before,
             duration_s=round(time.monotonic() - step_started, 6))

        current_step = "2_restore_snapshot"
        step_started = time.monotonic()
        manifest = snapshot.get(target, backend)
        source = manifest.get("source_region", previous)
        if source != previous:
            raise ValueError(f"Snapshot source {source!r} differs from active region {previous!r}")
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        result.update(rpo)
        result["embed_model_version"] = manifest["embed_model_version"]
        result["snapshot"] = manifest
        emit(step=current_step, ok=True, target=target, **rpo,
             embed_model_version=manifest["embed_model_version"],
             snapshot_at=manifest.get("snapshot_at"), source_region=source,
             duration_s=round(time.monotonic() - step_started, 6))

        current_step = "3_scale_pool"
        step_started = time.monotonic()
        atomic_write(pathlib.Path(f"state/region-{target}/pool_state"), "full")
        emit(step=current_step, ok=True, target=target, pool_state="full",
             duration_s=round(time.monotonic() - step_started, 6))

        current_step = "4_wait_ready"
        step_started = time.monotonic()
        deadline = step_started + wait
        last_reason = "not_probed"
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                response = httpx.get(
                    f"{URL[target]}/readyz", timeout=min(REQUEST_TIMEOUT, remaining),
                )
                body = response.json()
                if not isinstance(body, dict):
                    last_reason = "invalid_readiness_payload"
                elif response.status_code == 200 and body.get("ready") is True:
                    replica = state_of(target)
                    if not replica.get("weights") or replica.get("count", 0) < 1:
                        raise ValueError("Ready target has incomplete replica state")
                    result.update(replica=replica, count=replica["count"], weights=replica["weights"])
                    waited = round(time.monotonic() - step_started, 6)
                    result["waited_s"] = waited
                    emit(step=current_step, ok=True, target=target, waited_s=waited,
                         readiness=body, replica=replica)
                    break
                else:
                    last_reason = f"http_{response.status_code}: {body.get('reasons', [])}"
            except httpx.HTTPError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            except json.JSONDecodeError:
                last_reason = "invalid_readiness_json"
            time.sleep(max(0.0, min(READY_INTERVAL, deadline - time.monotonic())))
        else:
            raise TimeoutError(f"Target {target} not ready within {wait}s: {last_reason}")

        current_step = "5_dns_cutover"
        step_started = time.monotonic()
        current = ACTIVE.read_text().strip() if ACTIVE.exists() else "a"
        if current != previous:
            raise RuntimeError("Active region changed during failover; aborting cutover")
        atomic_write(ACTIVE, target)
        result.update(ok=True, cutover=True, active_region=target)
        cutover = emit(step=current_step, ok=True, target=target, previous_region=previous,
                       active_region=target, duration_s=round(time.monotonic() - step_started, 6))
        result["cutover_ts"] = cutover["ts"]
    except (Exception, SystemExit) as exc:
        result.update(ok=False, failed_step=current_step, error=f"{type(exc).__name__}: {exc}")
        emit(step=current_step, ok=False, target=target, error=result["error"],
             duration_s=round(time.monotonic() - step_started, 6))
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
