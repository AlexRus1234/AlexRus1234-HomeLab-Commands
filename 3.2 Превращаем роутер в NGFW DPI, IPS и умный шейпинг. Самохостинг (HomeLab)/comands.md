---

# Превращение маршрутизатора в шлюз NGFW: гибридный DNS, адаптивный шейпинг, DPI и активная защита (Arch Linux)

Настоящая инструкция описывает второй этап построения маршрутизатора: превращение настроенного в предыдущей части хоста в универсальный шлюз границы сети (uCPE). Описаны контроль версий конфигурации `/etc`, цепочка разрешения имён из трёх звеньев, распределение прерываний и адаптивный шейпинг на алгоритме CAKE, средства внутреннего контроля сети, развёртывание анализаторов трафика с записью логов в оперативную память, активная защита на базе CrowdSec и конвейер доставки логов на сервер хранения.

Исходные данные (назначение интерфейсов задано в предыдущей части):

* `enp6s0` - **WAN** (внешняя сеть / Интернет).
* `enp5s0` - **LAN** (основная локальная сеть, подсеть `172.20.0.0/16`).
* `enp3s0` - **IoT** (изолированная сеть устройств, подсеть `172.22.0.0/16`).

Целевая схема разрешения имён:

```
Клиент -> AdGuard Home (порт 53) -> dnsmasq (порт 5353) -> Unbound (порт 5335) -> Интернет
```

---

## Этап 0. Страховка конфигурации (Etckeeper)

Каталог `/etc` превращается в Git-репозиторий: любое изменение конфигурации фиксируется, появляется возможность откатить сломанные настройки на предыдущее рабочее состояние.

**1.** Установите пакет:

```bash
sudo pacman -S etckeeper
```

**2.** Проверьте, что в качестве системы контроля версий используется Git:

```bash
sudo nano /etc/etckeeper/etckeeper.conf
```

```ini
VCS="git"
# Остальные параметры можно не изменять
```

**3.** Инициализируйте репозиторий в каталоге `/etc`:

```bash
sudo etckeeper init
```

**4.** Зафиксируйте исходное состояние системы (создаётся скрытая папка `/etc/.git`):

```bash
sudo etckeeper commit "com 1: base (dnsmasq+nftables)"
```

**5.** Проверка: убедитесь, что рабочее дерево чистое.

```bash
sudo etckeeper vcs status
```

*Ожидаемый вывод: `On branch master. nothing to commit, working tree clean`.*

Дальнейшее восстановление испорченного конфига выполняется командой `sudo etckeeper vcs checkout /etc/dnsmasq.conf`. Коммиты также создаются автоматически при каждой операции `pacman`.

---

## Этап 1. Перестройка DNS (цепочка из трёх звеньев)

Сейчас в системе работает только `dnsmasq` на порту 53. Задача этапа: собрать цепочку, в которой фильтрацией занимается AdGuard Home, работой с устройствами и именами - dnsmasq, а рекурсивным разрешением - Unbound. Сборка идёт от конца цепочки к началу, чтобы не потерять связь с внешней сетью на промежуточных шагах.

### Шаг 1.1. Установка Unbound (рекурсивный резолвер)

Локальный резолвер заменяет внешние DNS-серверы и работает в режиме Hyperlocal: корневая зона загружается в кэш роутера, поэтому провайдер не видит запрашиваемые доменные имена.

**1.** Установите пакеты:

```bash
sudo pacman -S unbound expat
```

*(пакет `expat` необходим для работы DNSSEC и доверенных якорей)*

**2.** Сохраните штатный конфиг и создайте новый:

```bash
sudo mv /etc/unbound/unbound.conf /etc/unbound/unbound.conf.bak
sudo nano /etc/unbound/unbound.conf
```

**3.** Внесите конфигурацию локального резолвера на порту 5335:

```yaml
server:
    verbosity: 1
    interface: 127.0.0.1
    port: 5335
    do-ip4: yes
    do-udp: yes
    do-tcp: yes

    # Разрешаем запросы только от локалхоста (от dnsmasq)
    access-control: 127.0.0.0/8 allow

    # Прячем версию софта (безопасность)
    hide-identity: yes
    hide-version: yes

    # Оптимизация (префетчинг для скорости)
    prefetch: yes
    prefetch-key: yes

    # Путь к корневым сертификатам
    tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt
```

**4.** Запустите службу:

```bash
sudo systemctl enable --now unbound
```

**5.** Проверка: выполните запрос напрямую к резолверу.

