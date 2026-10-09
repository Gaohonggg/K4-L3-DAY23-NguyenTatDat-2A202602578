# RTO/RPO Evidence — Lab 23

Ngày diễn tập: 09/10/2026. Hai region local, bare mode, backend `fs`, chaos `netblock --mock`. RTO/RPO mục tiêu 300s. Epoch là nguồn tính toán; ISO của chaos dùng UTC, không phải giờ Việt Nam.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T04:48:13Z; epoch 1791521293.999682 | Kill A; B còn alive, không forced double outage | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | +0.381693s ≈ 0.4s | 1791521294.381375 − t_outage; HTTP 503, ReadTimeout | `reports/drill-1-nodr.jsonl:40` |
| Requests lỗi | 11 trong tổng 50 requests | Dòng 40–50 đều lỗi | `reports/measure-drill-1.json:28`, `reports/drill-1-nodr.jsonl:50` |
| Request thành công sau lỗi | Không có trong cửa sổ đo | Không tìm thấy recovery | `reports/measure-drill-1.json:9` |
| RTO | NO_RECOVERY | Không có timestamp phục hồi | `reports/measure-drill-1.json:25` |
| Khôi phục sau drill | SIGCONT cho A | Nằm sau cửa sổ traffic baseline | `chaos/chaos-events.jsonl:2` |

Warnings thiếu health alert và DNS cutover ở baseline phù hợp vì chưa chạy DR; drill 2 phải có warnings rỗng.

## 2. Drill 2 — có DR

Traffic có request đầu ở epoch 1791521852.84547 và cuối ở 1791521952.534966; kill nằm trong cửa sổ này. Evidence: `reports/drill-2-withdr.jsonl:1`, `reports/drill-2-withdr.jsonl:148`, `chaos/chaos-events.jsonl:3`.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 0.000000s | epoch 1791521864.8723462; kill A, B còn alive | `chaos/chaos-events.jsonl:3` |
| Request lỗi đầu tiên | 0.061834s ≈ 0.1s | Request HTTP 503/ReadTimeout bắt đầu | `reports/drill-2-withdr.jsonl:25` |
| Checker phát hiện A | 19.974415s ≈ 20.0s | UNHEALTHY sau 4 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Xác nhận outage xong | 22.261992s | Ba probe primary lỗi, target alive, xác nhận lại sau alert | `reports/runbook-run.jsonl:1` |
| Mở incident | 22.263733s | ts incident − t_outage; auto trong drill | `reports/runbook-run.jsonl:2` |
| Verify target xong | 22.274541s | B warm, count=0, weights=false | `reports/failover-events.jsonl:1` |
| Snapshot restore xong | 22.278517s | Đo RPO, giữ embedding version | `reports/failover-events.jsonl:2` |
| Scale pool xong | 22.279671s | pool_state=full | `reports/failover-events.jsonl:3` |
| B ready | 28.483253s | /readyz 200; 215 vectors và weights | `reports/failover-events.jsonl:4` |
| DNS cutover | 28.484414s ≈ 28.5s | Sau readiness và sau detection | `reports/failover-events.jsonl:5` |
| **Request đầu tiên thành công từ B** | **34.278322s ≈ 34.3s** | 1791521899.1506681 − t_outage; HTTP 200, served_by=b | `reports/drill-2-withdr.jsonl:42` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO — Inference API | 34.3s | 300s | PASS; còn 265.7s dư địa | `reports/measure-drill-2.json:20` |
| RPO — Vector DB tại restore | 10.01s / 5 documents | 300s | PASS về thời gian; bản restore thiếu 5 documents | `reports/failover-events.jsonl:2` |
| Drill hợp lệ | valid=true, warnings=[], recovery=b | Không double outage; phục hồi bằng region khác | PASS | `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4`, `reports/measure-drill-2.json:6` |
| Request lỗi sau outage | 17 / 148 requests toàn drill | Thống kê tác động | Quan sát thực tế | `reports/measure-drill-2.json:25`, `reports/drill-2-withdr.jsonl:148` |
| Golden signals trực tiếp B | 10 requests, 0 lỗi, p95=2.159ms | Smoke check sau cutover | PASS về phục vụ; mẫu nhỏ | `reports/runbook-run.jsonl:6` |

