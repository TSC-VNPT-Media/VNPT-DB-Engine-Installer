# VDE-Install — Công cụ cài đặt & quản trị vòng đời PostgreSQL HA

Bộ công cụ triển khai và quản trị vòng đời **PostgreSQL 16 + Patroni + etcd + PgBouncer + HAProxy + Keepalived (VIP, tùy chọn) + pgBackRest + PMM 3** trên **RHEL / Rocky / AlmaLinux 8 & 9**.

Tool được tải và chạy trực tiếp trên máy cần cài:
- **Standalone**: chạy hoàn toàn cục bộ trên 1 server (không cần SSH).
- **HA**: chạy cục bộ trên 1 node điều phối trong cụm và điều phối các node còn lại qua native SSH (không dùng Ansible).

---

## Nguyên lý kiến trúc

- **Một file YAML duy nhất (SSOT)**: toàn bộ cluster, topology, PostgreSQL GUC, `hba_rules`, `users`, `databases`, bật/tắt component nằm trong một file `vde-cluster.yml`. DAG được dựng động trong `src/core/inventory.py`.
- **Mô hình thống nhất Standalone & HA**: cả `standalone` (1 node) và `ha` (≥ 3 node) đều dùng chung chuỗi `os_baseline -> local_repo -> etcd & postgresql16 -> patroni -> db_ready -> pgbouncer & haproxy & pgbackrest (+ keepalived sau haproxy khi bật VIP) (+ pmm_server -> pmm)`. Patroni là supervisor duy nhất của PostgreSQL (`postgresql-16` systemd luôn bị disable).
- **Một node local duy nhất**: `standalone` mặc định `local: true`; `ha` bắt buộc khai báo đúng 1 host `local: true`. Tool chạy `/bin/bash` trên node đó và native SSH tới các host còn lại (log gắn nhãn `[LOCAL]` / `[SSH]`).
- **Preflight & Smart Defaults**: tự đọc RAM/vCPU/interface, kiểm tra IP node local, firewall/đồng hồ (trên HA) và áp dụng 4 mẫu tải `oltp | olap | crit | tiny`. Cấu hình khai báo tay trong YAML luôn thắng Smart Defaults.
- **Đồng bộ Day-2 & Baseline schema 3**: cập nhật cấu hình qua Patroni DCS (`PATCH :8008/config`, reload, kiểm tra `pending_restart`, rolling restart replica trước primary khi được phép) và lưu hash theo từng component tại `/etc/vde/baseline.json`.
- **Kho gói RPM**: mặc định dùng kho nội bộ VNPT DB Engine (`package_source: vnpt` / `internal`, cài `vnpt-db-engine-release.rpm` kèm khóa GPG `RPM-GPG-KEY-VNPT-DB-ENGINE`); hỗ trợ chuyển sang `package_source: percona` khi dùng kho Percona public. `package_bridge: true` để node đang chạy tool làm cầu HTTP cho `dnf` của các node không ra được internet.

---

## Cấu trúc thư mục

```text
install-tool/
├── .gitignore                        # Virtualenv, cache, log và cấu hình cục bộ
├── requirements.txt                  # Dependency Python của máy điều phối (PyYAML)
├── bin/
│   └── vde-ins                       # Wrapper chạy src/main.py (ưu tiên .venv, rồi python3.12, rồi python3)
├── documents/                        # Tài liệu hướng dẫn, đặc tả YAML & vận hành
├── src/
│   ├── main.py                       # CLI: init | validate | plan | apply | status | enforce | cleanup
│   ├── config/
│   │   ├── vde-cluster.example.yml   # File mẫu SSOT cho cụm HA
│   │   └── standalone.example.yml    # File mẫu cài đặt Standalone (1 server)
│   ├── core/
│   │   ├── inventory.py              # Parser SSOT, 4 mẫu tuning, unified DAG builder
│   │   ├── engine.py                 # DAG engine (Kahn topological sort)
│   │   ├── platform_check.py         # Kiểm tra nền tảng, OS, firewalld, đồng hồ
│   │   ├── ssh_runner.py             # Local shell + native SSH + dry-run
│   │   ├── execution.py              # Thực thi pipeline & xử lý thứ tự db_ready
│   │   ├── state_enforcer.py         # SSOT hash/drift + Patroni DCS live sync
│   │   ├── pmm.py                    # Cấu hình, kiểm tra & ngân sách RAM cho PMM 3
│   │   └── cleanup.py                # Kiểm kê và gỡ cài đặt cục bộ interactive
│   └── components/                   # Các module shell idempotent (mỗi thư mục 1 run.sh)
│       ├── os_baseline/   local_repo/   etcd/         postgresql16/  patroni/
│       └── pgbouncer/     haproxy/      keepalived/   pgbackrest/   pmm_server/    pmm/
├── tests/                            # Unit tests kiểm thử CLI, transport, PMM, cleanup
└── README.md
```