```bash
dig -p 5335 google.com @127.0.0.1
```

*Ожидаемый результат: раздел `ANSWER SECTION` с IP-адресом. Если утилита `dig` отсутствует, установите её: `sudo pacman -S bind`.*

### Шаг 1.2. Перевод dnsmasq на порт 5353

Порт 53 освобождается под AdGuard Home, а `dnsmasq` переводится на нестандартный порт и получает в качестве единственного источника ответов локальный Unbound.

**1.** Откройте конфигурацию:

```bash
sudo nano /etc/dnsmasq.conf
```

**2.** Задайте новый порт в начале файла:

```ini
port=5353
```

**3.** Удалите все внешние DNS-серверы из конфигурации:

```ini
# server=8.8.8.8
# server=8.8.4.4
# server=1.1.1.1
```

**4.** Укажите единственный upstream - локальный Unbound:

```ini
server=127.0.0.1#5335
```

**5.** Перезапустите службу и проверьте ответ:

```bash
sudo systemctl restart dnsmasq
dig -p 5353 google.com @127.0.0.1
```

*Ответ должен содержать тот же IP-адрес, что и в предыдущей проверке: цепочка dnsmasq -> Unbound работает.*

### Шаг 1.3. Установка AdGuard Home (фильтрация и статистика)

**1.** Для загрузки пакетов временно верните внешний резолвер (порт 53 ещё не занят новым сервером):

```bash
sudo nano /etc/resolv.conf
```

```ini
nameserver 8.8.8.8
```

```bash
ping google.com
```

**2.** Установите AdGuard Home:

```bash
sudo pacman -S adguardhome
```

*(при отсутствии пакета в репозитории используется AUR: `yay -S adguardhome`)*

**3.** Запустите службу:

```bash
sudo systemctl enable --now adguardhome
sudo systemctl status adguardhome
```

*В логе службы выводится адрес веб-интерфейса вида `http://<IP>:3000`.*

**4.** Откройте в браузере `http://<IP-РОУТЕРА>:3000` и пройдите мастер первоначальной настройки:

* **Интерфейс администрирования:** сетевой интерфейс `Все интерфейсы`, порт `3000` (порт 80 оставьте свободным для будущих веб-сервисов).
* **DNS-сервер:** сетевой интерфейс `Все интерфейсы`, порт `53` (он освобождён на шаге 1.2).
* Создайте логин и пароль администратора.

### Шаг 1.4. Замыкание цепочки (upstream AdGuard)

По умолчанию AdGuard Home использует внешние серверы, предложенные при установке. Их необходимо заменить локальным `dnsmasq`.

**1.** Перейдите в **Настройки** -> **Настройки DNS**.

**2.** В поле **Upstream DNS-серверы** удалите все записи и укажите:

```text
127.0.0.1:5353
```

**3.** В поле **Bootstrap DNS-серверы** также укажите:

```text
127.0.0.1:5353
```

**4.** Включите опцию **Использовать приватные reverse DNS-серверы** и укажите в ней тот же адрес - это позволит видеть в журнале имена локальных устройств вместо IP-адресов:

```text
127.0.0.1:5353
```

**5.** Нажмите **Тест upstream-серверов**, дождитесь сообщения об успешной проверке и примените настройки кнопкой **Применить**.

### Шаг 1.5. Возврат системного резолвера роутера

**1.** Верните локальный адрес в конфигурацию резолвера:

```bash
sudo nano /etc/resolv.conf
```

```ini
nameserver 127.0.0.1
options edns0 trust-ad
```

**2.** Проверка:

```bash
ping google.com
```

*В журнале запросов AdGuard Home (**Query Log**) должна появиться запись `google.com` от клиента `127.0.0.1`.*

**3.** Зафиксируйте состояние конфигурации:

```bash
sudo etckeeper commit "com 3: AdGuard installed, DNS chain complete"
```

---

## Этап 2. Производительность: распределение прерываний и адаптивный шейпинг

При скорости выше гигабита обработка пакетов на одном ядре и буферизация на стороне провайдера приводят к росту задержки. Этап решает обе задачи: прерывания распределяются по ядрам, а управление очередями переносится на роутер.

### Шаг 2.1. irqbalance (распределение нагрузки)

**1.** Установите пакет:

```bash
sudo pacman -S irqbalance
```

**2.** Запустите службу:

```bash
sudo systemctl enable --now irqbalance
```

*Дополнительная настройка не требуется: служба автоматически распределяет прерывания сетевых карт по ядрам процессора.*

