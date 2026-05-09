
# Развертывание сервера Matrix (Synapse) с интеграцией S3, Coturn и мостов

Данное руководство описывает процесс развертывания децентрализованного сервера обмена сообщениями на базе протокола Matrix. Архитектура предполагает разделение ролей: хранение данных (PostgreSQL/S3) вынесено на NAS, ядро Synapse и мосты изолированы в отдельных LXC-контейнерах, а маршрутизация внешнего трафика и ретрансляция звонков осуществляются через VPS.

---

## Этап 1. Подготовка среды и базы данных

### 1. Настройка параметров LXC (Proxmox)
Для корректной работы системы монтирования S3-хранилища контейнер Synapse должен иметь доступ к модулю FUSE.
1. В веб-интерфейсе Proxmox выберите контейнер, выделенный под Synapse.
2. Перейдите в раздел **Options** -> **Features**.
3. Установите флажки напротив **FUSE** и **Nesting**.
4. Перезагрузите контейнер.

### 2. Инициализация баз данных (на сервере баз данных / NAS)
Сервер Synapse критически зависим от локали базы данных. Использование локали по умолчанию может привести к ошибкам сортировки данных.

Подключитесь к серверу PostgreSQL и выполните команды:
```bash
sudo -u postgres psql
```
```sql
-- База данных для ядра Synapse
CREATE USER synapse WITH PASSWORD 'Ваш_Пароль_Synapse';
CREATE DATABASE synapse OWNER synapse ENCODING 'UTF8' LC_COLLATE = 'C' LC_CTYPE = 'C' TEMPLATE template0;

-- База данных для моста Telegram
CREATE USER mautrix_tg WITH PASSWORD 'Ваш_Пароль_TG_Bridge';
CREATE DATABASE mautrix_tg OWNER mautrix_tg ENCODING 'UTF8' LC_COLLATE = 'C' LC_CTYPE = 'C' TEMPLATE template0;

\q
```

---

## Этап 2. Интеграция объектного хранилища (S3) через Rclone

Медиафайлы будут храниться в бакете S3, примонтированном к файловой системе контейнера. Все действия ниже выполняются в контейнере Synapse (например, `172.20.6.5`).

1. Установите зависимости и разрешите работу FUSE:
   ```bash
   apt update && apt upgrade -y
   apt install -y curl rclone fuse3
   echo "user_allow_other" >> /etc/fuse.conf
   ```

2. Создайте конфигурацию Rclone:
   ```bash
   mkdir -p /root/.config/rclone
   nano /root/.config/rclone/rclone.conf
   ```
   Вставьте параметры доступа к вашему S3 (MinIO):
   ```ini
   [rustfs]
   type = s3
   provider = Minio
   env_auth = false
   access_key_id = ВАШ_ACCESS_KEY
   secret_access_key = ВАШ_SECRET_KEY
   endpoint = http://172.20.50.13:9000
   force_path_style = true
   region = us-east-1
   ```

