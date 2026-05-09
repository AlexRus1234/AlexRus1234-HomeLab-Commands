

---

# Настройка пограничного шлюза (VPS) и оверлейной сети 

Данное руководство описывает процесс подготовки защищенного внешнего сервера (VPS) на базе Debian 12 и настройку оверлейной сети (Headscale + Caddy) для безопасного доступа к локальным ресурсам домашней инфраструктуры.

---

## Этап 1. Базовая подготовка ОС и настройка прав доступа

Первичная настройка выполняется от имени учетной записи `root`. Рекомендуется сразу создать непривилегированного пользователя для дальнейшего администрирования.

1. Принудительно задайте использование протокола IPv4 для пакетного менеджера и обновите систему:
   ```bash
   echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4
   apt update && apt upgrade -y
   apt install sudo curl nano -y
   ```
2. Создайте нового системного пользователя (замените `ваш_пользователь` на желаемое имя) и добавьте его в группу `sudo`:
   ```bash
   adduser ваш_пользователь
   usermod -aG sudo ваш_пользователь
   ```
3. **ВАЖНО:** Переключитесь на созданного пользователя. Все последующие действия выполняются от его имени:
   ```bash
   su - ваш_пользователь
   ```
4. Подготовьте директорию для хранения SSH-ключей и задайте строгие права доступа:
   ```bash
   mkdir -p ~/.ssh
   chmod 700 ~/.ssh
   touch ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```

---

## Этап 2. Настройка криптографических ключей (Действия на локальном ПК)

Для безопасного подключения необходимо сгенерировать SSH-ключ стандарта `ed25519` на вашем рабочем компьютере и передать его на VPS.

**Если ваш компьютер работает под управлением Windows (PowerShell):**
1. Сгенерируйте пару ключей:
   ```powershell
   cd ~/.ssh
   ssh-keygen -t ed25519 -f vps_debian
   ```
2. Отправьте публичный ключ на сервер:
   ```powershell
   type vps_debian.pub | ssh ваш_пользователь@ВАШ_IP_VPS "cat >> ~/.ssh/authorized_keys"
   ```

**Если ваш компьютер работает под управлением Linux / macOS:**
1. Сгенерируйте пару ключей:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/vps_debian
   ```
2. Отправьте публичный ключ на сервер:
   ```bash
   ssh-copy-id -i ~/.ssh/vps_debian.pub ваш_пользователь@ВАШ_IP_VPS
   ```

---

## Этап 3. Защита службы SSH и системная оптимизация (На VPS)

Возвращаемся к консоли сервера. Необходимо отключить аутентификацию по паролю и перенести службу на нестандартный порт.

1. Откройте конфигурацию SSH-сервера:
   ```bash
   sudo nano /etc/ssh/sshd_config
   ```
2. Внесите следующие изменения:
   *   `Port 63231` *(Укажите выбранный вами порт)*
   *   `PermitRootLogin no`
   *   `PasswordAuthentication no`
   *   `PubkeyAuthentication yes`
3. Проверьте конфигурацию на ошибки и перезапустите службу:
   ```bash
   sudo sshd -t
   sudo systemctl restart ssh
   ```
   *(Не закрывая текущую сессию, откройте новый терминал и убедитесь, что подключение по ключу и новому порту работает корректно).*

4. Создайте файл подкачки `[swap]` размером 2 ГБ для предотвращения сбоев при нехватке ОЗУ:
   ```bash
   sudo fallocate -l 2G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
5. Оптимизируйте параметры ядра (включение алгоритма BBR и защиты от IP-спуфинга):
   ```bash
   sudo nano /etc/sysctl.conf
   ```
   Добавьте в конец файла:
   ```ini
   net.ipv4.conf.default.rp_filter = 1
   net.ipv4.conf.all.rp_filter = 1
   net.core.default_qdisc = fq
   net.ipv4.tcp_congestion_control = bbr
   ```
   Примените изменения: `sudo sysctl -p`.
6. Ограничьте размер системного журнала `[journald]` до 200 МБ:
   ```bash
   sudo nano /etc/systemd/journald.conf
   ```
   Установите `SystemMaxUse=200M` и выполните `sudo systemctl restart systemd-journald`.

---

## Этап 4. Настройка межсетевого экрана и ловушки Endlessh

Установим tarpit-сервер на освободившийся 22 порт для замедления автоматических сканеров, а затем настроим правила маршрутизации.