### Шаг 2.2. Скрипт Smart Shaper

Готовый текст скрипта приложен отдельным файлом `smart-shaper.py` рядом с этой инструкцией. Скрипт измеряет задержку до шлюза провайдера и подстраивает полосу пропускания: при превышении порога лага лимит снижается на 10 процентов, при нормальной задержке раз в 30 секунд выполняется попытка повышения на 1 процент. Ограничение очередей возлагается на алгоритм CAKE, для входящего трафика используется виртуальный интерфейс `ifb0`.

**1.** Установите Python (если отсутствует):

```bash
sudo pacman -S python
```

**2.** Создайте файл скрипта и вставьте в него текст из приложенного файла `smart-shaper.py`:

```bash
sudo nano /opt/smart-shaper.py
```

*Параметры в начале файла подбираются под канал: `WAN_IFACE` - имя внешнего интерфейса, `MAX_SPEED` - тариф с запасом (около 95 процентов полосы), `MIN_SPEED` - нижняя граница, `PING_THRESH` - порог лага в миллисекундах.*

**3.** Сделайте скрипт исполняемым:

```bash
sudo chmod +x /opt/smart-shaper.py
```

### Шаг 2.3. Автозагрузка (Systemd)

**1.** Создайте файл службы:

```bash
sudo nano /etc/systemd/system/smart-shaper.service
```

**2.** Внесите содержимое:

```ini
[Unit]
Description=Smart CAKE Shaper
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/smart-shaper.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**3.** Запустите службу:

```bash
sudo systemctl enable --now smart-shaper
```

**4.** Проверка: посмотрите журнал службы.

```bash
journalctl -u smart-shaper -f
```

*Признак корректной работы: строки вида `Ping: ... | Speed: ...`. Если WAN-интерфейс не подключён, шейпер запускается, но регулировка не выполняется.*

**5.** Зафиксируйте состояние:

```bash
sudo etckeeper commit "com 4: irqbalance + custom smart-shaper"
```

---

## Этап 3. Внутренний контроль сети

### Шаг 3.1. lldpd (топология сети)

Демон видит соседей по сети (коммутаторы, точки доступа, серверы), поддерживающих LLDP или CDP, и позволяет определить, в какой порт коммутатора включён роутер.

**1.** Установите пакет:

```bash
sudo pacman -S lldpd
```

**2.** Запустите службу:

```bash
sudo systemctl enable --now lldpd
```

**3.** Проверка:

```bash
lldpcli show neighbors
```

*При прямом подключении к ПК или неуправляемому коммутатору список может быть пустым.*

### Шаг 3.2. Arpwatch (контроль пар IP-MAC)

Служба отслеживает соответствие IP- и MAC-адресов устройств. Смена MAC-адреса у известного адреса (подмена ARP) или появление нового устройства фиксируются в журнале.

**1.** Установите пакет:

```bash
sudo pacman -S arpwatch
```

**2.** Создайте экземпляр службы для локального интерфейса:

```bash
sudo cp /usr/lib/systemd/system/arpwatch@.service /etc/systemd/system/arpwatch@enp5s0.service
```

*(если файла нет, его расположение уточняется командой `sudo pacman -Ql arpwatch`)*

**3.** Запустите службу:

```bash
sudo systemctl enable --now arpwatch@enp5s0
```

*Для сети IoT выполняется аналогично: `sudo systemctl enable --now arpwatch@enp3s0`.*

**4.** Проверка:

```bash
journalctl -u arpwatch@enp5s0 -f
```

*При подключении нового устройства появляется запись `new station`.*

### Шаг 3.3. iperf3 (замер скорости)

Постоянно работающий сервер замера скорости позволяет проверять пропускную способность с любого клиента сети.

**1.** Установите пакет:

```bash
sudo pacman -S iperf3
```

**2.** Создайте файл службы (в пакете он отсутствует):

```bash
sudo nano /etc/systemd/system/iperf3.service
```

```ini
[Unit]
Description=iperf3 server
After=network.target

[Service]
ExecStart=/usr/bin/iperf3 -s
Restart=always
User=nobody

