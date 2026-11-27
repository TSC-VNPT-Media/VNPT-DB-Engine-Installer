# Môi trường Python trên máy điều phối

Máy chạy `vde-install` chính là server `standalone` hoặc một node điều phối duy nhất trong cụm `ha`.

## 1. Yêu cầu hệ thống

- **Python 3.12** cùng các tiện ích hệ thống `free`, `nproc`, `ip`.
- Cài đặt thật cần quyền `root` hoặc cờ `--sudo` với tài khoản sudo không cần mật khẩu.
- **Triển khai HA**: node điều phối cần thêm **OpenSSH client** (`ssh`) và kết nối SSH không tương tác tới các node còn lại.
- **Triển khai Standalone**: không cần SSH client/server/key.
- Máy phát triển Linux/WSL nằm ngoài cụm chỉ phù hợp cho `validate`, `plan --offline` và `apply --dry-run`; cài thật cần máy có IP khớp node `local: true` và hệ điều hành đích có `dnf`/`systemd`.

Dependency Python ngoài thư viện chuẩn hiện chỉ có `PyYAML` (ghim trong `requirements.txt`). Tool gọi trực tiếp lệnh `ssh` của hệ điều hành, không dùng Paramiko/AsyncSSH. Không chạy `sudo pip`; cờ `--sudo` chỉ nâng quyền các lệnh cài đặt trên hệ thống, không đổi interpreter Python của virtualenv.

---

## 2. Tạo và kiểm tra virtualenv

Chạy từ thư mục `install-tool/`:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Kiểm tra interpreter, dependency và CLI
python -c "import sys, yaml; print(sys.executable); print(yaml.__version__)"
python -m pip check
python src/main.py --version
python src/main.py validate -c src/config/vde-cluster.example.yml
python src/main.py plan -c src/config/vde-cluster.example.yml --offline
```

- Nếu `python3` chưa trỏ đúng phiên bản, chỉ định rõ interpreter khi tạo môi trường, ví dụ `python3.12 -m venv .venv`.
- Sau khi `activate`, luôn dùng `python -m pip` để cài gói vào đúng interpreter; không dùng `sudo pip` hoặc sửa Python hệ thống.
- Mỗi phiên terminal mới cần chạy lại `source .venv/bin/activate`; thoát môi trường bằng lệnh `deactivate`.

Có thể gọi trực tiếp interpreter trong `.venv` mà không cần `activate` (phù hợp cho script tự động hóa):

```bash
.venv/bin/python src/main.py validate -c cluster.yml
.venv/bin/python src/main.py plan -c cluster.yml --offline
.venv/bin/python src/main.py apply -c cluster.yml --dry-run
```

Hoặc dùng wrapper `bin/vde-ins`: tự chọn `.venv/bin/python` nếu có, nếu không dùng `python3.12`, cuối cùng là `python3` (báo lỗi `Python 3 was not found` khi không có interpreter nào):

```bash
bin/vde-ins validate -c cluster.yml
bin/vde-ins plan -c cluster.yml --offline
```

> Virtualenv chỉ phục vụ CLI trên node local. Không sao chép `.venv` sang các node remote; các module shell sẽ cài RPM và thành phần cần thiết trên từng node theo luồng riêng.

---

## 3. Xử lý lỗi môi trường thường gặp

| Lỗi | Cách xử lý |
|---|---|
| `No module named venv`, `ensurepip is not available` hoặc tạo venv thất bại ở bước pip | Cài gói hỗ trợ venv cho đúng phiên bản Python. Trên Ubuntu/Debian: `sudo apt-get update && sudo apt-get install python3-venv python3-pip` (hoặc `python3.12-venv` nếu dùng phiên bản riêng). Sau đó tạo lại `.venv`. |
| `No module named pip` bên trong venv | Nếu interpreter có `ensurepip`, chạy `.venv/bin/python -m ensurepip --upgrade` rồi `.venv/bin/python -m pip install -r requirements.txt`. Nếu không có, cài đủ gói Python/venv như dòng trên. |
| `ModuleNotFoundError: No module named 'yaml'` | Chạy `.venv/bin/python -m pip install -r requirements.txt` và dùng `.venv/bin/python` để thực thi tool. |
| `externally-managed-environment` hoặc pip báo không đủ quyền ghi thư mục hệ thống | Kiểm tra `python -c "import sys; print(sys.executable)"` và chắc chắn đang dùng Python trong `.venv`. |
| Không tải được gói do mạng/proxy | Máy điều phối cần truy cập PyPI hoặc mirror nội bộ; với môi trường không có Internet, dùng quy trình chuẩn bị wheel ở mục 5. |

---

## 4. Tạo lại virtualenv khi đổi Python hoặc chuyển máy

Khi đổi phiên bản Python hoặc di chuyển source sang máy khác, tạo mới virtualenv và cài lại từ `requirements.txt`. Có thể giữ bản cũ để đối chiếu trước khi xóa:

```bash
deactivate  # chỉ chạy nếu đang activate venv
mv .venv .venv.previous
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

Không tiếp tục dùng virtualenv đã đổi tên vì các script bên trong chứa đường dẫn tuyệt đối. Thư mục `.venv.previous` đã được `.gitignore` loại trừ.

---

## 5. Cài đặt dependency Python cho môi trường offline

Chuẩn bị gói wheel trên một máy có Internet có **cùng OS, kiến trúc CPU và phiên bản Python** với máy điều phối offline:

```bash
# Trên máy có Internet
python -m pip download --only-binary=:all: -r requirements.txt -d .local/wheels

# Chép requirements.txt và thư mục .local/wheels sang máy offline đã có Python/venv
.venv/bin/python -m pip install --no-index --find-links .local/wheels -r requirements.txt
```

> Quy trình này chỉ chuẩn bị dependency Python cho máy điều phối. Kho RPM cho các node đích (VNPT internal repo hoặc Percona mirror) cần được chuẩn bị riêng.

---

## 6. File cục bộ và Git

File `.gitignore` mặc định loại trừ virtualenv (`.venv/`, `.venv.*/`), Python cache, log, dữ liệu `.codegraph/`, `.env`, thư mục `.local/` và các manifest cluster cục bộ (`cluster*.yml`, `vde-cluster.yml`).

- Lưu các manifest chứa IP thật và mật khẩu trong `.local/` hoặc các file `cluster*.yml`.
- Chỉ commit các bản mẫu đã loại bỏ thông tin nhạy cảm như `src/config/vde-cluster.example.yml` và `src/config/standalone.example.yml`.
- Lưu ý `.gitignore` không tự hủy theo dõi những file đã được commit từ trước.

Tài liệu tham khảo: [Python venv](https://docs.python.org/3/library/venv.html), [PyYAML](https://github.com/yaml/pyyaml).
