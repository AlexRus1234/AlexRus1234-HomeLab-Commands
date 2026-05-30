---

# Развертывание файлового облака (SFTPGo) и интеграция SSO (OIDC)

---

## Этап 1. Расширение прямого сетевого линка (переход на подсеть /24)

Для обеспечения прямого взаимодействия сервисов необходимо перенастроить сетевые интерфейсы и службы на внешнем узле хранилища (NAS) и гипервизоре.

### 1. Конфигурация узла хранения (NAS `SKLD00`)
1. Измените адрес сетевого интерфейса:
   ```bash
   sudo micro /etc/systemd/network/10-enp8s0.network
   ```
   *Измените параметр:* `Address=172.21.6.249/24`

2. Настройте прослушивание портов в PostgreSQL:
   ```bash
   sudo micro /var/lib/postgres/data/postgresql.conf
   ```
   *Измените параметр:* `listen_addresses = '172.20.50.10, 172.21.6.249'`

3. Разрешите подключения к БД из новой подсети:
   ```bash
   sudo micro /var/lib/postgres/data/pg_hba.conf
   ```
   *Добавьте строку:* `host all all 172.21.6.0/24 scram-sha-256`

4. Обновите параметры привязки (bind) для других баз данных (при их наличии):
   * В файле `/etc/mongodb.conf` добавьте IP: `bindIp: 127.0.0.1,172.20.50.11,172.21.6.249`
   * В файле `/etc/valkey/valkey.conf` добавьте IP: `bind 172.20.50.12 172.21.6.249`

5. Перезапустите сетевой стек и службы баз данных:
   ```bash
   sudo systemctl restart systemd-networkd postgresql mongodb valkey
   ```
   *(Примечание: Убедитесь, что база данных `sftpgo` и соответствующий пользователь предварительно созданы в PostgreSQL).*

### 2. Конфигурация гипервизора (Proxmox `yadr01`)
1. Откройте файл настройки сетевых интерфейсов:
   ```bash
   nano /etc/network/interfaces
   ```
2. Удалите IP-адрес с физического интерфейса `nic1` и создайте сетевой мост:
   ```ini
   iface nic1 inet manual
   
   auto vmbr1
   iface vmbr1 inet static
           address 172.21.6.250/24
           bridge-ports nic1
           bridge-stp off
           bridge-fd 0
   ```
3. Обновите пути монтирования и конфигурацию хранилищ, указав новый IP-адрес NAS (`172.21.6.249`):
   * `nano /etc/fstab`
   * `nano /etc/pve/storage.cfg`
4. Примените конфигурацию сети и перемонтируйте ресурсы:
   ```bash
   systemctl restart networking
   systemctl daemon-reload
   umount /mnt/.nas_root_hidden
   mount -a
   ```

---

## Этап 2. Подготовка файловой системы (Bind Mount)

Выполняется на хосте Proxmox (`yadr01`) для последующего проброса директории внутрь непривилегированного контейнера.

1. Создайте целевую директорию:
   ```bash
   mkdir -p /mnt/sftpgo_data
   ```
2. Откройте таблицу файловых систем:
   ```bash
   nano /etc/fstab
   ```
3. Добавьте в конец файла правило монтирования (соблюдайте порядок загрузки):
   ```text
   /mnt/.nas_root_hidden /mnt/sftpgo_data none bind,_netdev,x-systemd.requires-mounts-for=/mnt/.nas_root_hidden,x-systemd.before=pve-guests.service 0 0
   ```
4. Обновите конфигурацию Systemd и смонтируйте директорию:
   ```bash
   systemctl daemon-reload && mount -a
   ```

---

## Этап 3. Создание и подготовка контейнера (LXC)

1. В веб-интерфейсе Proxmox VE создайте контейнер:
   * **Тип:** Unprivileged (Непривилегированный).
   * **Шаблон ОС:** `archlinux`.
   * **ID:** `106` | **Hostname:** `sftpgo`.
   * **Disk:** 8 GB | **CPU:** 2 vCPU | **RAM:** 1024 MB.
   * **Сетевой интерфейс 0:** `vmbr0`, IP: `172.20.6.8/16`, Шлюз: `172.20.0.1`.
   * **Сетевой интерфейс 1:** `vmbr1`, IP: `172.21.6.251/24` (Без шлюза).
   * **Не инициируйте запуск контейнера.**

2. В консоли Proxmox настройте проброс директории (Bind Mount):
   ```bash
   nano /etc/pve/lxc/106.conf
   ```
   Добавьте строку:
   ```ini
   mp0: /mnt/sftpgo_data,mp=/srv/sftpgo/data
   ```

3. Запустите контейнер, откройте его консоль и выполните базовую настройку Arch Linux:
   * Откройте `nano /etc/pacman.conf` и раскомментируйте параметр `DisableSandbox`.
   * Инициализируйте ключи и обновите систему:
     ```bash
     pacman-key --init && pacman-key --populate archlinux
     pacman -Sy archlinux-keyring --noconfirm
     pacman -Syu base-devel git micro sudo --noconfirm
     ```
   * Интегрируйте корневой сертификат (Root CA):
     Скопируйте сертификат в `/etc/ca-certificates/trust-source/anchors/step-ca.crt` и выполните `update-ca-trust`.