---

## Bắt đầu nhanh

### 1. Thiết lập môi trường Python (trên máy điều phối)

Yêu cầu **Python 3.12**, `free`, `nproc`, `ip` (và OpenSSH client nếu cài HA). Chạy từ thư mục `install-tool/`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export PATH="$PWD/bin:$PATH"
vde-ins --version
```

`vde-ins` là tên ngắn của `bin/vde-ins`. Dòng `export PATH` có hiệu lực trong shell hiện tại. Muốn gọi từ mọi thư mục, symlink một lần: `sudo ln -sfn "$PWD/bin/vde-ins" /usr/local/bin/vde-ins`.

> Xem hướng dẫn xử lý lỗi `venv`/`pip`, chạy không cần activate và chuẩn bị wheel offline tại [Môi trường Python](documents/python-environment.md).

### 2. Cài đặt Standalone (1 node cục bộ)

```bash
# Chạy trực tiếp trên server cần cài (ví dụ IP 10.0.0.11)
vde-ins init -p standalone -n mydb --ips 10.0.0.11 -o cluster.yml
# Đổi mật khẩu mẫu, rà soát storage/parameters trong cluster.yml
vde-ins validate -c cluster.yml
vde-ins plan -c cluster.yml
vde-ins apply -c cluster.yml --dry-run
vde-ins apply -c cluster.yml --sudo      # bỏ --sudo nếu chạy bằng root
vde-ins status -c cluster.yml --sudo
```

### 3. Cài đặt HA (3+ node: 1 node local + các node qua SSH)

```bash
# Chạy trên node điều phối duy nhất (IP đầu tiên tự được gắn local: true)
vde-ins init -p ha -n mydb \
  --ips 10.0.0.11,10.0.0.12,10.0.0.13 -o cluster-ha.yml
# Khai báo ssh.user/port/key_file, đổi mật khẩu và rà soát cấu hình
vde-ins validate -c cluster-ha.yml
vde-ins plan -c cluster-ha.yml --offline
vde-ins plan -c cluster-ha.yml --sudo
vde-ins apply -c cluster-ha.yml --dry-run
vde-ins apply -c cluster-ha.yml --sudo
vde-ins status -c cluster-ha.yml --sudo
```

### 4. Vận hành Day-2, gỡ cài đặt & chạy kiểm thử

```bash
# Đồng bộ cấu hình GUC/HBA (thêm --restart trong cửa sổ bảo trì nếu đổi tham số postmaster)
vde-ins apply -c cluster.yml -t guc,hba --sudo
vde-ins apply -c cluster.yml -t guc --restart --sudo

# Kiểm tra trạng thái: mặc định mỗi component được probe trên 1 node ngẫu nhiên có role tương ứng
vde-ins status -c cluster.yml --sudo
vde-ins status -c cluster.yml --node dbe-node-02 --sudo   # ghim node (tên, IP hoặc ssh_host; lặp cờ hoặc dùng dấu phẩy)

# Phát hiện lệch cấu hình theo baseline và chỉ reconcile component bị lệch
vde-ins enforce -c cluster.yml --sudo

