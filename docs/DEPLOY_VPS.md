# Hướng dẫn Deploy lên VPS (Venv / Docker / Cloudflare Tunnel)

Tài liệu này hướng dẫn cách đưa project **Exam Fraud Monitor** lên máy chủ VPS. Project được thiết lập để chạy trên port không phổ biến (**9142**) nhằm tăng tính bảo mật.

---

## 1. Yêu cầu hệ thống (Ubuntu/Debian)

Cài đặt các gói cơ bản:
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx git redis-server curl
```

---

## 2. Cách 1: Deploy bằng Docker (Khuyên dùng)

Dữ liệu (Media & Database) được Docker quản lý tập trung bên trong các volumes, giúp dọn dẹp thư mục máy host.

### Bước 1: Cài đặt Docker & Compose
```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

### Bước 2: Build và chạy app (Port 9142)
```bash
cd /var/www/chatbot-violation
# Chạy containers
docker compose up -d
```

### Bước 3: Tạo Admin & Init data
```bash
docker compose exec app python manage.py migrate
docker compose exec app python manage.py createsuperuser
docker compose exec app python manage.py set_user_role <username> --role super_admin
```

---

## 3. Cách 2: Deploy bằng Venv (Truyền thống)

### Bước 1: Setup Venv
```bash
cd /var/www/chatbot-violation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Bước 2: Config Systemd
Sử dụng port `9142` trong file `/etc/systemd/system/daphne.service`:
```ini
[Unit]
Description=Daphne Service
After=network.target

[Service]
User=root
WorkingDirectory=/var/www/chatbot-violation
ExecStart=/var/www/chatbot-violation/venv/bin/daphne -b 0.0.0.0 -p 9142 chatbox_vi_pham.asgi:application
Restart=always

[Install]
WantedBy=multi-user.target
```
Kích hoạt: `sudo systemctl enable --now daphne`

---

## 4. Public web bằng Cloudflare Tunnel

Sử dụng Cloudflare Tunnel trỏ thẳng vào port **9142** của ứng dụng.

### Bước 1: Cài đặt cloudflared
```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
```

### Bước 2: Login và tạo Tunnel
```bash
cloudflared tunnel login
cloudflared tunnel create my-exam-app
```

### Bước 3: Cấu hình DNS
```bash
cloudflared tunnel route dns my-exam-app exam.yourdomain.com
```

### Bước 4: Chạy Tunnel trỏ vào Port 9142
```bash
# Lưu ý: Trỏ vào localhost:9142
cloudflared tunnel run --url http://localhost:9142 my-exam-app
```

---

## 5. Lưu ý quan trọng

1. **Media serving**: Django đã được cấu hình để tự serve media kể cả khi `DEBUG=False` (trong `urls.py`) để tương thích tốt nhất với Cloudflare Tunnel.
2. **Persistent Storage**: Docker đang dùng **Named Volumes** (`media_data`, `db_data`). Bạn có thể kiểm tra bằng lệnh `docker volume ls`.
3. **ALLOWED_HOSTS**: Hãy cập nhật domain của bạn vào biến môi trường trong `docker-compose.yml` nếu cần thiết.
4. **Firewall**: Vì dùng Cloudflare Tunnel, bạn **không cần** mở port 9142 trên Firewall (UFW/Iptables) của VPS. Cloudflared sẽ tự kết nối ra ngoài.
