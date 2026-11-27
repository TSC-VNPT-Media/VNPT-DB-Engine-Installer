# Đặc tả Cấu hình Manifest Cluster YAML (`vde-install`)

Tài liệu tham chiếu đầy đủ các trường trong file cấu hình YAML (SSOT) của `vde-install`, đối chiếu trực tiếp với `src/core/inventory.py`, `src/core/pmm.py`, `src/core/state_enforcer.py`, `src/core/platform_check.py` và `src/main.py`.

> **Lưu ý bảo trì:** Bất kỳ thay đổi nào liên quan đến schema YAML, quy tắc `topology`, giá trị mặc định, `settings` hoặc các trường bắt buộc/tùy chọn đều phải cập nhật đồng bộ vào tài liệu này.

---

## 1. Nhóm 1: Định danh cụm & kho gói (`cluster`)

| stt | thành phần | bắt buộc | mục đích | giá trị cần điền |
|---|---|---|---|---|
| 1 | `cluster.name` | **Có** | Tên cụm & scope của Patroni | Chuỗi duy nhất, ví dụ `"dbe-core-prod"` |
| 2 | `cluster.profile` | **Có** | Chọn mô hình triển khai | `"standalone"` (1 node) \| `"ha"` (≥ 3 nodes) |
| 3 | `cluster.version` | Không | Phiên bản PostgreSQL | Mặc định `"16"` |
| 4 | `cluster.package_source` | Không | Nguồn kho gói RPM | `"vnpt"` *(mặc định)* \| `"internal"` \| `"percona"` |
| 5 | `cluster.release_rpm_url` / `repo_url` / `gpg_key_url` | Không | URL repo nội bộ & GPG key (dùng khi `package_source: vnpt/internal`) | Mặc định trỏ `dbengine-repo.ttpmhcm.com` |
| 6 | `cluster.package_bridge` | Không | Node `local: true` (node đang chạy tool, có internet) làm cầu HTTP cho `dnf`/`curl` của các node còn lại | `false` *(mặc định)* \| `true` |
| 7 | `cluster.package_bridge_port` | Không | Cổng proxy trên node cài đặt, chỉ dùng khi `package_bridge: true` | `8888` *(mặc định)*. Node remote chỉ cần tới được IP cụm của node cài đặt. Tool không mở firewall |
| 8 | `cluster.safeguard` | Không | Chống xóa nhầm dữ liệu hiện có | `true` *(mặc định)* \| `false` |

---

## 2. Nhóm 2: Hạ tầng máy chủ & kết nối (`topology` & `ssh`)

| stt | thành phần | bắt buộc | mục đích | giá trị cần điền |
|---|---|---|---|---|
| 1 | `topology.hosts` | **Có** | Danh sách các máy chủ tham gia cụm | Mảng đúng 1 host (`standalone`) hoặc ≥ 3 hosts (`ha`) |
| 2 | `topology.hosts[].ip` | **Có** | IPv4 định danh node (`etcd`, `Patroni`, `pg_hba`, `HAProxy`) | IPv4 duy nhất; `ha` cấm `127.0.0.1` / `localhost` / `::1`; `standalone` khuyến nghị IPv4 `scope global` thật |
| 3 | `topology.hosts[].local` | **Có (HA)** | Đánh dấu máy chạy tool trực tiếp (không qua SSH) | `ha`: đúng 1 host đặt `true`, các host khác `false`; `standalone`: mặc định `true` (không được đặt `false`) |
| 4 | `topology.hosts[].name` | Không | Tên node trong cụm, Patroni node name & PMM service name | Chuỗi duy nhất; mặc định tự sinh `dbe-node-01`, `dbe-node-02`... |
| 5 | `topology.hosts[].roles` | Không | Quyết định component cài trên node | Mảng gồm `"db"`, `"etcd"`, `"balancer"`; mặc định `["db", "etcd", "balancer"]`. `standalone` bắt buộc có cả `"db"` và `"etcd"`; `ha` bắt buộc `≥ 1` node `"db"` và tổng số node `"etcd"` là **số lẻ $2n+1 \ge 3$** (3, 5, 7...) |
| 6 | `topology.hosts[].ssh_host` & `node_ip` | Không | Tách IP khi SSH đi qua NAT/port-forward | `ssh_host`: IP SSH tới; `node_ip`: IP interface `scope global` thật trong VM (`standalone` cô lập hoàn toàn dùng `"127.0.0.1"`) |
| 7 | `topology.hosts[].parameters` | Không | Ghi đè GUC riêng cho 1 node (ưu tiên cao nhất lúc bootstrap) | Key-value GUC, ví dụ `{max_connections: 300}` |
| 8 | `ssh` (`user`, `port`, `use_sudo`, `key_file`) | Không | Kết nối SSH tới các node `local: false` (HA; `--sudo` áp dụng cả local) | Mặc định `user: "root"`, `port: 22`, `use_sudo: false`, `key_file`: không chỉ định |

---

## 3. Nhóm 3: Tuning, thư mục, xác thực & đối tượng DB (`settings` cốt lõi)

