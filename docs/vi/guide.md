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
| `build.run TASK [--fix]` | worktree riêng của task, chỉ đường dẫn của nó | hợp đồng đã review và đã đỏ (job lớn: đã duyệt); `--fix` một lần, cộng một lần cho mỗi `grant-fix` |
| `build.run integration --fix` | worktree tích hợp, chỉ đường dẫn của các task | nhánh tích hợp hỏng kiểm hoặc review; một lần, cộng một lần cho mỗi `grant-fix` |
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
- Worker của một task chỉ bị chấm bằng các lệnh kiểm của chính task đó. Test của các task khác trong plan có thể fail trong worktree của nó cho tới khi tích hợp; test nó muốn thêm ngoài đường dẫn của mình thì ghi vào phần tóm tắt, không làm task dừng.
- Khi các task đã tích hợp, chỉ nhánh tích hợp đi tiếp; không build, kiểm hay review riêng một task được nữa.
- Test đòi hỏi hành vi plan không nêu là finding important.

## Chạy và chạy tiếp

`qk-harness run JOB` (cũng là `resume`) chạy từng bước tiếp theo tới cổng, bước bị chặn, chỗ cần người quyết hoặc kết thúc; hai lượt task chạy cùng lúc, mỗi lượt một tiến trình `qk agent run`. Trước khi chạy, nó gỡ trạng thái "đang chạy" của task mà tiến trình đã mất. Nó không build đè lên commit mà job không ghi nhận, và `pr.open` dùng lại pull request đã có.

Lượt bị dừng vì giới hạn hạn mức của runtime (qk báo bằng trường `limit` của `qk agent run`) không làm job thất bại và không tốn vòng sửa: job ghi `limits` và bước tiếp theo thành `wait: the <runtime> usage limit until HH:MM, then …`. Task dùng runtime khác vẫn chạy tiếp. `qk-harness run --until HH:MM` ngủ tới hai phút sau giờ mở lại (30 phút nếu runtime không nêu giờ), báo (macOS), thử lại rồi chạy tiếp, tối đa ba lần chờ và không quá hạn chót; `--wait-limits` chờ mà không có hạn chót, `--no-wait-limits` tắt việc chờ. Không có các cờ này, `run` dừng với `stopped: limited`, và chạy lại sau sẽ thử lại. `status` hiện các giới hạn, và bản ghi trong sổ mang `limited`, nên `qk brief` hiện `waiting for <runtime> limit until HH:MM`.

`approve JOB STAGE` ghi quyết định duyệt của người. `grant-fix JOB TARGET [--reason TEXT]` cho một task (trước khi tích hợp) hoặc `integration` thêm một vòng sửa khi vòng của nó đã dùng mà vẫn fail check hoặc còn finding nghiêm trọng đều chỉ rõ file và dòng; trường hợp khác bị từ chối, và log của job giữ quyết định cùng lý do. `abort JOB --reason TEXT` dừng hẳn job và liệt kê các nhánh, worktree còn lại để bạn xem hoặc xoá.

`qk-harness status` không kèm job liệt kê các job chưa hoàn tất; dùng `--line` để hiện mỗi job trên một dòng. Runtime adapter có thể đọc các dòng này bằng cách thêm `qk-harness status --line` vào `~/.config/qk/status-commands`. Các dòng trông như sau; nhiều job không chạy được gộp thành một dòng:

```text
harness aaea46 · review.run eval · 1/2 tasks
harness aaea46 · waits: approve plan
harness aaea46 · claude limit until 23:50
harness aaea46 · task api stopped mid-step · qk-harness run
harness 7 jobs not running (qk-harness status)
```

Các lệnh hằng ngày:

```bash
qk-harness status --line                     # mọi job chưa xong, mỗi job một dòng
qk-harness run JOB --until 07:00             # qua đêm: chờ hạn mức mở lại, nhưng không chờ quá 07:00
qk-harness grant-fix JOB api --reason "các finding đáng cho thêm một lượt"
qk-harness run JOB                           # vòng được cấp sẽ chạy: build.run JOB api --fix
qk-harness abort JOB --reason "thay bằng một plan nhỏ hơn"
```