# Kiểm kê và gỡ cài đặt tương tác (local hoặc toàn cụm HA)
vde-ins cleanup --check --sudo
vde-ins cleanup --interactive --sudo
vde-ins uninstall -c cluster-ha.yml --cluster --check --sudo
vde-ins uninstall -c cluster-ha.yml --cluster --interactive --sudo

# Chạy bộ unit test (không cài đặt dịch vụ)
.venv/bin/python -m unittest discover -s tests -v
```

---

## Wrapper `bin/vde-ins`

`bin/vde-ins` chạy `src/main.py` với interpreter theo thứ tự: `.venv/bin/python`, `python3.12`, `python3`. Các lệnh ở trên dùng tên `vde-ins` sau khi `bin` nằm trên `PATH`.

---

## Virtual IP cho HAProxy (Keepalived, tùy chọn)

Mặc định tắt. Khi bật `settings.keepalived`, tool cài Keepalived trên các node role `balancer` để giữ một địa chỉ VIP phía trước HAProxy; client kết nối `VIP:5433` (write) / `VIP:5434` (read). VRRP chuyển VIP khi node đang giữ không còn mở được cổng write của HAProxy.

```yaml
settings:
  haproxy:
    enabled: true
  keepalived:
    enabled: true
    virtual_ip: "10.0.0.20/24"     # địa chỉ chưa dùng trong mạng balancer
    auth_pass: "vdeVip01"          # 1-8 ký tự chữ hoặc số, giống nhau trên mọi balancer
    virtual_router_id: 51          # duy nhất trong L2 segment
```

Cần `settings.haproxy.enabled` và ít nhất một host role `balancer`. Preflight HA yêu cầu firewalld cho phép giao thức `vrrp` trên các balancer (tool chỉ in gợi ý `--add-service=vrrp`, không tự sửa firewall). Chi tiết trường cấu hình: [Đặc tả cấu hình YAML](documents/cluster-config-reference.md).

---

## Giám sát PMM 3

- **Standalone**: `standalone.example.yml` và `init -p standalone` bật sẵn PMM 3 ở `mode: local` (tự cài PMM Server container bằng Podman, sinh mật khẩu root-only tại `/etc/vde/pmm-admin.password`, mở dashboard tại `https://127.0.0.1:8443` qua SSH tunnel và đăng ký PMM Client). Ngân sách RAM tự động trừ mặc định 4 GiB cho PMM Server + 1 GiB cho OS trước khi tính GUC PostgreSQL.
- **HA**: dùng `mode: external` kết nối tới PMM Server độc lập, truyền credential qua biến môi trường trước khi chạy live `apply`.

Xem chi tiết cấu hình, mở tunnel, giới hạn và nghiệm thu tại [Giám sát PMM 3](documents/pmm-standalone.md).

---

## Tài liệu chi tiết

| Tài liệu | Nội dung chính |
|---|---|
| [Môi trường Python](documents/python-environment.md) | Thiết lập `.venv`, xử lý lỗi môi trường, chuẩn bị wheel offline và quy tắc `.gitignore` |
| [Đặc tả cấu hình YAML](documents/cluster-config-reference.md) | Bảng đầy đủ các trường bắt buộc/tùy chọn trong SSOT YAML (gồm `settings.keepalived`) và quy tắc Smart Defaults |
| [Hướng dẫn triển khai & Day-2](documents/runbook-walkthrough.md) | Luồng cài Standalone/HA, preflight, `db_ready`, tag, `baseline.json`, nghiệm thu và giới hạn hiện tại |
| [Kiểm kê & gỡ cài đặt (`cleanup`)](documents/cleanup-guide.md) | Gỡ phần mềm/purge trên máy local hoặc toàn cụm HA (`--cluster`), kiểm tra dependency RPM và rào chắn an toàn |
| [Giám sát PMM 3](documents/pmm-standalone.md) | Cấu hình PMM Server/Client local & external, `db_ready`, ngân sách RAM và phạm vi kiểm chứng |
| [Thiết kế kiến trúc](documents/architecture-and-design.md) | Kiến trúc phân tầng, pipeline 5 giai đoạn và ma trận component giữa Standalone & HA |
