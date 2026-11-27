# Hướng dẫn kiểm kê và gỡ cài đặt (`cleanup` / `uninstall`)

Lệnh `cleanup` (bí danh `uninstall`) hỗ trợ kiểm kê và gỡ cài đặt các thành phần DB Engine (RPM/systemd trên RHEL / Rocky / AlmaLinux) theo hai chế độ:

- **Cục bộ (mặc định, không SSH)**: chỉ tác động lên máy local; không bắt buộc có file YAML (`-c` là tùy chọn để đọc đường dẫn storage và nhận diện cụm HA).
- **Toàn cụm HA (`--cluster -c <cluster-ha.yml>`)**: chạy trên node điều phối (`local: true`) và điều phối kiểm kê/gỡ đồng bộ trên toàn bộ các node trong inventory qua local bash + native SSH theo thứ tự an toàn cho cụm HA.

---

## 1. Gỡ cài đặt cục bộ (Standalone hoặc 1 node đơn lẻ)

```bash
# Chỉ đọc: liệt kê RPM, service, binary, cấu hình, dữ liệu và backup hiện có
.venv/bin/python src/main.py cleanup --check --sudo

# Mở menu tương tác, chọn component và xem trước kế hoạch; không thực hiện thay đổi
.venv/bin/python src/main.py cleanup --interactive --dry-run --sudo

# Gỡ thật: chọn chế độ, chọn component và nhập đúng chuỗi xác nhận hostname
.venv/bin/python src/main.py cleanup --interactive --sudo

# Kèm file YAML cũ để nhận diện đường dẫn storage tùy chỉnh
.venv/bin/python src/main.py cleanup -c cluster.yml --interactive --sudo
```

> Khác với các lệnh triển khai (`apply`/`enforce`), `cleanup --dry-run` vẫn chạy các lệnh kiểm kê chỉ đọc để lập kế hoạch thực tế, nhưng không thực thi lệnh dừng dịch vụ hay xóa file.

---

## 2. Gỡ cài đặt toàn cụm HA (`--cluster`)

Chạy trên node điều phối (`local: true`) có sẵn kết nối SSH tới các node còn lại (cùng cấu hình `ssh` trong YAML hoặc ghi đè bằng `-u/-p/-k/--sudo`). Cờ `--cluster` **bắt buộc truyền rõ `-c <cluster-ha.yml>`** (không tự dùng file mẫu).

```bash
# Kiểm kê chỉ đọc trên toàn bộ các node trong cụm HA (không thay đổi hệ thống)
.venv/bin/python src/main.py uninstall -c cluster-ha.yml --cluster --check --sudo

# Mở menu tương tác, xem trước thứ tự tắt cụm & kế hoạch gỡ từng node (không thực thi)
.venv/bin/python src/main.py uninstall -c cluster-ha.yml --cluster --interactive --dry-run --sudo

# Gỡ toàn bộ cụm HA (tắt dịch vụ có thứ tự, gỡ RPM và tùy chọn purge/xóa data)
.venv/bin/python src/main.py uninstall -c cluster-ha.yml --cluster --interactive --sudo

# Gỡ một tập con node trong cụm HA (chỉ dùng sau khi đã tách các node đó khỏi cụm)
.venv/bin/python src/main.py uninstall -c cluster-ha.yml --cluster -l dbe-node-02 --decommissioned --sudo
```

### Thứ tự gỡ cụm HA an toàn (4 pha)

Để tránh kích hoạt failover bất thường, hỏng quorum giữa chừng hoặc để lại trạng thái nửa vời khi một node gặp xung đột dependency RPM, `--cluster` thực thi chặt chẽ theo 4 pha:

1. **Pha 0 — Pre-flight trên TẤT CẢ các node (chưa thay đổi gì)**:
   - Chạy `rpm -e --test` và kiểm tra toàn bộ đường dẫn (`validate_path`, symlink cha, mount point thực tế trên `/proc/self/mountinfo`) tại từng node.
   - Nếu **bất kỳ node nào** có lỗi dependency RPM, đường dẫn bị chặn, binary không do RPM quản lý hoặc kiểm kê chưa xác minh (`UNVERIFIED`), toàn bộ quá trình dừng ngay — không có dịch vụ nào bị tắt.