[Install]
WantedBy=multi-user.target
```

**3.** Запустите службу:

```bash
sudo systemctl enable --now iperf3
```

**4.** Проверка: с клиентского устройства выполните замер в обе стороны (указан порт из конфигурации службы):

```bash
iperf3 -c 172.20.0.1 -p 5200
iperf3 -c 172.20.0.1 -p 5200 -R
```

*Флаг `-R` переключает направление: данные передаёт сервер. Результат около 2.1-2.8 Гбит/с соответствует физическому пределу порта 2.5G с учётом служебного трафика.*

### Шаг 3.4. Node Exporter (метрики железа)

Служба отдаёт метрики процессора, памяти, температуры и сети для последующей визуализации в Grafana.

**1.** Установите пакет:

```bash
sudo pacman -S prometheus-node-exporter
```

**2.** Запустите службу:

```bash
sudo systemctl enable --now prometheus-node-exporter
```

**3.** Проверка: откройте в браузере `http://<IP-РОУТЕРА>:9100/metrics`.

*Страница должна содержать набор метрик в текстовом виде.*

Фиксация этапа:
```bash
sudo etckeeper commit "com 5: ops tools (lldp, arpwatch, iperf, node-exporter)"
```

---

## Этап 4. Анализ трафика: Zeek и Suricata

Оба анализатора работают с копией трафика и пишут логи в оперативную память, чтобы не изнашивать накопитель роутера. Развёртывание выполняется в контейнерах: пакеты анализаторов в системе не устанавливаются, зависимости не засоряют хост.

### Шаг 4.1. Подготовка RAM-диска для логов

**1.** Создайте точку монтирования:

```bash
sudo mkdir -p /var/log/ids-ram
```

**2.** Добавьте запись в `/etc/fstab`, чтобы диск создавался при загрузке:

```bash
echo "tmpfs /var/log/ids-ram tmpfs defaults,noatime,size=512M 0 0" | sudo tee -a /etc/fstab
```

*(размер 512 МБ достаточен: конвейер Vector забирает и освобождает данные)*

**3.** Смонтируйте диск и перечитайте конфигурацию системы:

```bash
sudo systemctl daemon-reload
sudo mount /var/log/ids-ram
```

**4.** Проверка:

```bash
mount | grep ids-ram
```

### Шаг 4.2. Установка Docker

**1.** Установите пакеты:

```bash
sudo pacman -S docker docker-compose
```

**2.** Запустите службу:

```bash
sudo systemctl enable --now docker
```

**3.** Добавьте пользователя в группу `docker` (чтобы не указывать `sudo` в каждой команде):

```bash
sudo usermod -aG docker $USER
```

*(изменение вступает в силу после повторного входа в сессию; до этого команды выполняются через `sudo`)*

### Шаг 4.3. Zeek (сетевая телеметрия)

**1.** Создайте каталог проекта:

```bash
mkdir -p ~/uCPE/zeek
cd ~/uCPE/zeek
```

**2.** Создайте файл конфигурации:

```bash
nano docker-compose.yml
```

```yaml
version: '3'
services:
  zeek:
    image: zeek/zeek:lts
    container_name: zeek
    restart: always
    network_mode: host
    cap_add:
      - NET_ADMIN
      - NET_RAW
    volumes:
      # Логи пишутся в RAM-диск
      - /var/log/ids-ram/zeek:/zeek/logs
    command: zeek -i enp5s0 -C
```

*Параметр `network_mode: host` даёт контейнеру доступ к реальным интерфейсам роутера, флаг `-C` отключает проверку контрольных сумм (необходимо при аппаратном оффлоадинге). Контейнерный Zeek допускает только один интерфейс в опции `-i`: при указании второго он завершается с ошибкой `Only a single interface option (-i) is allowed`, поэтому контролируется только LAN.*

**3.** Запустите контейнер:

```bash
sudo docker compose up -d
```

**4.** Проверка: в логах контейнера должно быть сообщение о прослушивании интерфейса, а в RAM-диске - файлы журналов.

```bash
sudo docker logs zeek
ls -l /var/log/ids-ram/zeek
```

*Ожидаемые файлы: `conn.log`, `dns.log`, `http.log`, `ssl.log`.*

### Шаг 4.4. Suricata (система обнаружения вторжений)

**1.** Перейдите в каталог проекта и создайте подкаталог:

```bash
cd ~/uCPE
mkdir suricata
cd suricata
```

**2.** Создайте файл конфигурации:

```bash
nano docker-compose.yml
```

```yaml
version: '3'
services:
  suricata:
    image: jasonish/suricata:latest
    container_name: suricata
    restart: always
    network_mode: host
    cap_add:
      - NET_ADMIN
      - NET_RAW
      - SYS_NICE
    volumes:
      # Логи в RAM
      - /var/log/ids-ram/suricata:/var/log/suricata
      # Конфигурация и правила (сохраняются при пересоздании контейнера)
      - ./etc:/etc/suricata
      - ./lib:/var/lib/suricata
    command: -i enp5s0
```