| stt | thành phần | bắt buộc | mục đích | giá trị cần điền |
|---|---|---|---|---|
| 1 | `settings.auto_tune` & `workload` | Không | Bật/tắt Smart Defaults & chọn template tải để tự tính GUC/sysctl | `auto_tune: true` *(mặc định)* \| `false`; `workload`: `"oltp"` *(mặc định)* \| `"olap"` \| `"crit"` \| `"tiny"` |
| 2 | `settings.storage` | Không | Đường dẫn data, WAL và backup local | Mặc định `/data/postgres/16/data`, `/data/postgres/16/wal`, `/data/backup` |
| 3 | `settings.auth` | Không | Mật khẩu 3 tài khoản hệ thống (`postgres_password`, `replicator_password`, `monitor_password`) | Mặc định `"postgres_password"`, `"replicator_password"`; `monitor_password` mặc định `""` *(nên đổi khi dùng thật; khai báo `monitor_password` để tự tạo user `monitor` quyền `pg_monitor`)* |
| 4 | `settings.parameters` | Không | Ghi đè PostgreSQL GUC cấp toàn cụm (thắng Smart Defaults) | Key-value GUC, ví dụ `shared_buffers: "8GB"`, `work_mem: "64MB"`, `max_connections: 400` *(máy dùng HDD/SAN chậm cần đặt `random_page_cost: 4.0`, `effective_io_concurrency: 2`)* |
| 5 | `settings.sysctl` & `ports` | Không | Ghi đè tham số kernel Linux & cổng dịch vụ | `sysctl`: dict tham số kernel; `ports`: mặc định `5432` (PG), `6432` (PgBouncer), `5433/5434/7000` (HAProxy), `8008` (Patroni), `2379/2380` (etcd) |
| 6 | `settings.hba_rules` | Không | Quy tắc xác thực `pg_hba.conf` đẩy vào Patroni DCS | Mảng rule string hoặc dict `{type, database, user, address, method}`; mặc định mở localhost + IP cụm + `0.0.0.0/0 scram-sha-256` |
| 7 | `settings.users`, `databases`, `extensions` | Không | Tạo/đồng bộ user nghiệp vụ, database và extension | `users`: `[{name, password, superuser, replication, createdb, conn_limit, roles, pgbouncer}]`; `databases`: `[{name, owner, extensions}]`; `extensions`: `["pg_stat_statements", ...]` |

---

## 4. Nhóm 4: Các thành phần dịch vụ (`settings.<component>`)

| stt | thành phần | bắt buộc | mục đích | giá trị cần điền |
|---|---|---|---|---|
| 1 | `settings.etcd` | Không | Lưu trạng thái DCS & bầu leader | `enabled: true` *(mặc định)*, `token: "vde-etcd-cluster"` *(nếu tắt `false`, cụm vẫn phải có `etcd` chạy sẵn trên các node role `etcd`)* |
| 2 | `settings.patroni` | Không | Supervisor PostgreSQL & failover | `enabled: true`, `synchronous_mode` *(HA: `true`, standalone: `false`)*, `synchronous_mode_strict: false`, `ttl: 30`, `loop_wait: 10`, `retry_timeout: 10` |
| 3 | `settings.pgbouncer` | Không | Connection pooler `:6432` | `enabled: true`, `pool_mode: "transaction"`, `max_client_conn: 1000`, `default_pool_size: 50`, `max_prepared_statements: 200` |
| 4 | `settings.haproxy` | Không | Cân bằng tải Read/Write (`:5433` / `:5434`) & stats `:7000` | `enabled: true` *(mặc định)* \| `false` |
| 5 | `settings.keepalived` | Không | Virtual IP (VRRP) phía trước HAProxy trên các node role `balancer`; client kết nối `VIP:5433` / `VIP:5434`. Chuyển VIP khi node đang giữ không còn mở được cổng write của HAProxy | `enabled: false` *(mặc định; khi tắt bỏ qua mọi trường còn lại)*, `virtual_ip` *(bắt buộc khi bật; IPv4, có thể kèm `/prefix`, mặc định lấy prefix của interface chứa IP node)*, `auth_pass` *(bắt buộc khi bật; 1–8 ký tự chữ hoặc số, giống nhau trên mọi balancer)*, `virtual_router_id: 51` *(1–255, duy nhất trong L2 segment)*, `interface` *(tùy chọn; mặc định là thiết bị chứa IP node)*, `priorities` *(tùy chọn; map tên/IP balancer → 1–255)* |
| 6 | `settings.pgbackrest` | Không | Backup & WAL archiving (tự động điều khiển `archive_mode`/`archive_command` theo `stanza`) | `enabled: true`, `stanza: "vde_cluster"`, `destination: "local"` \| `"s3"`, `retention_full: 2`, `retention_diff: 7`. S3/MinIO thêm `s3_endpoint`, `s3_bucket`, `s3_key`, `s3_key_secret`, `s3_region` (mặc định `us-east-1`), `s3_uri_style` (mặc định `path`), `s3_verify_tls` (mặc định `true`). HA nên dùng `s3`: `destination: local` là kho riêng từng node và đứt archive khi failover. `enabled: false` tự đặt `archive_mode: off` |
| 7 | `settings.pmm` | Không* | Giám sát PMM 3 (local container trên standalone hoặc nối server ngoài) | `enabled: false` *(mặc định)*, `mode: "local"` \| `"external"`, `version: 3`, `query_source: "pgstatements"` \| `"none"`, `database_user` *(mặc định `"monitor"` nếu có `auth.monitor_password`, ngược lại `"postgres"`)*, `database_password_env`, `os_reserve_mb: 1024`, `server_install` (`podman`/`docker`, `https_port: 8443`, `memory: "4g"`, `retention: "168h"`) |

