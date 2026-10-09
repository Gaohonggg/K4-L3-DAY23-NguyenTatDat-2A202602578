# Runbook — Region A down, failover sang B

**Owner:** on-call thực hiện; Incident commander (IC) phê duyệt; Data owner xác nhận dữ liệu. Mục tiêu RTO/RPO 300s. Chạy từ repo root sau `source .venv/bin/activate`; bare mode, backend fs, chỉ localhost. Log append: số dòng dưới dành cho drill hiện tại; khi nhiều incident phải chọn event đúng timestamp.

**Preconditions:** A active; B process alive; checker/traffic chạy độc lập; replication có snapshot. Kiểm tra bằng `curl --max-time 3 -sS localhost:8080/edge/state`, `curl --max-time 3 -sS localhost:8002/healthz`, `python3 state/snapshot.py lag --backend fs`. Snapshot thiếu hoặc B không alive: dừng cutover, báo IC. Không chạy make clean.

| # | Bước | Lệnh copy-paste | Tín hiệu hoàn thành / điều kiện dừng | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `for i in 1 2 3; do curl --max-time 3 -sS -i localhost:8001/readyz; sleep 1; done` | A timeout/503 ba lần; `tail -n 5 reports/health-events.jsonl` có A UNHEALTHY; B /healthz 200. Primary hồi phục thì dừng | on-call |
| 2 | Mở incident, xác nhận xử lý | `python3 dr/runbook.py --primary a --target b --backend fs` | Log incident; IC chấp thuận thì nhập y, mặc định N. Lệnh thực hiện cả restore/scale/ready/cutover một lần | on-call + IC |
| 3 | Verify restore state | `sed -n '2p' reports/failover-events.jsonl` | 2_restore_snapshot ok=true, có RPO/docs_lost/embed_model_version. Không gọi restore/failover lại | Data owner |
| 4 | Verify scale và readiness | `curl --max-time 3 -sS -i localhost:8002/readyz` | HTTP 200, ready=true, pool full, vectors>0. Timeout/503: không đổi DNS | Platform engineer |
| 5 | Verify DNS/LB cutover | `curl --max-time 3 -sS localhost:8080/edge/state` | active_region=b sau TTL; 5_dns_cutover sau readiness và alert | on-call |
| 6 | Verify golden signals | `sed -n '6p' reports/runbook-run.jsonl` | 10 requests thật vào B, error_rate=0; đánh giá thủ công p95<250ms. `curl --max-time 3 -sS localhost:8080/v1/infer` phải phục vụ từ B | on-call |
| 7 | Đo RTO, đóng incident và postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Traffic đã kết thúc; valid=true, warnings=[], PASS, recovery=B, RPO có số; giữ raw evidence | on-call + IC |

**Automation:** verify target → restore → scale → wait ready → cutover. Bước 3–6 trong bảng đọc kết quả một lần chạy bước 2. Exit khác 0 hoặc event ok=false: giữ log, điều tra, không sửa pointer tay. --auto chỉ cho drill/CI. P95<250ms là ngưỡng đánh giá thủ công local, không phải SLO được chứng minh hay gate tự động CLI; 10 samples chỉ smoke check.

**Rollback/failback:** trước cutover, restore/readiness lỗi thì giữ pointer A và escalate; việc này không tự sửa outage A. Sau cutover, B lỗi liên tục hoặc golden signals không đạt thì IC xem xét failback. Chỉ IC quyết định sau khi Data owner đối soát write, chọn nguồn canonical và xác nhận state/model A tương thích; A /readyz phải 200 ba lần cách nhau 5s. A chưa an toàn thì không trả traffic về A, không flap tự động.

Với netblock mock, `python3 chaos/kill_region.py restore --region a --backend bare` chỉ SIGCONT, không failback. Với stop/SIGKILL phải khởi động lại process; không chạy up_bare.sh lên cả stack khi B/edge còn giữ cổng. Sau đối soát/phê duyệt, nếu B là nguồn canonical: `python3 state/snapshot.py put --region b --backend fs`, rồi `python3 dr/failover.py --target a --backend fs`. Không chạy hai lệnh đó khi write riêng A chưa được xử lý vì restore ghi đè A. Xác nhận edge/traffic phục hồi từ A trước đóng incident.

**Drill hiện tại:** RTO 34.3s; RPO 10.01s/5 documents; B 215 vectors và weights; p95 trực tiếp B 2.159ms, 0/10 lỗi. Nguồn: `reports/measure-drill-2.json`, `reports/failover-events.jsonl`, `reports/runbook-run.jsonl`. Giữ hai loadgen logs, health/replication/chaos logs cùng ba báo cáo khi nộp bài.
