
---

# Развертывание Authentik (Podman + Quadlets) и интеграция сервисов (SSO)

---

## Этап 1. Подготовка виртуальной машины и установка пакетов

Перед началом работы с контейнерами необходимо подготовить среду внутри виртуальной машины (Arch Linux).

1. Установите базовые системные утилиты и компоненты для работы с контейнерами:
   ```bash
   sudo pacman -S qemu-guest-agent btop nano wget curl git net-tools base-devel podman podlet podman-compose openssl --noconfirm
   ```

**Назначение ключевых пакетов:**
* `podman` — основной движок для запуска контейнеров (без демона).
* `podlet` — конвертер манифестов Docker Compose в системные файлы Systemd (`.container`).
* `podman-compose` — используется исключительно как препроцессор для разрешения переменных среды перед конвертацией (контейнеры через него запускаться не будут).
* `openssl` — требуется для безопасной генерации криптографического ключа.

---

## Этап 2. Подготовка базы данных (PostgreSQL)

База данных располагается на внешнем узле хранилища. 

1. Подключитесь по SSH к серверу баз данных.
2. Войдите в консоль управления PostgreSQL:
   ```bash
   sudo -u postgres psql
   ```
3. Создайте пользователя и базу данных для Authentik (замените пароль на свой):
   ```sql
   CREATE USER authentik WITH PASSWORD 'Ваш_Пароль';
   CREATE DATABASE authentik OWNER authentik;
   \q
   ```

---

## Этап 3. Загрузка и адаптация манифеста (docker-compose)

Официальный манифест Authentik содержит компоненты, которые не требуются при использовании внешних баз данных. Их необходимо удалить.

1. Вернитесь в виртуальную машину для контейнеров.
2. Создайте рабочую директорию и загрузите манифест:
   ```bash
   mkdir ~/authentik-build
   cd ~/authentik-build
   wget https://goauthentik.io/docker-compose.yml
   ```
3. Откройте файл для редактирования:
   ```bash
   nano docker-compose.yml
   ```
4. Внесите следующие изменения в конфигурацию:
   * **Удалите** полные блоки сервисов `postgresql` и `redis`.
   * **Удалите** параметры `depends_on:` в сервисах `server` и `worker`.
   * **Удалите** проброс сокета `- /var/run/docker.sock:/var/run/docker.sock` (несовместимо с Podman).
   * **Удалите** блоки `environment:`, так как переменные будут передаваться через отдельный файл.
   * **Удалите** блок `volumes:` в самом конце файла.
   * **Измените** пути в параметрах `env_file:` на абсолютные: `- /etc/authentik/.env`.

Итоговый файл `docker-compose.yml` должен выглядеть следующим образом:
```yml
services:
  server:
    command: server
    env_file:
    - /etc/authentik/.env
    image: ${AUTHENTIK_IMAGE:-ghcr.io/goauthentik/server}:${AUTHENTIK_TAG:-2026.2}
    ports:
    - ${COMPOSE_PORT_HTTP:-9000}:9000
    - ${COMPOSE_PORT_HTTPS:-9443}:9443
    restart: unless-stopped
    shm_size: 512mb
    volumes:
    - ./data:/data
    - ./custom-templates:/templates
  worker:
    command: worker
    env_file:              
    - /etc/authentik/.env  
    image: ${AUTHENTIK_IMAGE:-ghcr.io/goauthentik/server}:${AUTHENTIK_TAG:-2026.2}
    restart: unless-stopped
    shm_size: 512mb
    user: root
    volumes:
    - ./data:/data
    - ./certs:/certs
    - ./custom-templates:/templates
```

---

## Этап 4. Настройка файла переменных окружения (.env)

Данный файл будет хранить параметры подключения и криптографические ключи.

1. Сгенерируйте случайный 36-значный ключ и скопируйте вывод команды:
   ```bash
   openssl rand -base64 36
   ```
2. Создайте директорию и файл конфигурации:
   ```bash
   sudo mkdir -p /etc/authentik
   sudo nano /etc/authentik/.env
   ```
