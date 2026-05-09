---

# Настройка базовой маршрутизации на Arch Linux (systemd-networkd, nftables, dnsmasq)

---

## Исходные данные: Топология интерфейсов
Перед началом конфигурации зафиксируйте назначение сетевых портов:
*   `enp6s0` — **WAN** (Внешняя сеть / Интернет).
*   `enp5s0` — **LAN** (Основная локальная сеть, подсеть `172.20.0.0/16`).
*   `enp3s0` — **IoT** (Изолированная сеть устройств, подсеть `172.22.0.0/16`).
*   `enp1s0` — **Rescue** (Аварийный порт для прямого управления, подсеть `192.168.99.0/24`).

---

## Этап 1. Подготовка операционной системы

Необходимо установить требуемые пакеты и деактивировать службы, которые могут конфликтовать с ручной настройкой маршрутизации.

1. Выполните установку системных утилит:
   ```bash
   pacman -Syu systemd dnsmasq nftables ethtool chrony neovim
   ```
2. Удалите стандартные конфигурации сети (если они присутствуют), чтобы исключить конфликты:
   ```bash
   rm /etc/systemd/network/20-ethernet.network 2> /dev/null
   rm /etc/systemd/network/20-wlan.network 2> /dev/null
   ```
3. Отключите сторонние сетевые менеджеры и системный резолвер:
   ```bash
   systemctl disable --now NetworkManager dhcpcd systemd-resolved
   ```

---

## Этап 2. Конфигурация интерфейсов (systemd-networkd)

Настройте параметры каждого интерфейса. Создайте 4 конфигурационных файла в директории `/etc/systemd/network/`.

**1. Конфигурация WAN-интерфейса**
Откройте файл `20-wan.network`:
```bash
nano /etc/systemd/network/20-wan.network
```
Вставьте следующие параметры:
```ini
[Match]
Name=enp6s0

[Link]
# Замена MAC-адреса для идентификации у провайдера (опционально)
MACAddress=XX:XX:XX:XX:XX:XX

[Network]
DHCP=ipv4
IPMasquerade=ipv4
```

**2. Конфигурация LAN-интерфейса**
Откройте файл `30-lan.network` и добавьте конфигурацию:
```ini
[Match]
Name=enp5s0

[Network]
Address=172.20.0.1/16
DHCPServer=no
# Принудительное назначение IP-адреса при отсутствии физического линка
ConfigureWithoutCarrier=yes
```

**3. Конфигурация IoT-интерфейса**
Откройте файл `40-iot.network` и добавьте конфигурацию:
```ini
[Match]
Name=enp3s0

[Network]
Address=172.22.0.1/16
DHCPServer=no
ConfigureWithoutCarrier=yes
```

**4. Конфигурация аварийного порта (Rescue)**
Откройте файл `10-rescue.network` и добавьте конфигурацию:
```ini
[Match]
Name=enp1s0

[Network]
# Статический адрес для прямого подключения администратора
Address=192.168.99.1/24
DHCP=ipv4
```

---

## Этап 3. Активация IP-маршрутизации

Для того чтобы система функционировала как маршрутизатор, необходимо включить пересылку пакетов (IP Forwarding) на уровне ядра Linux.

1. Создайте конфигурационный файл:
   ```bash
   nano /etc/sysctl.d/99-router.conf
   ```
2. Добавьте следующий параметр:
   ```ini
   net.ipv4.ip_forward = 1
   ```
3. Примените изменения без перезагрузки системы:
   ```bash
   sysctl --system
   ```

---

## Этап 4. Настройка DHCP и DNS (dnsmasq)

Необходимо освободить порт 53 и настроить локальный DNS- и DHCP-сервер.

1. Удалите символическую ссылку системного резолвера и создайте базовый файл конфигурации:
   ```bash
   rm /etc/resolv.conf
   echo "nameserver 8.8.8.8" > /etc/resolv.conf
   ```
2. Откройте конфигурационный файл `dnsmasq`:
   ```bash
   nano /etc/dnsmasq.conf
   ```
3. Вставьте следующую конфигурацию:
   ```ini
   # Исключение прослушивания на WAN-интерфейсе
   except-interface=enp6s0
   
   # Прослушивание локальных интерфейсов
   interface=enp5s0
   interface=enp3s0
   interface=enp1s0

   # Базовые параметры DNS
   domain-needed
   bogus-priv
   no-resolv
   server=8.8.8.8
   server=1.1.1.1

   # Настройка пула адресов LAN (172.20.0.0/16)
   dhcp-range=interface:enp5s0,172.20.255.10,172.20.255.250,255.255.0.0,12h
   domain=lan,172.20.0.0/16
   expand-hosts

   # Настройка пула адресов IoT (172.22.0.0/16)
   dhcp-range=interface:enp3s0,172.22.0.10,172.22.0.250,255.255.0.0,12h

   # Настройка пула адресов Rescue (192.168.99.0/24)
   dhcp-range=interface:enp1s0,192.168.99.10,192.168.99.20,255.255.255.0,1h
   ```

---

## Этап 5. Настройка межсетевого экрана и NAT (nftables)

Создайте правила для трансляции сетевых адресов и фильтрации трафика.

1. Откройте файл конфигурации межсетевого экрана:
   ```bash
   nano /etc/nftables.conf
   ```
2. Приведите содержимое файла к следующему виду:
   ```nft
   flush ruleset

   table ip nat {
       chain postrouting {
           type nat hook postrouting priority 100; policy accept;
           # Включение трансляции адресов (Masquerade) только для исходящего трафика WAN
           oifname "enp6s0" masquerade
       }
   }

   table inet filter {
       chain input {
           type filter hook input priority 0; policy drop;

           # Разрешение установленных и связанных соединений
           ct state established,related accept
           
           # Разрешение локального трафика
           iifname "lo" accept
           
           # Разрешение ICMP (Ping)
           ip protocol icmp accept

           # Разрешение входящих подключений к маршрутизатору только из локальных сетей
           iifname { "enp5s0", "enp3s0", "enp1s0" } accept
       }
       chain forward {
           type filter hook forward priority 0; policy accept;
       }
   }
   ```

---

## Этап 6. Инициализация служб и проверка

Запустите настроенные сервисы и добавьте их в автозагрузку.

1. Выполните инициализацию служб:
   ```bash
   systemctl enable --now systemd-networkd
   systemctl enable --now nftables
   systemctl enable --now dnsmasq
   systemctl enable --now chronyd
   ```
2. **Проверка работоспособности:**
   * Проверьте назначение IP-адресов командой `ip a`. Адреса должны быть присвоены локальным интерфейсам даже при отсутствии физического подключения.
   * Проверьте состояние сетевых линков командой `networkctl status` (интерфейсы должны находиться в статусе `configured`).
   * Подключите клиентское устройство к порту **LAN** (`enp5s0`). Убедитесь, что оно получило IP-адрес из пула `172.20.255.x` и имеет доступ к внешней сети (Интернет).