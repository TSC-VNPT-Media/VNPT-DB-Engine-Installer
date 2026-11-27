# PMM cho standalone

Standalone example và `init --profile standalone` bật PMM 3 ở `mode: local`: cài PMM Server trên cùng máy, rồi cài/register Client theo thứ tự DAG. HA mặc định tắt PMM; bật HA thì dùng `mode: external` và server riêng. `enabled: false` vẫn được hỗ trợ để chủ động bỏ giám sát.

## Kết nối

`PostgreSQL :5432 -> postgres_exporter/QAN + pmm-agent trên host -> push HTTPS -> PMM Server container 127.0.0.1:8443 -> UI/Grafana + metrics/query stores trong volume /srv`.

- PMM Server image mặc định pin `docker.io/percona/pmm-server:3.10.0`, container `vde-pmm-server`, volume `vde-pmm-data`.
- `runtime: podman`: tự cài Podman từ repo RPM của OS nếu thiếu. Không yêu cầu Docker có sẵn. `runtime: docker` dùng Docker đã được người vận hành cài; installer chỉ enable/start daemon.
- Systemd unit `vde-pmm-server.service` quản lý container và khởi động khi boot. Installer yêu cầu container running, healthcheck healthy nếu được cấu hình, endpoint `/v1/readyz` trả HTTP 200, xác thực admin thành công và service active trước khi tiếp tục. Gate này kiểm tra lúc cài, không thay thế giám sát định kỳ.
- Client cài `pmm-client` từ `pmm3-client`. Không tự nâng PMM 2 lên 3. Không dùng `--force` khi đăng ký lại node. Query source đúng là `pgstatements`.
- Local self-signed TLS được khai báo `insecure_tls: true`; external mặc định xác minh chứng thư. Chỉ mở loopback theo example, không tự sửa firewall.
- Chế độ push áp dụng cả node exporter và PostgreSQL exporter, để server container không phải truy cập ngược vào loopback của host.

## Cấu hình tối thiểu

```yaml
settings:
  pmm:
    enabled: true
    mode: local
    version: 3
    query_source: pgstatements
    insecure_tls: true
    server_install:
      runtime: podman
      image: docker.io/percona/pmm-server:3.10.0
      https_port: 8443
      memory: 4g
      retention: 168h
```

`mode: local` tự suy ra endpoint client `https://127.0.0.1:8443`. Có thể cấu hình `listen_address`, `container_name`, `volume`, `password_file`, `pull_timeout` và `ready_timeout` như standalone example. Co-located PMM tiêu thụ tài nguyên chung với PostgreSQL: giới hạn container mặc định 4 GiB RAM, retention 7 ngày; cần tính RAM/disk thực tế theo tải, cấu hình 7 ngày không cam kết dung lượng tối đa.

Pull image có `server_install.pull_timeout` mặc định 2400 giây (1–7200); chờ server sẵn sàng dùng `ready_timeout` mặc định 300 giây (1–600). Runner có deadline theo từng lệnh/component, mặc định 3600 giây, điều chỉnh qua `--timeout`. Deadline runner bao trùm cả bước PMM Server, nên nếu đặt `--timeout` thấp hơn tổng thời gian pull, khởi động và kiểm tra thì runner có thể dừng trước timeout bên trong. Chọn đủ dư địa theo môi trường; tăng pull_timeout trên 2400 có thể cần tăng cả `--timeout`. Khi timeout, runner dừng nhóm process local; với SSH, việc đóng kết nối không đảm bảo process remote đã dừng, cần kiểm tra trước khi thử lại.

## Tài khoản và truy cập

Mật khẩu admin local tự sinh một lần và lưu `/etc/vde/pmm-admin.password` mode 0600. Không commit secret. Có thể cung cấp `password_env: PMM_SERVER_PASSWORD` từ môi trường máy điều phối; giữ nguyên credential giữa các lần chạy. Đây là mật khẩu admin dùng bootstrap, chưa tự tạo service-account token. Khi khai báo `settings.auth.monitor_password` (hoặc đặt `database_user: monitor` với `database_password_env`), tool tự tạo role `monitor` với quyền `pg_monitor` trong PostgreSQL và mặc định để PMM kết nối bằng user `monitor` (nếu không cấu hình cả hai thì `database_user` mặc định về `postgres`). Để dùng một tài khoản giám sát tên khác, tạo user/quyền `pg_monitor` phù hợp (ví dụ qua `settings.users` với `roles: ["pg_monitor"]`) và đặt `database_user` + `database_password_env` riêng.

Mở tunnel trên Windows rồi truy cập `https://localhost:8443`:

```powershell
ssh -L 8443:127.0.0.1:8443 rep1
```

Đăng nhập `admin`, đọc password trực tiếp trên server bằng tài khoản root (không gửi password vào chat/log). Không restart/reset PMM container để thay password; làm rotation tường minh theo tài liệu Percona. Không xóa volume khi chạy lại; nếu resource trùng tên không thuộc installer, image/config/volume binding đổi hoặc credential bị mất thì dừng để xử lý, không tự takeover/recreate.