**3.** Запустите контейнер:

```bash
sudo docker compose up -d
```

**4.** Обновите базы сигнатур (в образе есть утилита обновления):

```bash
sudo docker exec -it suricata suricata-update
```

**5.** Проверка:

```bash
ls -l /var/log/ids-ram/suricata
```

*Ожидаемые файлы: `eve.json`, `fast.log`, `stats.log`, `suricata.log`. Основной поток событий - `eve.json` в формате JSON.*

### Шаг 4.5. Альтернативный вариант: установка из системных пакетов

Если контейнеры не используются, анализаторы устанавливаются в систему напрямую.

**Zeek:**

**1.** Установите пакет:

```bash
sudo pacman -S zeek
```

**2.** Задайте интерфейс прослушивания:

```bash
sudo nano /etc/zeek/node.cfg
```

```ini
[zeek]
type=standalone
host=localhost
interface=enp5s0
```

**3.** Перенесите каталог логов в RAM-диск:

```bash
sudo nano /etc/zeek/zeekctl.cfg
```

```ini
LogDir = /var/log/ids-ram/zeek
```

**4.** Создайте каталог и разверните конфигурацию:

```bash
sudo mkdir -p /var/log/ids-ram/zeek
sudo zeekctl deploy
```

**5.** Создайте файл службы:

```bash
sudo nano /etc/systemd/system/zeek.service
```

```ini
[Unit]
Description=Zeek Network Security Monitor
After=network.target

[Service]
Type=forking
ExecStart=/usr/bin/zeekctl deploy
ExecStop=/usr/bin/zeekctl stop

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now zeek
```

**Suricata:**

**1.** Установите пакет и обновите правила:

```bash
sudo pacman -S suricata
sudo suricata-update
```

**2.** Отредактируйте конфигурацию:

```bash
sudo nano /etc/suricata/suricata.yaml
```

Изменения в файле:

* в секции `af-packet` укажите интерфейс `enp5s0`;
* каталог логов переведите в оперативную память: `default-log-dir: /var/log/ids-ram/suricata`;
* в секции `outputs` оставьте включённым только `eve-log`, вывод `fast.log` и `stats.log` отключите.

**3.** Создайте каталог и запустите службу:

```bash
sudo mkdir -p /var/log/ids-ram/suricata
sudo systemctl enable --now suricata
```

**4.** Проверка:

```bash
ls -l /var/log/ids-ram/suricata/
```

---

## Этап 5. Активная защита (CrowdSec)

CrowdSec читает журналы анализаторов, распознаёт атаки по сценариям и блокирует адреса нарушителей через `nftables`. Параллельно загружаются коллективные базы адресов, замеченных в атаках на других узлах.

### Шаг 5.1. Установка CrowdSec и баунсера

**1.** Установите зависимости и сам сервис:

```bash
sudo pacman -S go
yay -S crowdsec
```

**2.** Установите баунсер (связка сервиса с межсетевым экраном):

```bash
yay -S crowdsec-firewall-bouncer
```

*В AUR предлагается три варианта пакета. Для системы на `nftables` выбирается `crowdsec-firewall-bouncer-nftables`. Пакет `iptables`-варианта ставит правила в `iptables`, и блокировки не будут видны в `nft list ruleset`.*

### Шаг 5.2. Подключение источника событий

**1.** Откройте конфигурацию источников:

```bash
sudo nano /etc/crowdsec/acquis.yaml
```

**2.** Добавьте в конец файла поток событий Suricata:

```yaml
---
filename: /var/log/ids-ram/suricata/eve.json
labels:
  type: suricata
```

**3.** Запустите службы:

```bash
sudo systemctl enable --now crowdsec
sudo systemctl enable --now crowdsec-firewall-bouncer
```

### Шаг 5.3. Настройка баунсера

**1.** Откройте конфигурацию баунсера:

```bash
sudo nano /etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml
```

**2.** Проверьте режим работы межсетевого экрана в начале файла:

```yaml
mode: nftables
```

**3.** Перезапустите службу и проверьте статус:

```bash
sudo systemctl restart crowdsec-firewall-bouncer
sudo systemctl status crowdsec-firewall-bouncer
```

### Шаг 5.4. Коллекции правил и проверка блокировки

**1.** Установите коллекцию для разбора событий Suricata:

```bash
sudo cscli collections install crowdsecurity/suricata
sudo systemctl reload crowdsec
```