3. Создайте системную службу для монтирования бакета:
   ```bash
   mkdir -p /opt/synapse/data/media_store
   mkdir -p /var/cache/rclone
   nano /etc/systemd/system/rclone-matrix.service
   ```
   Конфигурация службы:
   ```ini
   [Unit]
   Description=Rclone mount for Matrix Media
   After=network-online.target

   [Service]
   Type=notify
   ExecStartPre=/bin/mkdir -p /opt/synapse/data/media_store
   ExecStartPre=/bin/mkdir -p /var/cache/rclone
   ExecStart=/usr/bin/rclone mount rustfs:matrix-media /opt/synapse/data/media_store \
     --config=/root/.config/rclone/rclone.conf \
     --allow-other \
     --dir-perms 0770 \
     --file-perms 0660 \
     --vfs-cache-mode full \
     --vfs-cache-max-size 5G \
     --vfs-cache-max-age 24h \
     --vfs-read-chunk-size 16M \
     --buffer-size 32M \
     --umask 007
   ExecStop=/bin/fusermount3 -uz /opt/synapse/data/media_store
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
4. Запустите службу:
   ```bash
   systemctl daemon-reload
   systemctl enable --now rclone-matrix
   ```

---

## Этап 3. Установка и настройка ядра Synapse

1. Установите зависимости и создайте системного пользователя:
   ```bash
   apt install -y build-essential python3 python3-dev python3-venv python3-pip \
                  libffi-dev libssl-dev libjpeg-dev libxslt1-dev libpq-dev \
                  zlib1g-dev libwebp-dev sudo nano

   adduser --system --group --no-create-home synapse
   chown -R synapse:synapse /opt/synapse
   ```

2. Инициализируйте виртуальное окружение и установите Synapse:
   ```bash
   python3 -m venv /opt/synapse/env
   source /opt/synapse/env/bin/activate
   pip install --upgrade pip setuptools wheel
   pip install matrix-synapse[all] psycopg2
   ```

3. Сгенерируйте базовую конфигурацию:
   ```bash
   cd /opt/synapse/data
   python -m synapse.app.homeserver \
       --server-name ваш_домен.ru \
       --config-path /opt/synapse/data/homeserver.yaml \
       --generate-config \
       --report-stats=no
   
   chown -R synapse:synapse /opt/synapse/data
   ```

4. Отредактируйте конфигурационный файл `homeserver.yaml`:
   ```bash
   nano /opt/synapse/data/homeserver.yaml
   ```
   Внесите следующие изменения:
   *   **listeners:** Измените `bind_addresses` на `['0.0.0.0']`, добавьте `x_forwarded: true`.
   *   **database:** Настройте подключение к PostgreSQL (пользователь `synapse`, IP `172.20.50.10`).
   *   **redis:** Включите `enabled: true`, укажите IP и пароль сервера Valkey/Redis.
   *   **media_store_path:** Проверьте путь `/opt/synapse/data/media_store`.
   *   **enable_registration:** Установите в `false`.
   *   **push:** Включите службу и добавьте доверенные прокси (matrix.org, vector.im, element.io).
   *   **Rate Limits:** Для стабильной работы мостов увеличьте лимиты (`rc_messages`, `rc_joins` и т.д.) до значений `per_second: 1000`, `burst_count: 5000`.

5. Создайте системную службу `matrix-synapse.service`:
   ```bash
   nano /etc/systemd/system/matrix-synapse.service
   ```
   ```ini
   [Unit]
   Description=Matrix Synapse
   After=network.target rclone-matrix.service
   Requires=rclone-matrix.service

   [Service]
   Type=simple
   User=synapse
   Group=synapse
   WorkingDirectory=/opt/synapse/data
   ExecStart=/opt/synapse/env/bin/python -m synapse.app.homeserver --config-path /opt/synapse/data/homeserver.yaml
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=multi-user.target
   ```
6. Выйдите из VENV (`deactivate`), запустите службу и создайте администратора:
   ```bash
   systemctl daemon-reload
   systemctl enable --now matrix-synapse
   /opt/synapse/env/bin/register_new_matrix_user -c /opt/synapse/data/homeserver.yaml http://localhost:8008
   ```

---

## Этап 4. Настройка внешнего доступа и ретрансляции звонков (VPS)

Все действия выполняются на удаленном VPS-сервере.

### 1. Reverse Proxy (Caddy)
Отредактируйте `/etc/caddy/Caddyfile`, добавив блок маршрутизации:
```caddy
matrix.ваш_домен.ru {
    handle_path /.well-known/matrix/client {
        header Access-Control-Allow-Origin "*"
        header Content-Type "application/json"
        respond `{"m.homeserver":{"base_url":"https://matrix.ваш_домен.ru"}}`
    }
    handle_path /.well-known/matrix/server {
        header Content-Type "application/json"
        respond `{"m.server":"matrix.ваш_домен.ru:443"}`
    }
    reverse_proxy 172.20.6.5:8008 {
        header_up X-Forwarded-For {remote_host}
    }
}
```
Перезапустите Caddy: `systemctl reload caddy`.

### 2. Ретранслятор WebRTC (Coturn)
Установите пакет `coturn` и отредактируйте `/etc/turnserver.conf`:
```ini
listening-port=3479
tls-listening-port=5349
external-ip=ВНЕШНИЙ_IP_VPS
min-port=50000
max-port=52000
use-auth-secret
static-auth-secret=Секретный_Ключ_Для_Coturn
realm=matrix.ваш_домен.ru
no-tcp-relay
no-cli
```
Откройте порты `3479` (TCP/UDP), `5349` (TCP/UDP) и `50000-52000` (UDP) в Nftables и запустите службу `coturn`.

### 3. Интеграция Coturn в Synapse
Вернитесь в контейнер Synapse, откройте `homeserver.yaml` и добавьте настройки TURN:
```yaml
turn_uris:
  - "turn:matrix.ваш_домен.ru:3479?transport=udp"
  - "turn:matrix.ваш_домен.ru:3479?transport=tcp"
  - "turns:matrix.ваш_домен.ru:5349?transport=udp"
  - "turns:matrix.ваш_домен.ru:5349?transport=tcp"