---

## Этап 4. Установка и базовая конфигурация SFTPGo

1. Создайте пользователя для сборки пакетов из AUR и выполните установку:
   ```bash
   useradd -m -G wheel builduser
   passwd -d builduser
   echo "builduser ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
   su - builduser
   git clone https://aur.archlinux.org/sftpgo-bin.git
   cd sftpgo-bin
   makepkg -si --noconfirm
   exit
   ```

2. Откройте основной конфигурационный файл:
   ```bash
   micro /etc/sftpgo/sftpgo.json
   ```

3. Внесите следующие изменения:
   * В блоке `data_provider` укажите параметры БД: `driver` $\rightarrow$ `"postgresql"`, `host` $\rightarrow$ `"172.21.6.249"`, а также логин и пароль.
   * В блоке `httpd` $\rightarrow$ `bindings` замените конфигурацию по умолчанию на два раздельных блока для внутренней и внешней маршрутизации:

   *Блок 1 (Внешний доступ, порт 8080):*
   ```json
   {
     "port": 8080, "address": "0.0.0.0",
     "hide_login_url": 3, "disabled_login_methods": 12,
     "enable_web_admin": true, "enable_web_client": true,
     "oidc": {
       "client_id": "<ВАШ_CLIENT_ID>", "client_secret": "<ВАШ_CLIENT_SECRET>",
       "config_url": "https://auth.yadr00.internal/application/o/sftp-go",
       "redirect_base_url": "https://sftpgo.alexrus1234.ru",
       "scopes": ["openid", "profile", "email"],
       "username_field": "preferred_username", "implicit_roles": true
     }
   }
   ```

   *Блок 2 (Внутренний доступ, порт 8081):*
   ```json
   {
     "port": 8081, "address": "0.0.0.0",
     "hide_login_url": 2, "disabled_login_methods": 8,
     "enable_web_admin": true, "enable_web_client": true,
     "oidc": {
       "client_id": "<ВАШ_CLIENT_ID>", "client_secret": "<ВАШ_CLIENT_SECRET>",
       "config_url": "https://auth.yadr00.internal/application/o/sftp-go",
       "redirect_base_url": "https://sftpgo.yadr01.internal",
       "scopes": ["openid", "profile", "email"],
       "username_field": "preferred_username", "implicit_roles": true
     }
   }
   ```

4. Активируйте и запустите службу:
   ```bash
   systemctl enable --now sftpgo
   ```

---

## Этап 5. Настройка обратного прокси-сервера (Caddy)

На сервере Caddy настройте маршрутизацию трафика на соответствующие порты контейнера SFTPGo.

1. Внесите изменения в `Caddyfile`:
   ```caddyfile
   # Внутренний домен (OIDC-редирект локальной сети)
   sftpgo.yadr01.internal {
       import my_tls
       reverse_proxy 172.20.6.8:8081
   }
   
   # Внешний домен (Публичный доступ)
   sftpgo.alexrus1234.ru {
       # (Укажите ваши настройки TLS)
       reverse_proxy 172.20.6.8:8080
   }
   ```
2. Примените конфигурацию:
   ```bash
   systemctl reload caddy
   ```

---

## Этап 6. Настройка прав доступа и подключение клиентов

### 1. Инициализация профиля через Web-интерфейс (SSO)
1. Откройте в браузере внутренний адрес `https://sftpgo.yadr01.internal`.
2. Выполните вход через OIDC-провайдер (Authentik). Локальные формы авторизации будут скрыты.
3. После авторизации перейдите в административную панель.
4. Откройте раздел **Users**, найдите созданный OIDC-аккаунт.
5. В параметрах пользователя укажите путь к домашней директории (**Home Dir**): `/srv/sftpgo/data`. Сохраните изменения.

### 2. Настройка доступа по протоколу SFTP (Мобильные / десктоп клиенты)
*Архитектурная справка: Протокол SFTP не поддерживает прямое перенаправление OAuth2/OIDC, поэтому для сторонних клиентов требуется генерация локального пароля.*

1. В административной панели SFTPGo (раздел **Users**) задайте локальный пароль (Password) для вашего пользователя.
2. В клиентском приложении (например, FolderSync) создайте новое подключение со следующими параметрами:
   * **Протокол:** `SFTP`
   * **IP-адрес:** `172.20.6.8` (или доменное имя, если проксируется SSH-трафик)
   * **Порт:** `2022`
   * **Учетные данные:** Логин OIDC-пользователя и заданный локальный пароль.
3. Выполните подключение для доступа к файловой системе.

### *Альтернаивный вариант.*

Я настаятельно рекомендую его
1. В административной панели SFTPGo (раздел **Users**) удалите локальный пароль (Password) для вашего пользователя, впишите в поле ssh ключа публичный слепок
2. В клиентском приложении (например, FolderSync) создайте новое подключение со следующими параметрами:
   * **Протокол:** `SFTP`
   * **IP-адрес:** `172.20.6.8` (или доменное имя, если проксируется SSH-трафик)
   * **Порт:** `2022`
   * **Учетные данные:** Логин OIDC-пользователя и файл секретного ключа.
3. Выполните подключение для доступа к файловой системе.