2. **Pha 1 — Tắt & vô hiệu hóa dịch vụ theo tầng trên toàn cụm**:
   - **Tầng truy cập / backup / giám sát**: dừng `keepalived`, `haproxy`, `pgbouncer`, `pgbackrest` (kèm xóa `/etc/cron.d/pgbackrest`), `pmm-agent` trên mọi node được chọn.
   - **Tầng cơ sở dữ liệu**: dừng `patroni` và `postgresql` theo thứ tự **node không chạy DB → các Replica → Primary cuối cùng** (ưu tiên node remote trước node điều phối local) nhằm tránh failover storm.
   - **Tầng DCS**: sau khi `patroni` đã dừng trên mọi node, mới dừng & disable `etcd` trên từng node (node remote trước, node local sau cùng).
3. **Pha 2 — Kiểm tra process, gỡ RPM và xóa đường dẫn theo từng node**:
   - Xác minh không còn process mục tiêu chạy → `rpm -e` (không dùng `--nodeps`) → kiểm tra lại không có process tự khởi động lại → xóa config/data/backup theo kế hoạch bằng `--one-file-system` → `systemctl daemon-reload`.
4. **Pha 3 — Hậu kiểm trên TẤT CẢ các node**:
   - Xác minh RPM đã biến mất, unit đã dừng, không còn process/binary/đường dẫn trong kế hoạch trên mọi node.

### Quy tắc xác nhận và bảo vệ cho cụm HA

- **Gỡ toàn cụm** (`--cluster` không kèm `-l`): yêu cầu gõ chính xác `UNINSTALL CLUSTER <cluster.name>` (chế độ 2) hoặc `PURGE CLUSTER <cluster.name>` (chế độ 3).
- **Gỡ một phần cụm** (`--cluster -l <node>`): nếu tập component có `patroni`/`postgresql`/`etcd`, bắt buộc truyền thêm `--decommissioned` (xác nhận đã tách node đó khỏi cụm HA trước) và gõ chính xác `UNINSTALL SUBSET <cluster.name>` hoặc `PURGE SUBSET <cluster.name>`.
- Các node remote chỉ cần `python3` chuẩn của hệ điều hành (không cần cài `.venv` hay `PyYAML` trên node remote).

---

## 3. Chế độ menu và quy tắc xác nhận (áp dụng cả Local & Cluster)

Menu tương tác cung cấp 3 chế độ:

1. **Chỉ kiểm tra** *(mặc định)*: in báo cáo kiểm kê rồi kết thúc.
2. **Gỡ phần mềm**: dừng/disable service và gỡ đúng danh sách RPM đã hiển thị; **giữ nguyên** dữ liệu, WAL và backup. Các file cấu hình do package quản lý tuân theo cơ chế xóa / `.rpmsave` của RPM.
3. **Purge**: gỡ phần mềm và xóa các file cấu hình/log nằm trong kế hoạch. Câu hỏi xóa dữ liệu (`PGDATA`, WAL, tablespace, etcd data) và câu hỏi xóa backup local là **hai lựa chọn opt-in tách biệt**, đều mặc định **Không**.

Quy tắc chọn component và xác nhận:

- Danh sách component hỗ trợ: **Patroni, PostgreSQL** (các phiên bản RPM phát hiện được), **etcd, PgBouncer, HAProxy, Keepalived, pgBackRest, PMM**. Với Keepalived, tool gỡ RPM `keepalived` và dọn `/etc/keepalived`; Keepalived thuộc tầng truy cập nên được dừng cùng nhóm với HAProxy ở Pha 1.
- Nhập số thứ tự phân cách bằng dấu phẩy hoặc `all` để chọn toàn bộ component có dấu vết được phát hiện.
- Nếu Patroni còn hiện diện, bắt buộc chọn Patroni cùng với PostgreSQL/etcd.
- Trước khi thực thi, tool liệt kê rõ từng RPM, service, đường dẫn và yêu cầu nhập chính xác `UNINSTALL <hostname>` hoặc `PURGE <hostname>`. Nhấn Enter hoặc nhập sai sẽ hủy thao tác; **không có cờ `--yes`** để bỏ qua bước xác nhận.

---

## 4. Kiểm tra dependency và thứ tự thực thi