### Điều kiện bắt buộc phụ thuộc cấu hình
- Khi `settings.keepalived.enabled: true`: bắt buộc `settings.haproxy.enabled` (mặc định đã bật) và có ít nhất 1 host role `balancer`; `virtual_ip` phải là IPv4 hợp lệ và **không trùng** `ip`/`node_ip` của bất kỳ host nào; `auth_pass` khớp `[A-Za-z0-9]{1,8}` (giới hạn `auth_type PASS` của keepalived); mỗi key trong `priorities` phải là tên hoặc IP (`ip`/`node_ip`/`ssh_host`) của một host role `balancer`. Vi phạm bất kỳ điều kiện nào, `validate` dừng với lỗi `settings.keepalived...`.
- Priority mặc định theo thứ tự balancer trong `topology.hosts`: `150`, `140`, `130`... (tối thiểu `50`). Balancer có priority cao nhất cấu hình `state MASTER`, các node còn lại `BACKUP`; mỗi node nhận danh sách `unicast_peer` là IP cụm của các balancer khác.
- Khi `firewalld` đang active trên HA, preflight yêu cầu giao thức `vrrp` (protocol 112) được cho phép trên các balancer; nếu thiếu, lệnh dừng và gợi ý `firewall-cmd --permanent --add-service=vrrp && firewall-cmd --reload`. Tool không tự sửa firewall.
- Khi `settings.pmm.enabled: true` và `settings.pmm.mode: "external"`, chạy live (`apply`/`enforce`) bắt buộc khai báo `server` (HTTPS), `user` và `password` hoặc `password_env`.
- Khi `settings.pmm.enabled: true` và `settings.pmm.database_user: "monitor"`, chạy live bắt buộc khai báo `settings.auth.monitor_password` hoặc `settings.pmm.database_password_env`.

---

## 5. Quy tắc tính Smart Defaults (`auto_tune: true`)

- **Ngân sách tài nguyên đầu vào:** Lấy `min(RAM)` và `min(vCPU)` của node yếu nhất trong cụm; tính `DB_RAM = Tổng_RAM - RAM_PMM_local (4 GiB nếu bật) - RAM_OS (1 GiB nếu bật PMM local)`.
- **Bộ nhớ:**
  - `oltp` / `olap` / `crit`: `shared_buffers = 25% DB_RAM`, `effective_cache_size = 75% DB_RAM`, `maintenance_work_mem` theo tỷ lệ `DB_RAM` (`256MB–1024MB` cho `oltp`/`crit`; `512MB–2048MB` cho `olap`).
  - `tiny` (hoặc `DB_RAM < 2048 MiB`): `shared_buffers = 15% DB_RAM` (≥ `128MB`), `effective_cache_size = 40% DB_RAM`, `work_mem = 16MB`, `maintenance_work_mem = 256MB`, `max_connections = 100`.
- **Kết nối & phiên:**
  - `oltp` / `crit`: `work_mem = 32MB`, `max_connections = 500`, `max_parallel_workers_per_gather = 2`, `checkpoint_timeout = 15min` (`crit` trên HA ép thêm `full_page_writes = on`, `synchronous_commit = remote_apply`).
  - `olap`: `work_mem = 128MB`, `max_connections = 200`, `max_parallel_workers_per_gather = 4`, `checkpoint_timeout = 30min`.
- **CPU, I/O & kernel:** `max_worker_processes = max(8, vCPU)`, `max_parallel_workers = vCPU`, `random_page_cost = 1.1`, `effective_io_concurrency = 200`, kèm 5 tham số `sysctl` (`vm.overcommit_memory=2`, `vm.overcommit_ratio=80`, `vm.swappiness=1`, `net.core.somaxconn=4096`, `net.ipv4.tcp_max_syn_backlog=4096`).
- **Khi `auto_tune: false`:** Chỉ đặt 5 GUC tối thiểu (`shared_buffers: 128MB`, `max_connections: 100`, `wal_level: replica`, `archive_mode`, `archive_command`), không áp template workload; `effective_config.sysctl` rỗng `{}` (nếu không khai báo `settings.sysctl`, script `os_baseline/run.sh` vẫn fallback ghi 5 dòng sysctl an toàn).
