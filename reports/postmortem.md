# Postmortem — DR Drill Lab 23

Ngày 09/10/2026; bare mode, filesystem snapshots, netblock mock. Phạm vi mô phỏng local, không có khách hàng thật. Sau outage A, phục hồi qua B với **RTO 34.3s; RPO tại restore 10.01s / 5 documents**. Evidence: `reports/measure-drill-2.json:20`, `reports/failover-events.jsonl:2`.

## 1. Timeline

ISO dưới đây dùng UTC (Z); giờ Việt Nam cộng 7 giờ. Epoch giữ phần lẻ; ISO chaos gốc chỉ chính xác tới giây.

| ISO time UTC | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T04:57:27.750579Z | Snapshot đầu tiên có trước khi attack | `reports/replication.jsonl:1` |
| 2026-10-09T04:57:44Z | Outage A bắt đầu; epoch 1791521864.8723462, B còn alive | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T04:57:44.934180Z | Request đầu bị ảnh hưởng bắt đầu; response lỗi sau 2008.1ms | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T04:57:57.754587Z | Snapshot dùng để restore; replication độc lập vẫn chạy sau SIGSTOP serving A | `reports/replication.jsonl:2` |
| 2026-10-09T04:58:04.846761Z | Checker báo A UNHEALTHY sau 4 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T04:58:07.134338Z | Runbook hoàn tất xác nhận outage | `reports/runbook-run.jsonl:1` |
| 2026-10-09T04:58:07.136079Z | Mở incident, thời điểm thông báo trong mô phỏng | `reports/runbook-run.jsonl:2` |
| 2026-10-09T04:58:07.136582Z | Điểm xác nhận trước failover: auto trong drill, không phải operator nhấn y | `reports/runbook-run.jsonl:3` |
| 2026-10-09T04:58:07.150863Z | Restore xong, đo thiếu 5 documents | `reports/failover-events.jsonl:2` |
| 2026-10-09T04:58:13.355599Z | B ready: pool full, weights và 215 vectors | `reports/failover-events.jsonl:4` |
| 2026-10-09T04:58:13.356760Z | DNS pointer đổi sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T04:58:13.374818Z | 10 requests trực tiếp B: 0 lỗi, p95=2.159ms | `reports/runbook-run.jsonl:6` |
| 2026-10-09T04:58:19.150668Z | Request đầu thành công qua edge từ B bắt đầu; mốc resolved của công cụ đo | `reports/drill-2-withdr.jsonl:42` |
| 2026-10-09T05:02:03Z | SIGCONT cho A sau drill, không phải failback | `chaos/chaos-events.jsonl:4` |

Notification delay = 22.263733s, khác t_outage và alert ở +19.974415s. Runbook elapsed=28.444877s có điểm bắt đầu riêng, không thay thế user-observed RTO. Evidence: `reports/runbook-run.jsonl:7`.

## 2. RTO/RPO và gap analysis

| Chỉ số | Mục tiêu | Đo được | Gap = đo được − mục tiêu | Kết luận |
|---|---|---|---|---|
| RTO | 300s | 34.3s | −265.7s | PASS, còn 265.7s dư địa |
| RPO tại restore | 300s | 10.01s, thiếu 5 documents | −289.99s | PASS về thời gian, không đồng nghĩa không mất dữ liệu |

Evidence: `reports/measure-drill-2.json:20`, `reports/measure-drill-2.json:23`, `reports/measure-drill-2.json:24`. valid=true, warnings=[]: `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4`.

**Thành phần chậm nhất là detection:** 19.974415s, khoảng 58.3% RTO chưa làm tròn. Floor 15s; phần còn lại gồm pha polling/timeout theo policy observation window. Sau alert, xác nhận lại và verify target mất 2.300126s; warm-up 6.203582s; hậu cutover 5.793908s. Restore local khoảng 4ms nên chưa là nút thắt. Breakdown tổng 34.278322s được trình bày trong `reports/rto-evidence.md`.

Tác động: 17 requests lỗi sau outage trong 148 requests toàn drill; baseline trước DR có 11 lỗi trong 50 requests và NO_RECOVERY. Hai cửa sổ traffic khác nhau nên không suy ra availability dài hạn từ các tỷ lệ này. Evidence: `reports/measure-drill-2.json:25`, `reports/drill-2-withdr.jsonl:148`, `reports/measure-drill-1.json:28`, `reports/drill-1-nodr.jsonl:50`.

## 3. Root cause — 5 whys

1. Vì sao request lỗi? Edge định tuyến tới A không trả lời; request kết thúc bằng ReadTimeout. Evidence: `reports/drill-2-withdr.jsonl:25`.
2. Vì sao B không phục vụ ngay? Standby warm, thiếu weights/vectors trước restore; liveness không chứng minh inference readiness. Evidence: `reports/failover-events.jsonl:1`.
3. Vì sao không đổi DNS ngay? Phải restore state/version, scale pool và đợi ready; chuyển sớm sẽ gửi người dùng tới target cũng lỗi. Evidence: `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:4`.
4. Vì sao có độ trễ trước restore? Chống flapping yêu cầu lỗi liên tiếp và observation window, rồi runbook xác nhận lại primary. Evidence: `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:1`.
5. Vì sao còn gap dữ liệu? Ingest độc lập với replication định kỳ; bản restore chưa chứa mọi write mới, chưa có đồng bộ write giữa region. Evidence: `reports/replication.jsonl:2`, `reports/failover-events.jsonl:2`.