- Trước khi dừng service, tool chạy `rpm -e --test` để kiểm tra dependency. Nếu gói khác trên hệ thống vẫn phụ thuộc vào RPM được chọn, tool sẽ dừng lại để người vận hành xử lý — **không dùng `--nodeps`** và không tự gỡ thêm gói ngoài kế hoạch.
- Cron của pgBackRest được gỡ bỏ cùng bước gỡ pgBackRest.
- Service và process bắt buộc dừng trước khi gỡ package hoặc xóa dữ liệu. Nếu xảy ra lỗi ở bất kỳ bước nào, tool dừng ngay (có thể để lại trạng thái đã gỡ một phần).
- Sau khi thực thi, tool xác minh lại trạng thái RPM, service/process, binary và các đường dẫn đã lập kế hoạch. Tham khảo thêm: [RPM Dependencies](https://rpm.org/docs/latest/manual/more_dependencies.html).

---

## 5. Xử lý một node HA đơn lẻ hoặc dịch vụ đã offline (chế độ cục bộ)

Ở chế độ cục bộ (không dùng `--cluster`), tool đọc trạng thái membership etcd và Patroni REST API trước khi cho phép gỡ. Khi phát hiện cấu hình HA, nhiều member đang tồn tại hoặc không xác minh được trạng thái cụm, tool sẽ **chặn gỡ** các thành phần cốt lõi (`PostgreSQL`, `Patroni`, `etcd`). Ở chế độ `--dry-run`, kế hoạch và lý do chặn vẫn được hiển thị.

- Muốn gỡ **cả cụm HA** qua SSH từ node điều phối: dùng `--cluster -c cluster-ha.yml` (xem mục 2).
- Muốn gỡ **riêng 1 node đã tách khỏi cụm** ngay trên máy đó, **chỉ chạy sau khi đã tách node khỏi cụm đang vận hành hoặc đã ngừng sử dụng toàn cụm**:

```bash
.venv/bin/python src/main.py cleanup --interactive --sudo --decommissioned
```

Lưu ý quan trọng:

- `--decommissioned` là xác nhận trách nhiệm từ người vận hành, không phải cơ chế tool tự kiểm chứng trạng thái cụm.
- Cờ này **không bỏ qua** bước kiểm tra dependency RPM, kiểm tra đường dẫn/process hay bước nhập chuỗi xác nhận cuối cùng.
- Phiên bản hiện tại không tự `switchover`, không gỡ member etcd khỏi cụm đang chạy và không xóa namespace Patroni/DCS khi chỉ gỡ 1 node lẻ. Xóa dữ liệu etcd cục bộ không làm mất thông tin membership trên các node còn lại.

---

## 6. Đường dẫn tùy chỉnh và giới hạn an toàn

Tool tự dò đường dẫn từ YAML (nếu truyền `-c`), `/etc/patroni/patroni.yml` (hoặc `.yaml`), `/etc/etcd/etcd.yaml`, cấu hình pgBackRest và các symlink WAL/tablespace; nếu thiếu sẽ dùng đường dẫn mặc định của VDE. Khi file cấu hình cũ đã bị mất, có thể khai báo thủ công:

```bash
.venv/bin/python src/main.py cleanup --interactive --sudo \
  --data-dir /srv/postgres/16/data \
  --wal-dir /srv/postgres/16/wal \
  --etcd-data-dir /srv/etcd/default.etcd \
  --backup-dir /srv/backup
```

Các rào chắn an toàn khi xóa đường dẫn:

- `--backup-dir` là thư mục gốc backup: tool chỉ đưa thư mục con `pgbackrest` vào kế hoạch, không xóa toàn bộ thư mục cha. Kho backup local tìm thấy trong cấu hình pgBackRest cũng được liệt kê; kho remote/S3 được giữ nguyên.
- Với tablespace, tool chỉ đưa thư mục phiên bản của cluster hiện tại vào kế hoạch, giữ lại thư mục gốc tablespace và dữ liệu của phiên bản khác.
- Từ chối các đường dẫn cha nguy hiểm (`/data`, `/data/postgres`, `/data/backup`, `/var/lib`, `/etc`), đường dẫn chứa `..`, symlink ở thư mục cha, mount point hoặc thư mục chứa mount con. Cần `umount` hoặc di chuyển dữ liệu trước khi purge mount point.
- Từ chối xóa các đường dẫn đang chứa dữ liệu hoặc backup mà người vận hành đã chọn giữ lại.

Giới hạn phạm vi kiểm kê:

- Không tự gỡ binary cài tay/pip/venv, container, user/group hệ thống, kho RPM repository, cấu hình OS tuning hoặc backup remote.
- Các binary nằm trong `PATH` hoặc `/usr/pgsql-*/bin/postgres` không thuộc danh sách RPM tương ứng và các container liên quan (như PMM Server container/volume) sẽ được báo để xử lý riêng; nếu không xác minh được kiểm kê, tool sẽ chặn thực thi.
- Kiểm kê container phụ thuộc vào quyền runtime của user chạy tool (không bảo đảm phát hiện container rootless của user khác).
- Báo cáo thành công chỉ xác nhận hoàn tất **đúng kế hoạch đã hiển thị**, không cam kết quét sạch mọi tệp tin tùy chỉnh trên toàn đĩa.
