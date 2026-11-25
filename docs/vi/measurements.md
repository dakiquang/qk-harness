# Số đo

[English](../en/measurements.md) · [Hướng dẫn](guide.md)

Mỗi phần tuỳ chọn của harness phải cho thấy nó thay đổi điều gì. Đây là các lần chạy thật bằng `qk-harness eval`, Codex `gpt-6.1-terra` làm và Claude (`claude:`) review, trên qk v0.9.0 ngày 23/11/2026 (hợp đồng) và qk v0.10.0 ngày 24/11/2026 (review từng task, task song song). Mỗi ô là **một lần chạy**, nên một khác biệt đơn lẻ có thể chỉ là dao động.

## Bài đo

Chỉ hai bài eval của qk là bài xây và đi qua được harness: `csv-export` (thêm module và test) và `pagination-empty-page` (tìm và sửa lỗi, thêm test hồi quy). Cả hai chỉ có một task, nên task song song được đo trên một việc thật có hai task (bên dưới). Baseline: một lượt skill của qk, lấy từ `evals/baseline.json`: 6/6 ở cả hai bài, 88 đến 372 nghìn token, 35 đến 41 giây.

## Có và không có hợp đồng

| Bài | Có hợp đồng | Không hợp đồng (`--no-contract`) |
|---|---|---|
| `csv-export` | 6/6; 425.812 token; 197 s; 0,28 USD | 6/6; 281.562 token; 130 s; 0,18 USD |
| `pagination-empty-page` | 6/6; 406.222 token; 152 s; 0,24 USD | 5/6; 291.408 token; 109 s; 0,16 USD |

Cột USD chỉ là phần của Claude; Codex không báo chi phí.

- Không có hợp đồng, worker viết một test đòi hỏi hành vi đề không nêu (marker rỗng nghĩa là "còn trang"); bản sửa đúng làm test hỏng, và cả review task lẫn review tích hợp đều không bắt. Có hợp đồng, câu dặn "chỉ test điều plan yêu cầu" và review hợp đồng ngăn được lỗi này.
- Bước hợp đồng tốn thêm khoảng 45 đến 50% token và 40 đến 50% thời gian.
- Quyết định: giữ hợp đồng bật mặc định. So sánh này lẫn hai yếu tố (bước hợp đồng và câu dặn chỉ nằm trong prompt hợp đồng) và mỗi ô chỉ một lần chạy.

## Có và không có review từng task

Cả bốn lượt đều bật hợp đồng.

| Bài | Có review từng task | Không (`--no-task-review`) |
|---|---|---|
| `csv-export` | 6/6; 392.368 token; 188 s; 0,29 USD | 6/6; 332.137 token; 178 s; 0,19 USD |
| `pagination-empty-page` | 6/6; 373.729 token; 160 s; 0,24 USD | 6/6; 334.396 token; 136 s; 0,16 USD |

- Không có review từng task, cả hai bài vẫn đạt 6/6, ít hơn 11 đến 15% token, 5 đến 15% thời gian và khoảng một phần ba chi phí Claude. Review tích hợp vẫn chạy.
- Mặc định không đổi: review từng task vẫn bật. Hai bài này chỉ có một task, nên review mà cờ này bỏ đi gần như trùng hẳn với review tích hợp; với việc có nhiều task, đó là lần review duy nhất của mỗi task trước khi tích hợp.

## Task song song và tuần tự

Cùng một việc có hai task (ba sửa nhỏ trong `harness/evaluate.py`; finding của review trong `status JOB`, kèm docs), chạy thành hai job Medium trên bản clone tạm của repo này, không có bước hợp đồng, tới `pr.open`.

| | Song song (mặc định) | Tuần tự (`--sequential`) |
|---|---|---|
| Cả job | 653 s | 766 s |
| Giai đoạn task (build và review) | 300 s | 411 s |
| Token | 1.424.166 | 1.437.947 |
| Chi phí Claude | 0,50 USD | 0,45 USD |
| Bộ test sau tích hợp | 117 test OK | 117 test OK |
| Finding của review | không có | 3 nit |

- Chạy song song tiết kiệm đúng thời gian của task ngắn hơn (khoảng 110 s, 15% thời gian job) với cùng số token và cùng kết quả. Khoảng 245 s của mỗi job là bộ test đầy đủ trên nhánh tích hợp, phần này song song không rút ngắn được.
- Nhánh của job song song được merge thành [#21](https://github.com/dakiquang/qk-harness/pull/21), sau khi review chéo tìm ra hai loại quyết định có nguyên nhân khác mà `status` vẫn liệt kê finding, và finding của hợp đồng mà nó bỏ sót.

## Các lần chạy trước đã định hình thiết kế

- Trước khi hợp đồng có vòng sửa và câu dặn "chỉ test điều plan yêu cầu", `csv-export` dừng hai lần ở review hợp đồng với finding có thật, còn `pagination-empty-page` đạt 5/6 vì một test hợp đồng đề không yêu cầu.
- Một lần chạy `csv-export` không hợp đồng dừng ở bước plan: bên plan hiểu "mỗi task tự viết test" là phải tạo file trong lượt chỉ đọc. Chạy lại thì đạt; coi là dao động.
- Một lượt bị dừng bằng SIGTERM được `qk agent run` kết thúc gọn: không còn tiến trình agent hay thư mục tạm.

## Chưa đo

- Một job Large qua đủ bốn cổng: lần thử duy nhất dừng trước cổng hợp đồng (xem mục Giới hạn trong hướng dẫn).
- Review từng task trên việc có nhiều task.
- Độ khắt khe của reviewer trên ba bài `runner-review` của qk.