3. Заполните файл данными вашей инфраструктуры:
   ```env
   # Подключение к Postgres (IP-адрес внешнего узла)
   AUTHENTIK_POSTGRESQL__HOST=172.20.50.10
   AUTHENTIK_POSTGRESQL__NAME=authentik
   AUTHENTIK_POSTGRESQL__USER=authentik
   AUTHENTIK_POSTGRESQL__PASSWORD=Ваш_Пароль_От_БД
   
   # Подключение к кэш-серверу Valkey (Redis)
   AUTHENTIK_REDIS__HOST=172.20.50.12
   AUTHENTIK_REDIS__PASSWORD=Ваш_Пароль_От_Valkey
   
   # Секретный ключ приложения
   AUTHENTIK_SECRET_KEY=Ваша_Сгенерированная_Строка
   ```
4. Установите строгие права доступа (чтение и запись только для `root`):
   ```bash
   sudo chmod 600 /etc/authentik/.env
   ```

---

## Этап 5. Конвертация манифеста в системные модули (Quadlets)

Для корректной конвертации необходимо предварительно разрешить переменные в YAML-файле.

1. Создайте временный файл `.env` в рабочей директории (`~/authentik-build/.env`):
   ```env
   AUTHENTIK_IMAGE=ghcr.io/goauthentik/server
   AUTHENTIK_TAG=2026.2
   COMPOSE_PORT_HTTP=9000
   COMPOSE_PORT_HTTPS=9443
   ```
2. Выполните препроцессинг файла (генерацию чистого YAML):
   ```bash
   podman-compose config > resolved.yml
   ```
3. Откройте `resolved.yml` и переименуйте имена сервисов (во избежание системных конфликтов):
   * `server:` $\rightarrow$ `authentik-server:`
   * `worker:` $\rightarrow$ `authentik-worker:`
4. Выполните конвертацию в файлы `.container`:
   ```bash
   podlet -f . compose ./resolved.yml
   ```

### Модификация сгенерированных файлов
Откройте сгенерированные файлы (`nano authentik-server.container` и `nano authentik-worker.container`) и внесите изменения:

1. В секцию `[Container]` добавьте параметр автообновления: `AutoUpdate=registry`.
2. Измените относительные пути в параметрах `Volume` на абсолютные (например, `Volume=/opt/authentik/data:/data`).
3. Добавьте в конец каждого файла секцию для автозапуска:
   ```ini
   [Install]
   WantedBy=multi-user.target
   ```

*Пример итогового файла `authentik-server.container`:*
```ini
[Container]
EnvironmentFile=/etc/authentik/.env
Exec=server
Image=ghcr.io/goauthentik/server:2026.2
PublishPort=9000:9000
PublishPort=9443:9443
ShmSize=512mb
Volume=/opt/authentik/data:/data
Volume=/opt/authentik/custom-templates:/templates
AutoUpdate=registry

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

### Запуск служб
1. Создайте рабочие директории для монтирования:
   ```bash
   sudo mkdir -p /opt/authentik/{data,certs,custom-templates}
   ```
2. Переместите конфигурационные файлы в директорию Systemd:
   ```bash
   sudo mkdir -p /etc/containers/systemd/
   sudo mv *.{container,network,volume} /etc/containers/systemd/ 2>/dev/null
   ```
3. Активируйте таймер автоматического обновления Podman:
   ```bash
   sudo systemctl enable --now podman-auto-update.timer
   ```
4. Обновите конфигурацию Systemd и запустите службы:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start authentik-server authentik-worker
   ```
5. Проверьте статус запуска (ожидайте сообщение `Starting gunicorn`):
   ```bash
   journalctl -fu authentik-server
   ```

---

## Этап 6. Настройка Reverse Proxy (Caddy)

Для обеспечения доступа к Authentik из сети, настройте проксирование на сервере Caddy.

1. Откройте конфигурационный файл:
   ```bash
   sudo nano /etc/caddy/Caddyfile
   ```
2. Добавьте блок маршрутизации:
   ```caddyfile
   auth.ваш_домен.ru {
       reverse_proxy 172.20.0.101:9000
   }
   ```