Công cụ chấm dùng timestamp **bắt đầu** request. Request thành công có latency 13.8ms. Request lỗi đầu bắt đầu ở +0.061834s nhưng response timeout được nhận sau thêm 2008.1ms. Không diễn giải +0.1s là thời điểm người dùng đã nhận lỗi.

### RPO từ dữ liệu

Primary latest doc: 1791521885.816263; restored latest doc: 1791521875.803315. Hiệu 10.012948s được helper làm tròn thành **10.01s**. `docs_lost=5` đếm hàng primary có ingested_at sau mốc latest của bản restore. Evidence: `reports/failover-events.jsonl:2`.

Snapshot được dùng put ở epoch 1791521877.754587, chu kỳ 30s, version `embed-model=vi-e5-base@v3`: `reports/replication.jsonl:2`. RPO không phải tuổi snapshot. Ingest/replication là process riêng nên vẫn chạy khi serving A bị SIGSTOP; đây là RPO **tại restore**, không phải chứng minh mất dữ liệu vĩnh viễn khi cả region biến mất. Manifest cuối được replication cập nhật tiếp nên không đại diện snapshot dùng trong cutover này.

## 3. Breakdown RTO

Bốn thành phần bắt buộc và các overhead được tách riêng. Hiệu timestamp tại ranh giới event tạo các khoảng không đếm trùng.

| Thành phần | Giây | Cách tính và evidence | Hướng giảm |
|---|---|---|---|
| Health-check detect floor | 15.000000s | interval 5s × threshold 3; `reports/health-events.jsonl:2` | Giảm interval sau khi đo false alerts |
| Detection vượt floor | 4.974415s | t_detect − t_outage − 15; `reports/health-events.jsonl:2`, `chaos/chaos-events.jsonl:3` | Đánh giá lịch probe, timeout, observation window |
| Điều phối sau alert và verify | 2.300126s | t_verify − t_detect; `reports/failover-events.jsonl:1`, `reports/health-events.jsonl:2` | Giảm probe lặp; giữ xác nhận operator |
| Snapshot restore và log | 0.003976s | t_restore − t_verify; `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:1` | Incremental snapshot khi dữ liệu lớn |
| Ghi pool state và log | 0.001154s | t_scale − t_restore; `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:2` | Giữ atomic write |
| GPU pool warm-up / wait ready | 6.203582s | t_ready − t_scale; `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:3` | Full standby đổi chi phí lấy thời gian |
| Ghi DNS pointer và log | 0.001161s | t_cutover − t_ready; `reports/failover-events.jsonl:5`, `reports/failover-events.jsonl:4` | Không bỏ readiness gate |
| DNS/LB TTL và request sampling | 5.793908s | t_recovered − t_cutover; `reports/drill-2-withdr.jsonl:42`, `reports/failover-events.jsonl:5` | Hạ TTL và đo lại end-to-end |
| **Tổng** | **34.278322s ≈ 34.3s** | t_recovered − t_outage; `reports/measure-drill-2.json:20` | Đo lại sau thay đổi |

Restore duration riêng là 0.003615s; waited_s riêng là 6.203469s. Sai khác nhỏ với ranh giới event là log/điều phối. Khoảng restore→scale là ghi pool, không phải copy snapshot. TTL cấu hình 5s không làm phần hậu cutover luôn đúng 5s vì còn timeout và lịch request.

Checker yêu cầu đủ lỗi liên tiếp **và** observation window ít nhất interval × threshold từ probe lỗi đầu. Vì thế threshold 3 không có nghĩa luôn transition ở lỗi thứ ba; log ghi 4. Floor này là policy của bài, không phải giới hạn vật lý chung của mọi health checker.

## 4. Tái kiểm tra và bảo quản

```bash
python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
python3 -m pytest tests/ dr/tests/ -v
```

Full-suite đã được người vận hành chạy ngày 09/10/2026: **23 passed in 0.40s**, gồm 13 tests của repo và 10 safety tests bổ sung. Output terminal được giữ nguyên trong `reports/validation.log:32`.

Nộp ba báo cáo, hai loadgen logs, health/failover/replication/runbook logs, chaos log, hai JSON kết quả đo và validation log. Output debug khác trong logs giữ local. Không chạy make clean trước khi bảo quản evidence.
