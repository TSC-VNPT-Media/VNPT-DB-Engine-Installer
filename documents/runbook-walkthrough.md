# Vận hành install-tool trên chính node cần cài

Tool chạy trên server standalone hoặc một node thuộc cụm HA. Node local chạy shell trực tiếp; các node khác nhận script qua SSH. Không cần SSH vào chính máy chạy tool, không cần sao chép source/virtualenv tới các node remote.

## 1. Chuẩn bị

- Node local dùng hệ điều hành đích họ EL có systemd/dnf, Python 3.12, free/nproc/ip, đủ storage và RPM repository.
- Cài thật cần root hoặc sudo không tương tác. HA cần thêm SSH client trên local và SSH server/key/user/sudo phù hợp trên remote.
- Cổng SSH GitLab để clone source độc lập với cổng SSH cài các node.
- Validate, plan offline và dry-run có thể chạy từ máy phát triển; cài thật phải chạy trên đúng node local.

### Yêu cầu về IP và mạng

Tool sử dụng IP khai báo trong `topology.hosts[].ip` cho `etcd` (peer/client), `Patroni` (`connect_address`), `pg_hba.conf`, `HAProxy` backends, `PMM` và xác minh chạy đúng máy ở bước preflight (`ip -o -4 addr show scope global`):

- **HA (`profile: ha`, bắt buộc ≥ 3 node và số node `etcd` lẻ $2n+1 \ge 3$ để đạt etcd quorum):**
  - Mỗi node **bắt buộc** có IPv4 `scope global` thông mạng giữa các node (khuyến nghị dùng **LAN/VLAN nội bộ**, không chạy etcd/Patroni qua WAN công cộng); cấm dùng `127.0.0.1`, `localhost`, `::1`.
  - Nếu điều phối SSH đi qua NAT/port-forward (IP SSH khác IP giao tiếp nội bộ trong VM), tách `ssh_host` (IP để SSH tới) và `node_ip` (IP interface `scope global` thật bên trong node).
- **Standalone (`profile: standalone`, 1 node Patroni+etcd):**
  - Khuyến nghị khai báo 1 IPv4 `scope global` thật (LAN) để xác minh đúng server, cho phép kết nối từ ngoài và sẵn sàng mở rộng thành HA về sau.
  - Nếu dùng `127.0.0.1`, preflight vẫn cần suy ra một `node_ip` không rỗng từ interface `scope global` hoặc default route; trường hợp máy cô lập hoàn toàn không có interface `scope global` thì phải khai báo rõ `node_ip: "127.0.0.1"` trong YAML.

Từ thư mục install-tool:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Không dùng sudo pip. Khi dùng --sudo, tool chỉ nâng quyền các lệnh shell cài đặt; Python vẫn dùng virtualenv hiện tại.

## 2. Standalone: cài ngay trên server hiện tại

Ví dụ server có IP thật 10.0.0.11:

```bash
python src/main.py init -p standalone -n mydb --ips 10.0.0.11 -o cluster.yml
# Đổi mật khẩu, kiểm tra storage và parameters trong YAML
python src/main.py validate -c cluster.yml
python src/main.py plan -c cluster.yml
python src/main.py apply -c cluster.yml --dry-run
python src/main.py apply -c cluster.yml --sudo
python src/main.py status -c cluster.yml --sudo
```

Standalone chỉ chấp nhận đúng một host và tự đặt `local: true` nếu thiếu. Không dùng SSH user/key/port cho host này, không cần sshd. Nếu đang chạy root thì bỏ `--sudo`. Mẫu riêng tại `src/config/standalone.example.yml`.

> **Hành vi của `init` (áp dụng cả Standalone & HA):** Nếu file đầu ra đã tồn tại và không truyền `-f`/`--force`, tool hỏi xác nhận ghi đè trên terminal tương tác (hoặc từ chối khi chạy không tương tác). Sau khi ghi YAML, `init` tự validate cú pháp và chạy sanity check nhẹ trên máy local (OS, CPU, RAM, IP; bỏ qua nếu dùng `--offline` hoặc `local_ip` không nằm trên máy hiện tại) — chỉ cảnh báo `[WARN]`, không chặn bước tạo file.

## 3. HA: một node cài local, các node còn lại qua SSH

Ví dụ tool chạy trên node đầu tiên `10.0.0.11` (máy duy nhất giữ mã nguồn điều phối):

```bash
python src/main.py init -p ha -n mydb \
  --ips 10.0.0.11,10.0.0.12,10.0.0.13 -o cluster-ha.yml
```