Job đang chờ người thì `next` nói rõ, ví dụ `a person decides: task api has serious review findings that a fix round cannot take`. Chạy `qk-harness status JOB` để xem `findings`: các finding review nghiêm trọng dẫn tới quyết định đó, của hợp đồng (`target` là `contract`), của một task, hoặc của `integration`. Mỗi mục có `target`, `severity`, `file`, `line` và `claim`; vị trí thiếu hiện là null. Danh sách có thể rỗng khi quyết định cần thiết vì lý do khác. Tuỳ trường hợp, cấp thêm một vòng cho target đủ điều kiện hoặc huỷ job.

Khi job dừng (xong, bị chặn, bị huỷ, hoặc chờ người quyết; không tính chờ ở cổng duyệt), nó được ghi vào sổ bằng chứng của qk bằng `qk log run --producer "qk-harness <phiên bản>"`, nên hiện trong `qk show`, `qk stats` và `qk brief`. Card bị chặn ghi qk-harness là công cụ chạy tiếp nó.

## Đo

`qk-harness eval CASE [--impl SPEC] [--review SPEC] [--no-contract] [--no-task-review] [--repeat N]` chạy một bài eval xây dựng của qk: `qk eval prepare`, một job medium mà cổng plan do eval duyệt (ghi rõ như vậy), không mở pull request, rồi `qk eval grade` trên nhánh tích hợp. Bộ chấm đọc báo cáo bốn dòng của chính harness. `result.json` giữ các tiêu chí, token và thời gian của từng lượt, và baseline của bài. Lệnh này chạy agent thật và tốn tiền. Kết quả tới nay: [số đo](measurements.md).

Ví dụ, `qk-harness eval csv-export --repeat 3` chạy bài ba lần và báo `pass_share`, tỷ lệ lượt đạt mọi tiêu chí (`0.666…` khi hai trong ba lượt đạt).

Dùng `--repeat N` (số nguyên dương, mặc định 1) để chạy bài N lần liên tiếp. Mỗi lần gọi có thư mục riêng trong `evals/` của thư mục trạng thái, gồm các thư mục lượt chạy được đánh số chứa `result.json` và một `summary.json` bên cạnh. Bản tổng hợp ghi thư mục, tiêu chí, tổng token và thời gian của từng lượt; `pass_share` là tỷ lệ lượt đạt tất cả tiêu chí. Mỗi lần lặp lại chạy agent thật và làm tăng chi phí.

Bản tổng hợp cũng ghi trạng thái dừng, lý do, bước tiếp theo, kết quả đạt và baseline của từng lượt. Khi chỉ chạy một lượt, stdout còn chứa đầy đủ kết quả lượt chạy, gồm chi tiết tiêu chí trượt và tổng số liệu. Lệnh thoát với mã 0 khi mọi lượt dừng ở `waiting` hoặc `done`, kể cả khi chấm trượt; các trạng thái dừng khác hoặc lỗi cho mã 2.

## Giới hạn

- Agent thật đã được đo trên hai bài eval một task. Một job thật có hai task (Codex build, Claude review) đã đi tới pull request; job đó cần sửa prompt build và một vòng sửa do người cấp thêm. Cùng việc hai task đó, chạy song song và tuần tự không có hợp đồng, đều tới được `pr.open` ([số đo](measurements.md)).
- Job Large duy nhất từng chạy đã dừng trước cổng hợp đồng. Nó qua sáu lượt review hợp đồng; lượt nào cũng nhận các điểm cũ đã sửa xong nhưng lại tìm ra một test mới "chặt hơn plan", lượt cuối do chính câu chữ hai nghĩa trong plan. Chưa có giới hạn số lần review lại sau khi người sửa hợp đồng, và người duyệt ở cổng chưa bỏ qua được finding chỉ thuộc loại "chặt hơn plan"; bản sau cần một trong hai cách. Giờ `status JOB` đã liệt kê các finding đó.
- Mỗi coordinator chạy một job; không có daemon hay hàng đợi.
- Harness không merge, phát hành, gắn tag, xoá nhánh hay force-push, và không có capability nào làm được những việc đó.
- Runtime: `codex`, `claude` và `omp`, theo mức qk hỗ trợ.