External mode chỉ cài Client: khai báo `server: https://pmm.example.com:443`, `user: service_token`, `password_env: PMM_SERVER_TOKEN`. Live preflight từ chối thiếu thông tin trước hardware discovery/provisioning. `validate`/`plan --offline` vẫn cho xem manifest/DAG mà không cần secret; chúng không xác minh kết nối.

## PostgreSQL và giới hạn kiểm chứng

Khi bật `query_source: pgstatements`, inventory bổ sung `pg_stat_statements` vào preload (giữ các library khác), cấu hình tracking và extension. Bootstrap mới sẽ render cấu hình trước khi start PostgreSQL. PMM Client kiểm tra preload/extension thực tế trước khi add service; node đang chạy cần áp cấu hình/restart có kiểm soát trước, không âm thầm báo thành công.

Component kiểm tra node connected và service PostgreSQL có trong PMM Inventory, tránh add trùng service cùng tên/endpoint. Nó chưa tự sửa query-source/credentials của service đã đăng ký trước đó: thay đổi đó cần quản lý PMM inventory tường minh. Theo dõi PostgreSQL + host ở phạm vi này; chưa tự đăng ký riêng PgBouncer, HAProxy, Patroni hay etcd collectors.

Unit tests dùng subprocess/container/HTTP fixtures; không thay thế live acceptance: container readiness + admin auth, client Connected, exporters Running, sample metrics và QAN trên dashboard, rerun giữ node/service/volume, reboot auto-start, outage không báo thành công. Refactor đã chuyển DCS/SQL/restart/readiness vào db_ready trước monitoring. Apply theo tag ghi hash của component đã chạy, không giữ nguyên baseline của các component khác. HA etcd bootstrap dùng hai pha configure rồi verify quorum; kết quả quorum vẫn cần nghiệm thu trên cụm thật.

PMM Server container và named volume là tài nguyên riêng; cleanup hiện có không tự purge chúng. Muốn gỡ phải kiểm kê/chọn rõ container và volume, backup dữ liệu monitoring rồi duyệt thao tác; không xem dữ liệu volume PMM như PGDATA của PostgreSQL được giám sát.

Tài liệu gốc: https://docs.percona.com/percona-monitoring-and-management/3/reference/faq.html ; https://docs.percona.com/percona-monitoring-and-management/3/install-pmm/install-pmm-client/package_manager.html ; https://docs.percona.com/percona-monitoring-and-management/3/use/commands/pmm-admin/config.html ; https://docs.percona.com/percona-monitoring-and-management/3/install-pmm/install-pmm-client/connect-database/postgresql.html


## Refactor DB-ready và dependency

DAG mới: OS/repo -> etcd + PG binaries -> Patroni -> db_ready -> PgBouncer/HAProxy/pgBackRest + PMM Server -> PMM Client. db_ready đợi cấu hình DCS thực sự tới pg_file_settings, gửi pg_reload_conf và đợi pg_conf_load_time tiến lên trước khi đọc pending_restart; sau restart mới sync SQL/extension và đợi extension trên replica. Vì vậy PMM không còn kiểm tra preload trước restart. PMM lỗi vẫn khiến full apply fail, nhưng DB/access/backup đã được chuẩn bị trước monitoring.

Để bật PMM trên DB đã chạy: `apply -t pmm --restart -c cluster.yml`. Đây là partial apply: hash của `db_ready`, `pmm_server` và `pmm` được cập nhật, các component khác giữ hash cũ. Nếu chưa có DB, prerequisite check sẽ fail; không tự cài lại OS/etcd ngoài tag. Cần bao gồm primary trong host selection cho config/extension sync. Không restart nếu không có pending_restart; nếu có pending và không được cho phép restart thì dừng rõ ràng trước monitoring.

Ngân sách RAM: `settings.pmm.os_reserve_mb` mặc định 1024; trừ thêm memory limit PMM trước khi tính GUC. Với 7680 MiB tổng, còn 2560 MiB cho DB và shared_buffers auto là 640MB. Đây không phải giới hạn tổng RAM backend, cần quản lý concurrency/work_mem riêng. Không dùng lại override 4GB từ các manifest init cũ trên node nhỏ.

`server_install.pull_policy`: missing (mặc định) dùng cache, chỉ pull nếu thiếu; never yêu cầu image cache cho offline; always chủ động pull. `package_source: internal` cài pmm-client từ repo đã cấu hình, không tự bật public Percona. Chuẩn bị Podman/package và image mirror/cache trong môi trường offline. PMM repository được cấu hình ở bước Client, không buộc bước repo/PostgreSQL phải phụ thuộc repo PMM.

Full baseline schema 2 chỉ được ghi sau full apply hoàn tất, permission 0600, không chứa DB password. Partial PMM/config/tag/limit không nâng baseline. Baseline cũ cần một full apply, vẫn không phải bằng chứng live metrics/exporters khỏe.