**2.** Проверка поступления событий:

```bash
sudo cscli metrics
```

*В таблице `Parser Metrics` появляется строка `crowdsecurity/non-syslog` со значением `Hits` больше нуля: события из журнала Suricata разбираются.*

**3.** **Проверка блокировки.** Добавьте тестовое решение и убедитесь, что адрес попал в правила межсетевого экрана:

```bash
sudo cscli decisions add --ip 1.2.3.4 --duration 1h --reason "Test ban"
sudo nft list ruleset | grep 1.2.3.4
```

*Ожидаемый вывод содержит строку вида `1.2.3.4 timeout 56m13s expires 56m9s616ms`. Пустой вывод означает, что баунсер работает в неправильном режиме (см. шаг 5.3).*

**4.** Удалите тестовое решение, чтобы не блокировать нужный адрес:

```bash
sudo cscli decisions delete --ip 1.2.3.4
```

**5.** Зафиксируйте состояние:

```bash
sudo etckeeper commit "com 6: Security Layer (Zeek+Suricata+CrowdSec)"
```

---

## Этап 6. Конвейер логов (Vector)

Сервер хранения к моменту настройки ещё не подключён к сети, поэтому данные направляются в пустой сток (`blackhole`): конвейер читает журналы из оперативной памяти и показывает статистику потока, не записывая ничего на накопитель. Подключение внешней базы выполняется заменой стока.

### Шаг 6.1. Установка

Используется бинарная сборка из AUR: компиляция из исходников на Rust занимает несоизмеримо больше времени.

```bash
yay -S vector-bin
```

### Шаг 6.2. Права доступа

Служба работает от пользователя `vector`, журналы анализаторов создаются от `root`. Чтение каталога в оперативной памяти необходимо разрешить явно.

```bash
sudo chmod -R 755 /var/log/ids-ram
```

### Шаг 6.3. Конфигурация

**1.** Подготовьте файл конфигурации:

```bash
sudo mkdir -p /etc/vector
sudo touch /etc/vector/vector.toml
sudo chmod 644 /etc/vector/vector.toml
```

**2.** Откройте файл:

```bash
sudo nano /etc/vector/vector.toml
```

**3.** Внесите конфигурацию:

```toml
data_dir = "/var/lib/vector"

# === ИСТОЧНИКИ (INPUTS) ===

# Читаем Suricata из RAM
[sources.in_suricata]
type = "file"
include = ["/var/log/ids-ram/suricata/eve.json"]
ignore_older_secs = 600

# Читаем Zeek из RAM
[sources.in_zeek]
type = "file"
include = ["/var/log/ids-ram/zeek/current/*.log"]
ignore_older_secs = 600

# === СТОК (SINKS) ===

# Отправляем в никуда, выводя статистику раз в 5 секунд
[sinks.out_blackhole]
type = "blackhole"
inputs = ["in_suricata", "in_zeek"]
print_interval_secs = 5
```

### Шаг 6.4. Запуск и проверка

**1.** Запустите службу:

```bash
sudo systemctl enable --now vector
```

**2.** Проверка: следите за журналом службы.

```bash
journalctl -u vector -f
```

*Раз в 5 секунд появляются строки вида `... events_processed=150 ...`: конвейер читает журналы из памяти, обрабатывает их и готов к отправке на внешнее хранилище.*

---

## Проверка итогового состояния

**1.** Цепочка разрешения имён отвечает на всех звеньях:

```bash
dig -p 5335 google.com @127.0.0.1
dig -p 5353 google.com @127.0.0.1
dig google.com @127.0.0.1
```

**2.** Очереди шейпера созданы на внешнем и виртуальном интерфейсах:

```bash
tc qdisc show dev enp6s0
tc qdisc show dev ifb0
```

**3.** Службы запущены и добавлены в автозагрузку:

```bash
systemctl is-active unbound dnsmasq adguardhome irqbalance smart-shaper \
  lldpd arpwatch@enp5s0 iperf3 prometheus-node-exporter docker \
  crowdsec crowdsec-firewall-bouncer vector
```

**4.** Логи анализаторов пишутся в оперативную память:

```bash
ls -l /var/log/ids-ram/zeek /var/log/ids-ram/suricata
```

**5.** CrowdSec разбирает события и управляет блокировками:

```bash
sudo cscli metrics
sudo cscli decisions list
```

**6.** История изменений конфигурации доступна для отката:

```bash
sudo etckeeper vcs log --oneline
```
