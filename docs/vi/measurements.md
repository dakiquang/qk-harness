# Số đo

[English](../en/measurements.md) · [Hướng dẫn](guide.md)

Mỗi phần tuỳ chọn của harness phải cho thấy nó thay đổi điều gì. Đây là các lần chạy thật bằng `qk-harness eval`, Codex `gpt-6.1-terra` làm và Claude (`claude:`) review, trên qk v0.9.0, ngày 23/11/2026. Mỗi ô là **một lần chạy**, nên một khác biệt đơn lẻ có thể chỉ là dao động.

## Bài đo

Chỉ hai bài eval của qk là bài xây và đi qua được harness: `csv-export` (thêm module và test) và `pagination-empty-page` (tìm và sửa lỗi, thêm test hồi quy). Cả hai chỉ có một task nên chưa cho thấy giá trị của nhiều task song song. Baseline: một lượt skill của qk, lấy từ `evals/baseline.json`: 6/6 ở cả hai bài, 88 đến 372 nghìn token, 35 đến 41 giây.

## Có và không có hợp đồng

| Bài | Có hợp đồng | Không hợp đồng (`--no-contract`) |
|---|---|---|
| `csv-export` | 6/6; 425.812 token; 197 s; 0,28 USD | 6/6; 281.562 token; 130 s; 0,18 USD |
| `pagination-empty-page` | 6/6; 406.222 token; 152 s; 0,24 USD | 5/6; 291.408 token; 109 s; 0,16 USD |

Cột USD chỉ là phần của Claude; Codex không báo chi phí.

- Không có hợp đồng, worker viết một test đòi hỏi hành vi đề không nêu (marker rỗng nghĩa là "còn trang"); bản sửa đúng làm test hỏng, và cả review task lẫn review tích hợp đều không bắt. Có hợp đồng, câu dặn "chỉ test điều plan yêu cầu" và review hợp đồng ngăn được lỗi này.
- Bước hợp đồng tốn thêm khoảng 45 đến 50% token và 40 đến 50% thời gian.
- Quyết định: giữ hợp đồng bật mặc định. So sánh này lẫn hai yếu tố (bước hợp đồng và câu dặn chỉ nằm trong prompt hợp đồng) và mỗi ô chỉ một lần chạy.

## Các lần chạy trước đã định hình thiết kế

- Trước khi hợp đồng có vòng sửa và câu dặn "chỉ test điều plan yêu cầu", `csv-export` dừng hai lần ở review hợp đồng với finding có thật, còn `pagination-empty-page` đạt 5/6 vì một test hợp đồng đề không yêu cầu.
- Một lần chạy `csv-export` không hợp đồng dừng ở bước plan: bên plan hiểu "mỗi task tự viết test" là phải tạo file trong lượt chỉ đọc. Chạy lại thì đạt; coi là dao động.
- Một lượt bị dừng bằng SIGTERM được `qk agent run` kết thúc gọn: không còn tiến trình agent hay thư mục tạm.

## Chưa đo

- Bật và tắt review từng task (`--no-task-review`): đã có cờ, chưa chạy thật.
- Song song so với tuần tự: cần việc có nhiều task.
- Độ khắt khe của reviewer trên ba bài `runner-review` của qk.