1. Установите Endlessh:
   ```bash
   sudo apt install endlessh -y
   ```
2. Разрешите бинарному файлу прослушивать системные порты:
   ```bash
   sudo setcap 'cap_net_bind_service=+ep' /usr/bin/endlessh
   ```
3. Создайте конфигурацию Endlessh:
   ```bash
   sudo nano /etc/endlessh/config
   ```
   ```text
   Port 22
   Delay 10000
   MaxLineLength 32
   MaxClients 4096
   LogLevel 1
   BindFamily 0
   ```
4. Настройте службу systemd (если требуется переопределение стандартного юнита):
   ```bash
   sudo rm -rf /etc/systemd/system/endlessh.service.d
   sudo nano /etc/systemd/system/endlessh.service
   ```
   ```ini
   [Unit]
   Description=Endlessh SSH Tarpit
   After=network.target

   [Service]
   Type=simple
   User=root
   ExecStart=/usr/bin/endlessh -v -4 -p 22
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=multi-user.target
   ```
   Запустите службу: `sudo systemctl enable --now endlessh`.

5. Установите межсетевой экран **nftables** и задайте объединенные правила (для портов управления, веб-трафика, STUN-сервера и сети Docker):
   ```bash
   sudo apt install nftables -y
   sudo nano /etc/nftables.conf
   ```
   Замените содержимое на:
   ```nft
   flush ruleset
   table inet filter {
       chain input {
           type filter hook input priority 0; policy drop;
           iifname "lo" accept
           ct state established,related accept
           
           # SSH и ловушка
           tcp dport 63231 accept
           tcp dport 22 accept
           
           # Caddy (Web)
           tcp dport { 80, 443 } accept
           
           # Headscale DERP (STUN)
           udp dport 3478 accept
           
           icmp type echo-request accept
       }
       chain forward {
           type filter hook forward priority 0; policy drop;
           ct state established,related accept
           # Разрешение транзита для контейнеров Docker
           iifname "docker0" accept
           oifname "docker0" accept
           iifname "br-*" accept
           oifname "br-*" accept
       }
       chain output { type filter hook output priority 0; policy accept; }
   }
   ```
   Примените конфигурацию:
   ```bash
   sudo nft -f /etc/nftables.conf
   sudo systemctl enable --now nftables
   ```

---

## Этап 5. Установка системы предотвращения вторжений (CrowdSec)

Для автоматической блокировки вредоносного трафика используется CrowdSec совместно с nftables.

1. Установите репозиторий и ядро CrowdSec:
   ```bash
   curl -s https://install.crowdsec.net | sudo sh
   sudo apt install crowdsec -y
   ```
2. Установите интеграцию (bouncer) для межсетевого экрана:
   ```bash
   sudo apt install crowdsec-firewall-bouncer-nftables -y
   ```
3. Подключите коллекцию парсеров для ловушки Endlessh и перезапустите службы:
   ```bash
   sudo cscli collections install crowdsecurity/endlessh
   sudo systemctl restart crowdsec
   sudo systemctl restart crowdsec-firewall-bouncer
   ```
   *Для проверки статуса выполните `sudo cscli bouncers list`. Статус должен быть Valid.*

---

## Этап 6. Развертывание Docker и координатора сети (Headscale)

1. Установите Docker и добавьте пользователя в рабочую группу:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   newgrp docker
   ```
2. **ВАЖНО:** Перезапустите демон Docker для корректного создания сетевых цепочек поверх nftables:
   ```bash
   sudo systemctl restart docker
   ```
3. Создайте структуру каталогов для конфигурации Headscale:
   ```bash
   sudo mkdir -p /opt/headscale/{config,data}
   sudo chown -R $USER:$USER /opt/headscale
   cd /opt/headscale
   wget -O config/config.yaml https://raw.githubusercontent.com/juanfont/headscale/main/config-example.yaml
   ```
4. Настройте параметры Headscale:
   ```bash
   nano config/config.yaml
   ```
   *   Измените `server_url` на `https://vpn.ваш_домен.ru`.
   *   Измените `listen_addr` на `0.0.0.0:8080`.
   *   Измените `metrics_listen_addr` на `127.0.0.1:9090`.
   *   Настройте секцию DNS для перенаправления локальных зон:
       ```yaml
       dns:
         magic_dns: true
         base_domain: vds.ваш_домен.ru
         override_local_dns: true
         nameservers:
           global:
             - 77.88.8.8
             - 77.88.8.1
           split:
             internal:
               - 172.20.0.1
       ```
   *   Включите DERP (STUN-релей):
       ```yaml
       derp:
         server:
           enabled: true
           region_id: 999
           region_code: "my-derp"
           region_name: "My Headscale DERP"
           ipv4: "ВАШ_ВНЕШНИЙ_IP_VPS"
           stun_listen_addr: "0.0.0.0:3478"
       ```