`init` luôn đánh dấu IP đầu tiên là `local: true` (máy chạy tool duy nhất), các IP còn lại là `local: false`. Khi đọc YAML HA, tool yêu cầu đúng một host `local: true`; thiếu hoặc có nhiều node local sẽ bị từ chối. Có thể sửa marker trong YAML sang node khác nếu chuyển toàn bộ mã nguồn/venv sang chạy trên đúng node đó.

```yaml
topology:
  hosts:
    - ip: "10.0.0.11"
      name: "dbe-node-01"
      local: true
      roles: ["db", "etcd", "balancer"]
    - ip: "10.0.0.12"
      name: "dbe-node-02"
      roles: ["db", "etcd", "balancer"]
    - ip: "10.0.0.13"
      name: "dbe-node-03"
      roles: ["db", "etcd", "balancer"]
ssh:
  user: "ops"
  port: 22
  key_file: "~/.ssh/id_ed25519"
  use_sudo: true
```

ssh.user/port/key_file chỉ dành cho remote. use_sudo hoặc --sudo nâng quyền trên local và remote. Cấu hình SSH không đổi user đang chạy tool trên node local. CLI -u/-p/-k ghi đè YAML. Không cần ssh_host/node_ip khi ip đã là IP interface dùng chung cho SSH và liên lạc nội bộ. ssh_host vẫn hỗ trợ cho remote qua NAT; node_ip khai báo tay phải tồn tại trên interface.

```bash
python src/main.py validate -c cluster-ha.yml
python src/main.py plan -c cluster-ha.yml --offline
python src/main.py plan -c cluster-ha.yml
python src/main.py apply -c cluster-ha.yml --dry-run
# Chỉ cài thật trong lab đã kiểm tra bootstrap/quorum (xem mục 6)
python src/main.py apply -c cluster-ha.yml --sudo
python src/main.py status -c cluster-ha.yml --sudo
```

Preflight kiểm tra IP node local có trên máy đang chạy trước khi discovery remote. plan mặc định đọc phần cứng local/SSH; --offline hoặc --dry-run bỏ discovery. Với apply/enforce, --limit chỉ giới hạn phần thực thi; preflight vẫn đọc toàn bộ inventory.

Trên HA, preflight còn đọc `firewalld` và đồng hồ từng node, nhưng không sửa firewall:

- Nếu `firewalld` đang active mà thiếu cổng theo role (`etcd` 2379/2380, `db` 5432/8008 và 6432 khi bật PgBouncer, `balancer` 5433/5434/7000, hoặc cổng đã ghi đè trong `settings.ports`), lệnh dừng và in sẵn `firewall-cmd --permanent --add-port=... && firewall-cmd --reload` cho từng node.
- Nếu bật `settings.keepalived` và `firewalld` đang active, các node role `balancer` còn phải cho phép giao thức `vrrp` (protocol 112). Thiếu thì lệnh dừng và gợi ý `firewall-cmd --permanent --add-service=vrrp && firewall-cmd --reload`.
- Lệch giờ giữa các node trên 2 giây được cảnh báo; trên 10 giây thì dừng. etcd và Patroni cần `chronyd` trước khi cài.
- Node remote cần OpenSSH client (`ssh`) trên máy đang chạy tool. Thiếu client thì dừng trước discovery.
- `cluster.package_bridge: true` dùng khi chỉ node đang chạy tool ra được kho RPM. Trong `apply`/`enforce`, node đó mở proxy HTTP tạm trên IP cụm, cổng `package_bridge_port` (mặc định `8888`). `dnf` và `curl` của node remote đi qua proxy này; node cài đặt vẫn tải trực tiếp. Proxy chỉ nhận kết nối từ IP trong inventory và tắt khi lệnh kết thúc. Trước khi cài, tool kiểm tra node local tới được URL repo và mỗi node remote mở được cổng proxy. Nếu `firewalld` trên node cài đặt đang active mà thiếu cổng này, preflight dừng và in `firewall-cmd`. Bật hoặc tắt cờ không làm lệch baseline vì đây là đường tải gói, không phải cấu hình dịch vụ. Kéo image container (PMM) không đi qua cầu này.

## 4. Nghiệm thu và Day-2

Log có nhãn `LOCAL`/`SSH` cho mỗi host. Thành phần thực thi theo role: `db` gồm PostgreSQL/Patroni và component DB được bật; `etcd` chạy DCS; `balancer` chạy HAProxy và Keepalived (khi bật `settings.keepalived`). `os_baseline`/`local_repo` chạy trên mọi node được chọn.