Nguyên nhân hệ thống là standby chưa đủ khả năng phục vụ, detection có độ trễ và replication bất đồng bộ. Chaos kích hoạt điều kiện này; không quy lỗi cá nhân.

Nếu cả region mất vĩnh viễn, phép đo RPO hiện tại có giới hạn: helper phải đọc DB primary trên filesystem còn tồn tại. Cần write audit/sequence checkpoint bền vững ngoài failure domain để đối chiếu khi primary không truy cập được. Filesystem snapshot local cũng chưa có isolation giữa region. PASS một drill local chưa giải quyết những hạn chế đó.

## 4. Action items

Tác động dưới đây là giả thuyết cần đo lại, không phải kết quả đã thực nghiệm. Deadline theo ngày Việt Nam.

| # | Action item | Owner | Deadline | Tác động dự kiến / nghiệm thu |
|---|---|---|---|---|
| 1 | So sánh interval 1s và 5s, giữ threshold=3; đo false alerts | SRE/on-call | 2026-10-12 | Floor giảm 12s; chưa cam kết RTO giảm đúng 12s; giữ chống flapping |
| 2 | So sánh full standby và warm standby | Platform engineer | 2026-10-13 | Có thể giảm khoảng warm-up 6.2s; ghi chi phí standby |
| 3 | Thử replication 10s và write checkpoint ngoài primary | Data engineer | 2026-10-14 | Giảm lag kỳ vọng; đo lại RPO/doc loss kể cả primary không đọc được |
| 4 | Thử TTL 1s, đo recovery qua edge | SRE | 2026-10-15 | TTL cấu hình giảm 4s; hiệu quả còn phụ thuộc timeout/sampling |
| 5 | Đối soát write và phê duyệt failback trong vận hành thật | IC + Data owner | 2026-10-16 | Không trả A chỉ vì process sống; tránh mất write khi chọn nguồn canonical |

## 5. Câu hỏi bắt buộc và reflection

**Detection floor và tỷ trọng?** 5s × 3 = 15s, khoảng 43.7% RTO báo cáo 34.3s. Detection thực tế 19.974415s. Observation window làm transition ở lỗi thứ tư trong drill. Muốn RTO 300s thì `interval × 3 + ngân_sách_các_bước_còn_lại ≤ 300s`; interval 100s chỉ là trần lý thuyết khi các bước khác bằng 0, không phải cấu hình vận hành phù hợp.

**Hạ interval xuống 1s?** Floor giảm từ 15s xuống 3s, tức 12s. Probe theo lịch tăng khoảng 5 lần; timeout 2s dài hơn interval có thể làm bỏ tick. Cửa sổ chống flapping ngắn hơn nên dễ phản ứng với lỗi thoáng qua. Không công bố RTO mới trước khi đo lại.

**Outage 6 giờ và primary mất dữ liệu vĩnh viễn?** Trong drill, 5 documents là phần B thiếu tại restore, A vẫn giữ chúng. Nếu A mất hoàn toàn và không có nguồn khác, chúng có thể mất vĩnh viễn hoặc cần khách hàng gửi lại. Không nhân 5 theo 6 giờ để đoán số mất; cần biết write được chấp nhận, replication cuối và nơi write tiếp theo được ghi.

**Giảm thành phần nào ít tăng flapping?** Giữ standby full giảm warm-up mà không đổi health threshold, đổi lại chi phí tài nguyên. Hạ TTL không đổi tiêu chí alert nhưng tăng resolve và cần đo end-to-end.

**Ai báo outage khi serving chết?** Checker là process độc lập, không import serving; runbook đọc health log và probe HTTP. Vận hành thật cần monitor ngoài failure domain nếu checker có thể chết cùng serving.

**Bằng chứng RTO 5 phút ở đâu?** `reports/measure-drill-2.json:20`, t_outage ở `chaos/chaos-events.jsonl:3`, request recovery ở `reports/drill-2-withdr.jsonl:42`. Golden signals trực tiếp B không chứng minh edge đã phục hồi.

## 6. Validation

13 unit/safety tests đã pass trước drill. Drill 2 PASS từ log thật. Sau khi điền báo cáo, người vận hành chạy `python3 -m pytest tests/ dr/tests/ -v`: **23 tests passed in 0.40s**, bao gồm toàn bộ evidence gates và safety tests bổ sung. Output được giữ nguyên trong `reports/validation.log:32`; output debug khác giữ local trong logs.

Phạm vi nộp bài là các yêu cầu bắt buộc trong RUBRIC.md. Không triển khai stretch goals vì rubric không quy định điểm cộng riêng.