5. Подготовьте файл композиции `docker-compose.yml`:
   ```bash
   nano docker-compose.yml
   ```
   ```yaml
   services:
     headscale:
       image: headscale/headscale:latest
       container_name: headscale
       volumes:
         - ./config:/etc/headscale
         - ./data:/var/lib/headscale
       ports:
         - "127.0.0.1:8085:8080"
         - "3478:3478/udp"
       dns:
         - 77.88.8.8
         - 77.88.8.1
       command: serve
       restart: unless-stopped
   ```
6. Инициализируйте контейнер и создайте пользователя:
   ```bash
   docker compose up -d
   docker exec headscale headscale users create ваш_пользователь
   ```

---

## Этап 7. Настройка обратного прокси (Caddy)

Caddy будет осуществлять терминацию SSL и маршрутизацию запросов к панели управления VPN и внутренним сервисам.

1. Установите Caddy:
   ```bash
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install caddy
   ```
2. Отредактируйте конфигурационный файл:
   ```bash
   sudo nano /etc/caddy/Caddyfile
   ```
   ```caddyfile
   vpn.ваш_домен.ru {
       reverse_proxy 127.0.0.1:8085 {
           header_up X-Real-IP {remote}
           header_up X-Forwarded-For {remote}
           header_up X-Forwarded-Proto {scheme}
       }
   }

   service.ваш_домен.ru {
       reverse_proxy https://service.yadr00.internal {
           transport http {
               tls_insecure_skip_verify
           }
           header_up Host {upstream_hostport}
           header_up X-Real-IP {remote_host}
       }
   }
   ```
3. Примените конфигурацию:
   ```bash
   sudo systemctl reload caddy
   ```

---

## Этап 8. Маршрутизация на домашнем шлюзе и VPS

Для обеспечения двусторонней связи требуется настроить Tailscale на обоих узлах.

**Действия на домашнем маршрутизаторе (Arch Linux):**
1. Включите транзит пакетов:
   ```bash
   echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
   sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
   ```
2. Установите Tailscale и анонсируйте локальную подсеть:
   ```bash
   sudo pacman -S tailscale
   sudo systemctl enable --now tailscaled
   sudo tailscale up --login-server https://vpn.ваш_домен.ru --advertise-routes=172.20.0.0/16 --accept-routes --accept-dns=false
   ```
   *(Скопируйте предоставленную ссылку аутентификации).*

**Действия на VPS (Регистрация маршрутизатора):**
1. Подтвердите регистрацию узла, используя ключ из браузера:
   ```bash
   docker exec headscale headscale nodes register --user ваш_пользователь --key mkey:КЛЮЧ_РОУТЕРА
   ```
2. Узнайте идентификатор (ID) зарегистрированного роутера:
   ```bash
   docker exec headscale headscale nodes list
   ```
3. Одобрите передачу маршрутов для данного узла:
   ```bash
   docker exec headscale headscale nodes approve-routes -i ID_РОУТЕРА -r 172.20.0.0/16
   ```

**Действия на VPS (Подключение самого VPS к сети):**
1. Установите клиент Tailscale на VPS:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --login-server https://vpn.ваш_домен.ru --accept-routes --accept-dns=false
   ```
2. Настройте системный DNS-резолвер для разрешения домена `.internal`:
   ```bash
   sudo apt install systemd-resolved -y
   sudo systemctl enable --now systemd-resolved
   sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
   
   sudo resolvectl dns tailscale0 172.20.0.1
   sudo resolvectl domain tailscale0 internal
   sudo resolvectl flush-caches
   ```
   *(Для проверки выполните команду `resolvectl query service.yadr00.internal`. Она должна вернуть IP-адрес из вашей домашней подсети).*

**Настройка завершена.** Сетевая инфраструктура готова к работе с клиентскими устройствами. Подключение новых клиентов осуществляется аналогичным образом через приложение Tailscale с указанием адреса `https://vpn.ваш_домен.ru`.