**Kho gói RPM**: mặc định sử dụng repository nội bộ VNPT DB Engine (`package_source: vnpt` hoặc `internal`), tự cài gói phát hành `http://dbengine-repo.ttpmhcm.com/vnpt-db-engine-release.rpm` kèm khóa GPG `RPM-GPG-KEY-VNPT-DB-ENGINE` (các gói gồm `postgresql16-*` hoặc `percona-postgresql16-*`, `patroni`, `pgbouncer`, `haproxy`, `keepalived` khi bật VIP, `pgbackrest`, `etcd`, `pmm-client`). Khi muốn dùng trực tiếp kho Percona public, đặt `package_source: percona`.

```bash
python src/main.py apply -c cluster-ha.yml -t guc,hba --sudo
python src/main.py apply -c cluster-ha.yml -t users,pgbouncer -l dbe-node-02 --sudo
# Chỉ cấu hình lại VIP (tag keepalived hoặc vip, cần settings.keepalived.enabled)
python src/main.py apply -c cluster-ha.yml -t keepalived --sudo
# Chỉ trong cửa sổ bảo trì sau khi kiểm tra sức khỏe
python src/main.py apply -c cluster-ha.yml -t guc --restart --sudo
```

### Thứ tự `db_ready`, ý nghĩa tag và partial apply

- **`db_ready`** là action nội bộ của executor chạy sau `patroni` và trước nhóm access/backup (`pgbouncer`, `haproxy`, `keepalived`, `pgbackrest`) và PMM (`pmm_server`, `pmm`): đợi DB sẵn sàng, patch DCS một lần rồi reload từng node (đợi cấu hình vào file và reload ack), kiểm tra `pending_restart`, restart replica trước primary nếu được phép, đồng bộ SQL/extensions và đợi replication extension tới các node đã chọn. Mọi lỗi SQL/DCS/readiness/restart đều dừng pipeline (không nuốt lỗi).
- **Yêu cầu writable primary**: các thao tác sync config/extension yêu cầu có writable primary trong tập DB host được chọn; lệnh `--limit` chỉ trỏ vào replica sẽ yêu cầu bổ sung primary.
- **Hành vi theo tag (`-t`)**:
  - Tag config (`guc`, `hba`, `users`...) đồng bộ qua nhánh `db_ready` + các component tương ứng; `PATCH` DCS luôn có tác dụng cấp cluster dù `--limit` giới hạn host thực thi.
  - `apply -t pmm --restart` tự bổ sung `db_ready` cho DB đã tồn tại, không bootstrap lại OS/etcd/PostgreSQL ngoài tag; `-t pmm_server` cũng chuẩn bị `db_ready` trước nhưng không đăng ký Client; `-t guc,hba --restart` chạy `db_ready`, không chạy PMM.
  - Tag `keepalived` (hoặc `vip`) chỉ chạy trên các node role `balancer` và cũng là tag access-only: tool kiểm tra điều kiện DB sẵn sàng như với `pgbouncer`/`haproxy`. Đổi `virtual_ip`, `auth_pass`, `virtual_router_id`, `interface`, `priorities` hoặc cổng write của HAProxy làm lệch component `keepalived`; Keepalived được restart khi apply.
  - Node chưa có DB sẽ bị chặn trước bước monitoring (cài mới luôn chạy full `apply`). Tag không tồn tại hoặc của component đã tắt bị từ chối kể cả khi đi kèm tag hợp lệ. Điều kiện tiên quyết của các tag access-only được kiểm tra read-only trên các DB host đã chọn.
- **Lưu ý riêng cho HA & pgBackRest**: Replica Patroni mới được clone bằng `pg_basebackup --waldir` tới `wal_dir` và có cửa sổ chờ dài hơn standalone. `pgBackRest` trên HA không bật `backup-standby` khi chỉ có `pg1` cục bộ; cron backup chỉ chạy khi node đang là primary (`destination: local` sẽ phát cảnh báo vì kho cục bộ không đi theo failover).
- **Ảnh hưởng của đổi cấu hình tới baseline**: Partial apply cập nhật hash cho đúng các component đã chạy. Đổi GUC/HBA/user/database chỉ làm `db_ready` lệch; đổi mật khẩu `postgres` làm lệch `patroni`, `db_ready` và `pgbouncer` vì các file cấu hình đó nhúng mật khẩu. Không dùng `systemctl postgresql-16` làm supervisor song song với Patroni.