turn_shared_secret: "Секретный_Ключ_Для_Coturn"
turn_user_lifetime: 86400000
turn_allow_guests: true
```
Перезапустите Synapse. Звонки [аудио/видео] через мобильные сети теперь активны.

---

## Этап 5. Установка моста Telegram (Изолированный LXC)

Все действия выполняются в отдельном контейнере (например, `172.20.6.6`).

1. Подготовьте окружение:
   ```bash
   apt install -y build-essential python3 python3-dev python3-venv python3-pip libffi-dev libssl-dev libpq-dev libmagic-dev ffmpeg
   adduser --system --group --no-create-home mautrix
   mkdir -p /opt/mautrix-telegram && chown -R mautrix:mautrix /opt/mautrix-telegram
   cd /opt/mautrix-telegram
   python3 -m venv env && source env/bin/activate
   ```

2. Установите мост (с понижением версии setuptools для совместимости):
   ```bash
   pip install --upgrade pip wheel
   pip install "setuptools<70" 
   pip install mautrix-telegram[all] psycopg2 pysocks
   ```

3. Загрузите и настройте `config.yaml`:
   ```bash
   wget https://raw.githubusercontent.com/mautrix/telegram/v0.15.3/mautrix_telegram/example-config.yaml -O config.yaml
   nano config.yaml
   ```
   *   Укажите `homeserver.address` (`http://172.20.6.5:8008`) и `appservice.address` (`http://172.20.6.6:29317`).
   *   Настройте БД (строка `postgresql://mautrix_tg...`).
   *   В секции `double_puppet_server_map` настройте марионеточное управление. **Внимание:** параметр `login_shared_secret_map` должен быть записан в одну строку словаря: `{"ваш_домен.ru": "СЕКРЕТ_ИЗ_HOMESERVER_YAML"}`.
   *   Введите `api_id` и `api_hash`.

4. Сгенерируйте файл регистрации и перенесите в Synapse:
   ```bash
   python -m mautrix_telegram -g -c config.yaml
   ```
   Скопируйте вывод команды. В контейнере Synapse создайте файл `/opt/synapse/data/telegram-registration.yaml`, вставьте содержимое и добавьте путь к этому файлу в конец `homeserver.yaml` (в блок `app_service_config_files`). Перезапустите Synapse.

5. Запустите мост как службу:
   Создайте юнит `/etc/systemd/system/mautrix-telegram.service`:
   ```ini
   [Service]
   Type=simple
   User=mautrix
   WorkingDirectory=/opt/mautrix-telegram
   ExecStart=/opt/mautrix-telegram/env/bin/python -m mautrix_telegram -c /opt/mautrix-telegram/config.yaml
   Restart=always
   ```
   Активируйте службу. В клиенте Element начните чат с `@telegrambot:ваш_домен.ru` и введите команду `login`.

---

## Этап 6. Установка Web-интерфейса администратора (Synapse-Admin)

Панель управления ставится в контейнер Synapse (`172.20.6.5`).

1. Скачайте и распакуйте статические файлы:
   ```bash
   mkdir -p /opt/synapse-admin && cd /opt/synapse-admin
   URL=$(curl -s https://api.github.com/repos/Awesome-Technologies/synapse-admin/releases/latest | grep browser_download_url | grep "\.tar\.gz" | cut -d '"' -f 4)
   wget $URL -O admin.tar.gz && tar -xzf admin.tar.gz
   mv synapse-admin-*/* . 2>/dev/null
   rm -rf synapse-admin-*/ admin.tar.gz
   chown -R synapse:synapse /opt/synapse-admin
   ```

2. Настройте службу встроенного веб-сервера `synapse-admin.service`:
   ```ini
   [Service]
   Type=simple
   User=synapse
   WorkingDirectory=/opt/synapse-admin
   ExecStart=/usr/bin/python3 -m http.server 5173
   Restart=always
   ```
   Активируйте службу. Админ-панель доступна по адресу `http://172.20.6.5:5173`. При авторизации в поле Homeserver указывайте локальный адрес `http://172.20.6.5:8008`.

---

## Дополнение: Полезные команды

**Принудительная выдача прав администратора в комнате (через Admin API):**
```bash
curl -X POST \
     -H "Authorization: Bearer <ТОКЕН_АДМИНИСТРАТОРА>" \
     -H "Content-Type: application/json" \
     --data '{"user_id": "<@пользователь:ваш_домен.ru>"}' \
     '<URL_СЕРВЕРА>/_synapse/admin/v1/rooms/<ВНУТРЕННИЙ_ID_КОМНАТЫ>/make_room_admin'
```
*Внутренний ID комнаты всегда начинается с символа `!` и доступен в разделе "Дополнительно" настроек комнаты.*