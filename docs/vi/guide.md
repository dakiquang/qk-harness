# Hướng dẫn qk-harness

[English](../en/guide.md) · [README](../../README.vi.md) · [Số đo](measurements.md)

## Ý tưởng

Phần bền của một agent harness là các cam kết: ai được làm gì, điều gì phải đúng trước mỗi bước, và mỗi bước để lại bằng chứng gì. Thứ tự các bước là phần lỗi thời nhanh nhất. Vì vậy qk-harness có hai lớp:

- **Tám capability**, mỗi cái là một lệnh tự kiểm điều kiện tiên quyết từ trạng thái job, làm một bước, ghi lại, và trả `status`, `reason`, `next`.
- **Một coordinator mỏng** (`run`) chỉ đi theo `next`. Gọi tay một capability cũng bị kiểm và từ chối y như vậy.

Ba vai tách riêng: agent làm và kiểm, harness chạy git, người quyết ở các cổng và merge.

## Một job

`qk-harness start --repo PATH --task TEXT` tạo một job; chưa chạy gì. Tuỳ chọn:

| Tuỳ chọn | Nghĩa |
|---|---|
| `--size medium` (mặc định) | Một cổng: plan |
| `--size large` | Cổng sau plan, hợp đồng, build và tích hợp |
| `--base BRANCH` | Mặc định: nhánh hiện tại của repo |
| `--impl SPEC`, `--review SPEC` | `runtime:model` làm và review; mặc định `codex:gpt-6.1-terra` và `claude:`. Phải khác họ model |
| `--no-contract` | Bỏ bước hợp đồng; mỗi task tự viết test kiểm nó |
| `--no-task-review` | Chỉ review nhánh tích hợp |
| `--sequential` | Từng task một (mặc định hai task cùng lúc) |

Trạng thái job nằm ở `$QK_HARNESS_STATE_DIR`, không có thì `$XDG_STATE_HOME/qk-harness`, không có nữa thì `~/.local/state/qk-harness`: mỗi job một thư mục có `state.json`, plan, diff, các lượt agent và mọi worktree. Harness không ghi gì vào repo của bạn ngoài các nhánh `qk/<slug>`, `qk/<slug>--<task>`, việc đăng ký worktree của chúng, và upstream của `qk/<slug>` khi `pr.open` push nó.

## Plan

`plan.write` yêu cầu bên làm theo skill `qk-plan` của qk ở chế độ chỉ đọc và trả về plan có hai khối:

- ```` ```qk-verify ````: lệnh shell chứng minh toàn bộ kết quả. Plan thiếu khối này bị từ chối: harness chỉ chạy việc kiểm được.
- ```` ```qk-harness ````: `{"contract": ["tests/..."], "tasks": [{"name", "owns", "verify", "impl"?, "review"?}]}`. Mỗi task sở hữu đường dẫn không task nào khác sở hữu và khai lệnh kiểm riêng nó. Không có bước hợp đồng thì không có khoá `contract` và mỗi task tự sở hữu test của mình.

Chồng đường dẫn và họ model của các vai được kiểm ngay khi viết plan, trước mọi worker. Lệnh duyệt ghi lại hash của plan; sửa plan sau khi duyệt thì mọi bước sau bị từ chối.

## Các capability

| Capability | Được đổi gì | Điều kiện chính |
|---|---|---|
| `plan.write` | không gì | checkout sạch; plan chưa duyệt |
| `contract.write [--fix]` | worktree tích hợp, chỉ trong đường dẫn hợp đồng | plan đã duyệt; `--fix` một lần sau review hợp đồng thất bại |
| `contract.review` | không gì | có hợp đồng; reviewer khác họ |
| `build.run TASK [--fix]` | worktree riêng của task, chỉ đường dẫn của nó | hợp đồng đã review và đã đỏ (job lớn: đã duyệt); `--fix` một lần |
| `build.run integration --fix` | worktree tích hợp, chỉ đường dẫn của các task | nhánh tích hợp hỏng kiểm hoặc review; một lần |
| `verify.run TARGET [--expect-red]` | không gì; mỗi lúc chỉ một lần kiểm trên mọi job | task đã build, hoặc nhánh tích hợp |
| `review.run TARGET` | không gì | đích đã qua kiểm; reviewer khác họ mọi người làm mà nó kiểm |
| `integrate.run` | nhánh local `qk/<slug>` | mọi task đã build, kiểm và review; không task nào đang chạy |
| `pr.open` | remote: push `qk/<slug>`, mở một pull request | nhánh tích hợp đã kiểm và review; cổng cuối đã duyệt; tài khoản `gh` đang dùng push được |

Các luật do code ép, không do prompt:

- Worker không bao giờ chạy git. Worker tự commit, đổi nhánh hay sửa đường dẫn không thuộc mình thì bị từ chối và thay đổi không được commit.
- Agent chỉ thấy worktree của mình và một thư mục chung chứa plan và diff, không bao giờ thấy worktree khác.
- Hợp đồng phải đỏ trước mọi build (`--expect-red`); hợp đồng đã xanh sẵn thì job dừng.
- Tích hợp gộp đúng các commit đã kiểm và review; nhánh bị đổi sau đó thì job dừng.
- Vòng sửa chỉ nhận lần kiểm hỏng và finding có file và dòng. Còn lại chờ người.
- Test đòi hỏi hành vi plan không nêu là finding important.

## Chạy và chạy tiếp

`qk-harness run JOB` (cũng là `resume`) chạy từng bước tiếp theo tới cổng, bước bị chặn, chỗ cần người quyết hoặc kết thúc; hai lượt task chạy cùng lúc, mỗi lượt một tiến trình `qk agent run`. Trước khi chạy, nó gỡ trạng thái "đang chạy" của task mà tiến trình đã mất. Nó không build đè lên commit mà job không ghi nhận, và `pr.open` dùng lại pull request đã có.

`approve JOB STAGE` ghi quyết định duyệt của người. `abort JOB --reason TEXT` dừng hẳn job và liệt kê các nhánh, worktree còn lại để bạn xem hoặc xoá.

Khi job dừng (xong, bị chặn, bị huỷ, hoặc chờ người quyết; không tính chờ ở cổng duyệt), nó được ghi vào sổ bằng chứng của qk bằng `qk log run --producer "qk-harness <phiên bản>"`, nên hiện trong `qk show`, `qk stats` và `qk brief`. Card bị chặn ghi qk-harness là công cụ chạy tiếp nó.

## Đo

`qk-harness eval CASE [--impl SPEC] [--review SPEC] [--no-contract]` chạy một bài eval xây dựng của qk: `qk eval prepare`, một job medium mà cổng plan do eval duyệt (ghi rõ như vậy), không mở pull request, rồi `qk eval grade` trên nhánh tích hợp. Bộ chấm đọc báo cáo bốn dòng của chính harness. `result.json` giữ các tiêu chí, token và thời gian của từng lượt, và baseline của bài. Lệnh này chạy agent thật và tốn tiền. Kết quả tới nay: [số đo](measurements.md).

## Giới hạn

- Agent thật mới được đo trên bài một task; việc nhiều task mới chạy với qk giả.
- Mỗi coordinator chạy một job; không có daemon hay hàng đợi.
- Harness không merge, phát hành, gắn tag, xoá nhánh hay force-push, và không có capability nào làm được những việc đó.
- Runtime: `codex`, `claude` và `omp`, theo mức qk hỗ trợ.