Cơ chế quản lý file cấu hình PostgreSQL của Patroni (`data_dir = config_dir`, mặc định `/data/postgres/16/data`):
- **Lúc `initdb`**: Patroni đổi tên `postgresql.conf` gốc của PostgreSQL thành `postgresql.base.conf` (giữ các cấu hình nền của `initdb`), rồi tự sinh ra file `postgresql.conf` mới gồm `include_if_exists = 'postgresql.base.conf'` kèm các tham số từ DCS (và mục `postgresql.parameters` trong `/etc/patroni/patroni.yml`).
- **`pg_hba.conf`**: Cũng do Patroni sinh và ghi đè từ `pg_hba` trong DCS mỗi lần reload/reconfigure.
- **Không sửa tay trực tiếp trong `PGDATA`**: Mọi chỉnh sửa thủ công vào `postgresql.conf` hoặc `pg_hba.conf` sẽ bị Patroni ghi đè lại theo DCS. Muốn đổi GUC hoặc HBA ở Day-2, luôn sửa trong file YAML (`settings.parameters` / `settings.hba_rules`) rồi chạy `apply -t guc,hba` (thêm `--restart` nếu đổi tham số cần restart) hoặc `enforce` để tool đẩy cấu hình vào Patroni DCS.

Cơ chế `--restart` và `pending_restart` trong bước `db_ready`:
- **Cài mới lần đầu (Day-1 trên máy sạch)**: Không cần `--restart` vì `patroni` bootstrap PostgreSQL ngay từ đầu bằng bộ GUC trong YAML nên không phát sinh `pending_restart`.
- **Day-2 (hoặc chạy đè lên `PGDATA` cũ)**: Khi thay đổi các tham số loại `postmaster` (như `shared_buffers`, `max_connections`, `max_worker_processes`, `wal_level`, `archive_mode`, `shared_preload_libraries` khi mới bật `pmm`...), PostgreSQL đánh dấu vào `pg_settings.pending_restart` sau khi reload.
- **Bảo vệ chống restart ngoài ý muốn**: Nếu thiếu `--restart` (và thiếu `--force`), tool dừng pipeline và báo lỗi để tránh ngắt kết nối đột ngột; khi truyền `--restart` (hoặc `--force`), tool chạy `rolling_restart` theo thứ tự **Replica trước, Primary sau**.

Phạm vi lệnh `status` và trách nhiệm nghiệm thu của nhân viên vận hành / DBA:
- **`status`** là lệnh chỉ đọc, probe **từng component** (`etcd`, `patroni`, `pgbouncer`, `haproxy`, `keepalived` khi bật, `pgbackrest`, `pmm` khi bật) trên một node có role tương ứng (`etcd`→role `etcd`; `patroni`/`pgbouncer`/`pgbackrest`/`pmm`→role `db`; `haproxy`/`keepalived`→role `balancer`). Mỗi component in banner `probe: <node> execution=LOCAL|SSH mode=random|dedicated eligible=<n>`, trạng thái systemd và các port liên quan (`5432`, `6432`, `5433`, `5434`, `8008`, `2379`, `2380`, `7000`). Standalone không có replica để phục vụ endpoint read `:5434`.
- **Chọn node probe**: mặc định (`--random`) mỗi component chạy trên một node ngẫu nhiên đủ role. `--node HOST` (tên, IP hoặc `ssh_host`; lặp cờ hoặc dùng danh sách phân cách dấu phẩy) ghim node, và component chỉ chạy khi node đó có role tương ứng; `--node` không khớp host nào bị từ chối. `--random` và `--node` loại trừ nhau; `-l/--limit` thu hẹp tập node được chọn.
- **Nội dung probe theo component**: `etcd` in endpoint health toàn cụm, member list và endpoint status (số member, số leader); `patroni` in bảng role/state/lag của mọi member từ `:8008/cluster` (không lặp lại trên từng host); `keepalived` in VIP và cho biết VIP có đang nằm trên node được probe hay không (VIP chỉ nằm trên một balancer tại một thời điểm, nên node khác báo `not on this node` là bình thường); `pgbackrest` chạy `pgbackrest info` bằng user `postgres` và tóm tắt stanza/destination; `pmm` in chế độ và server. Service lỗi hoặc inactive sẽ in thêm journal gần nhất. Service được hỏi theo `roles` và cờ bật/tắt, nên node không chạy etcd không bị báo `etcd=inactive`.
- **Nghiệm thu HA (do nhân viên vận hành / DBA thực hiện)** sau `status`:
  - `etcd`, `patroni`, `pgbouncer`, `haproxy` (và `keepalived`, `pmm-agent` nếu bật) `active` đúng role; `postgresql-16` không phải supervisor.
  - etcd đủ voting member, mọi endpoint healthy, đúng một leader.
  - Patroni có một leader, sync standby khi `synchronous_mode: true`, replica `streaming`. `ls -l ${data_dir}/pg_wal` là symlink tới `wal_dir` trên primary và replica.
  - `SELECT pg_is_in_recovery()` qua HAProxy write trả `false`, qua read trả `true`. Cổng PostgreSQL và PgBouncer nhận SCRAM.
  - Ghi một dòng qua cổng write, đọc lại qua cổng read. `patronictl switchover` trong cửa sổ bảo trì, rồi kiểm tra write đi theo primary mới.
  - Keepalived, nếu bật: VIP nằm trên đúng một balancer (balancer priority cao nhất khi mọi node khỏe). Kết nối `VIP:5433` thấy `pg_is_in_recovery() = false`. Dừng HAProxy hoặc làm đóng cổng write trên node giữ VIP trong cửa sổ bảo trì thì VIP phải chuyển sang balancer khác, và quay lại khi cổng mở lại.
  - `pgbackrest info` trên primary báo `status: ok`. Cron HA chỉ chạy backup khi node đó đang là primary. `destination: local` không được xem là backup chịu failover.
  - PMM external, nếu bật: mỗi node `Connected`, service `<node>-pg16`, `pg_up=1`.