3. Примените конфигурацию:
   ```bash
   sudo systemctl reload caddy
   ```

*Первичная настройка Authentik теперь доступна по адресу `https://auth.ваш_домен.ru/if/flow/initial-setup/`.*

---

## Этап 7. Интеграция Matrix (Synapse) с Authentik

1. В веб-интерфейсе Authentik перейдите в **Applications** $\rightarrow$ **Providers**. Нажмите **Create** $\rightarrow$ **OAuth2/OpenID Provider**.
2. Заполните параметры:
   * **Name:** `Matrix`
   * **Authentication flow:** `default-authentication-flow`
   * **Authorization flow:** `default-provider-authorization-explicit-consent`
   * **Redirect URIs:** `https://matrix.ваш_домен.ru/_synapse/client/oidc/callback`
3. Нажмите **Finish**. Скопируйте значения **Client ID** и **Client Secret**.
4. Перейдите в **Applications** $\rightarrow$ **Applications**. Нажмите **Create**. Назовите приложение `Matrix` и привяжите к созданному провайдеру.
5. На сервере Synapse откройте конфигурационный файл:
   ```bash
   sudo nano /opt/synapse/data/homeserver.yaml
   ```
6. Настройте блок провайдера:
   ```yaml
   oidc_providers:
     - idp_id: authentik
       idp_name: "Authentik SSO"
       issuer: "https://auth.ваш_домен.ru/application/o/matrix/"
       client_id: "ВАШ_CLIENT_ID"
       client_secret: "ВАШ_CLIENT_SECRET"
       scopes: ["openid", "profile", "email"]
       user_mapping_provider:
         config:
           localpart_template: "{{ user.preferred_username }}"
           display_name_template: "{{ user.name }}"
       allow_existing_users: true 
   ```
7. В этом же файле отключите локальную аутентификацию:
   ```yaml
   password_config:
     enabled: false
   ```
8. Перезапустите службу: `sudo systemctl restart matrix-synapse`.

---

## Этап 8. Интеграция Proxmox VE с Authentik

*Архитектурная справка: Локальный пользователь `root@pam` в Proxmox не может быть авторизован через OIDC напрямую. Необходимо создать учетную запись через Authentik и делегировать ей права администратора кластера.*

1. В Authentik создайте провайдер (**Applications** $\rightarrow$ **Providers** $\rightarrow$ **OAuth2/OpenID Provider**).
   * **Name:** `Proxmox`
   * **Redirect URIs:** `https://pve.ваш_домен.ru/api2/extjs/access/oidc/callback` (также добавьте внутренний IP/домен, если применимо).
   * Скопируйте **Client ID** и **Client Secret**.
2. Создайте приложение (**Applications** $\rightarrow$ **Applications**) и привяжите к провайдеру `Proxmox`.
3. Зайдите в веб-интерфейс Proxmox под локальным `root`.
4. Перейдите в **Datacenter** $\rightarrow$ **Permissions** $\rightarrow$ **Realms**. Нажмите **Add** $\rightarrow$ **OpenID Connect**.
5. Заполните конфигурацию:
   * **Realm:** `authentik` (строго строчными буквами).
   * **Issuer URL:** `https://auth.ваш_домен.ru/application/o/proxmox/` (обязательно со слэшем в конце).
   * **Client ID / Client Key:** Вставьте скопированные значения.
   * Активируйте галочку **Autocreate Users**.
   * **Username Claim:** укажите `preferred_username`.
   * **Scopes:** укажите `openid profile email`.
6. Перейдите в **Datacenter** $\rightarrow$ **Permissions** и нажмите **Add** $\rightarrow$ **User Permission**.
7. Назначьте права:
   * **Path:** `/`
   * **User:** `ваш_логин_в_authentik@authentik`
   * **Role:** `Administrator`

**Готово!** Теперь при выходе из профиля Proxmox в списке *Realm* появится опция `authentik`. Авторизация будет происходить через SSO, а пользователю будут предоставлены права администратора.