- Tool không tự switchover hay restore. Lưu YAML mode 0600, log apply, `baseline.json` và biên bản nghiệm thu.

## 5. Baseline và đổi node chạy tool

Sau mỗi apply, kể cả khi chọn `-t` hoặc `-l`, tool ghi `/etc/vde/baseline.json` schema 3 (mode 0600, không chứa secret) với hash riêng của từng component đã chạy xong trên host đó. Component không nằm trong lần chạy giữ nguyên hash. `enforce` so từng hash: chỉ component bị sửa mà chưa apply thì hiện `[DRIFT]`. Không truyền `-t` và không `--force` thì enforce chỉ chạy lại các component lệch, và chỉ trên host lệch. Có `-t` hoặc `--force` thì reconcile đúng phạm vi được yêu cầu khi còn lệch hoặc khi có `--force`. File schema 2 chỉ được coi là khớp toàn bộ khi manifest hash vẫn đúng và đủ component; file schema 2 đã lệch cần một lần reconcile (thường là `enforce` không tag) trước khi `-t` tách diff chính xác. Đây không phải lệnh chỉ đọc hoặc live-state audit. `-l` không ghi baseline của host không được chọn; `db_ready` PATCH DCS cho cả cluster nên host không chọn vẫn có thể báo diff của `db_ready`. DB config/restart/SQL/extension/readiness chạy trong db_ready trước PMM; xem documents/pmm-standalone.md.

```bash
python src/main.py enforce -c cluster-ha.yml --sudo
```

Muốn chạy tool từ node khác: tải cùng source, tạo venv, chuyển YAML và SSH access cần thiết, đánh dấu đúng host mới local: true. Không đổi tên/thứ tự thành viên để chuyển node điều phối. Marker local không được đưa vào hash cấu hình dịch vụ; việc chuyển marker đơn thuần không tạo drift. Node điều phối không bị ràng buộc phải giữ vai trò primary PostgreSQL.

## 6. Giới hạn hiện tại

- Preflight hiện chưa kiểm tra loại block device của `storage.data_dir` (chưa đọc `lsblk -d -o NAME,ROTA,TRAN` hay `/sys/block/<dev>/queue/rotational`). Khi `auto_tune: true`, tool luôn gán mặc định `random_page_cost: 1.1` và `effective_io_concurrency: 200` (phù hợp với SSD / NVMe). Nếu server đích dùng HDD hoặc SAN/storage chậm (`rotational=1`), cần tự ghi đè trong YAML dưới `settings.parameters`:
  ```yaml
  settings:
    parameters:
      random_page_cost: 4.0
      effective_io_concurrency: 2
  ```
- Bootstrap etcd HA chia hai pha: mọi member được cấu hình và `start --no-block` trước, rồi mới kiểm tra quorum và đúng một leader. Standalone vẫn một pha. Scale-out chưa có learner gate riêng; đừng coi `apply --force` là quy trình thêm/rút member đã được bảo đảm cho production.
- Một số bước đồng bộ bỏ qua lỗi; log hoàn tất/baseline không thay thế nghiệm thu runtime.
- Source chưa có workflow hoàn chỉnh cho upgrade major, HA → standalone, remove member hoặc restore tự động. Không chỉ sửa profile/version rồi xem như migration hoàn tất.
- SSH trust, REST/DCS auth/TLS và secret xuất hiện trong env/log của phiên chạy cần hardening riêng trước production. Baseline chỉ lưu hash.

Kiểm thử transport mà không cài dịch vụ:

```bash
.venv/bin/python -m unittest discover -s tests -v
```
