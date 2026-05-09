
---

# Настройка узла баз данных: ZFS, Macvlan и установка СУБД

---

## Этап 1. Подготовка файловой системы ZFS

Для оптимизации работы накопителей и снижения износа SSD-кэша необходимо создать отдельные наборы данных (datasets) с индивидуальным размером блока (`recordsize`), соответствующим архитектуре конкретной СУБД.

1. Создайте родительский набор данных для баз данных:
   ```bash
   sudo zfs create tank/db
   ```
2. Отключите кэширование мелких блоков данных на SSD для созданного раздела (разрешается только запись метаданных):
   ```bash
   sudo zfs set special_small_blocks=0 tank/db
   ```
3. Создайте изолированные наборы данных для каждой службы:
   ```bash
   # PostgreSQL (Оптимальный размер блока транзакций — 16k)
   sudo zfs create -o recordsize=16k -o mountpoint=/var/lib/postgres tank/db/postgres

   # MongoDB (Движок WiredTiger оптимизирован под блок 64k)
   sudo zfs create -o recordsize=64k -o mountpoint=/var/lib/mongodb tank/db/mongodb

   # Valkey / In-memory кэш (Стандартный блок, работа происходит в RAM)
   sudo zfs create -o mountpoint=/var/lib/valkey tank/db/valkey

   # MinIO / S3 (Создается вне иерархии баз данных, блок 128k для крупных объектов)
   sudo zfs create -o mountpoint=/var/lib/minio tank/s3
   sudo zfs set mountpoint=/srv/minio/data tank/s3
   ```

---

## Этап 2. Конфигурация виртуальных сетевых интерфейсов (Macvlan)

Для изоляции баз данных на сетевом уровне `[L3]` каждой службе выделяется собственный виртуальный интерфейс со статическим IP и MAC-адресом.

1. Выполните команды для генерации файлов конфигурации `systemd-networkd`. Скопируйте блоки целиком в терминал:

   **PostgreSQL (IP: 172.20.50.10):**
   ```bash
   sudo tee /etc/systemd/network/20-macvlan-pg.netdev > /dev/null <<EOF
   [NetDev]
   Name=mv-pg
   Kind=macvlan
   MACAddress=02:17:22:05:00:10
   EOF

   sudo tee /etc/systemd/network/25-macvlan-pg.network > /dev/null <<EOF
   [Match]
   Name=mv-pg

   [Network]
   Address=172.20.50.10/16
   Gateway=172.20.0.1
   DNS=172.20.0.1
   EOF
   ```

   **MongoDB (IP: 172.20.50.11):**
   ```bash
   sudo tee /etc/systemd/network/20-macvlan-mongo.netdev > /dev/null <<EOF
   [NetDev]
   Name=mv-mongo
   Kind=macvlan
   MACAddress=02:17:22:05:00:11
   EOF

   sudo tee /etc/systemd/network/25-macvlan-mongo.network > /dev/null <<EOF
   [Match]
   Name=mv-mongo

   [Network]
   Address=172.20.50.11/16
   Gateway=172.20.0.1
   DNS=172.20.0.1
   EOF
   ```

   **Valkey (IP: 172.20.50.12):**
   ```bash
   sudo tee /etc/systemd/network/20-macvlan-valkey.netdev > /dev/null <<EOF
   [NetDev]
   Name=mv-valkey
   Kind=macvlan
   MACAddress=02:17:22:05:00:12
   EOF

   sudo tee /etc/systemd/network/25-macvlan-valkey.network > /dev/null <<EOF
   [Match]
   Name=mv-valkey

   [Network]
   Address=172.20.50.12/16
   Gateway=172.20.0.1
   DNS=172.20.0.1
   EOF
   ```

   **MinIO S3 (IP: 172.20.50.13):**
   ```bash
   sudo tee /etc/systemd/network/20-macvlan-s3.netdev > /dev/null <<EOF
   [NetDev]
   Name=mv-s3
   Kind=macvlan
   MACAddress=02:17:22:05:00:13
   EOF

   sudo tee /etc/systemd/network/25-macvlan-s3.network > /dev/null <<EOF
   [Match]
   Name=mv-s3

   [Network]
   Address=172.20.50.13/16
   Gateway=172.20.0.1
   DNS=172.20.0.1
   EOF
   ```

---

## Этап 3. Инициализация сетевых интерфейсов

Виртуальные интерфейсы необходимо привязать к физическому адаптеру сервера.

1. Добавьте директивы Macvlan в конфигурацию вашего основного сетевого интерфейса (замените `10-enp7s0.network` на имя вашего файла):
   ```bash
   echo -e "\nMACVLAN=mv-pg\nMACVLAN=mv-mongo\nMACVLAN=mv-valkey\nMACVLAN=mv-s3" | sudo tee -a /etc/systemd/network/10-enp7s0.network
   ```
2. Перезапустите сетевую службу:
   ```bash
   sudo systemctl restart systemd-networkd
   ```
3. Проверьте корректность создания интерфейсов:
   ```bash
   ip a | grep mv-
   ```
   *В выводе должны присутствовать 4 интерфейса с назначенными IP-адресами.*

---

## Этап 4. Установка и конфигурация PostgreSQL

СУБД будет прослушивать соединения только на выделенном виртуальном интерфейсе и интерфейсе прямого линка (Point-to-Point).

1. Установите пакет и инициализируйте кластер БД:
   ```bash
   sudo pacman -S postgresql
   sudo chown -R postgres:postgres /var/lib/postgres
   sudo -u postgres initdb -D /var/lib/postgres/data
   ```
2. Настройте сетевой доступ в конфигурационном файле:
   ```bash
   sudo sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '172.20.50.10, 172.21.6.255'/g" /var/lib/postgres/data/postgresql.conf
   ```
3. Добавьте правила авторизации `[scram-sha-256]` для заданных подсетей:
   ```bash
   echo "host    all             all             172.20.0.0/16           scram-sha-256" | sudo tee -a /var/lib/postgres/data/pg_hba.conf
   echo "host    all             all             172.21.6.254/31         scram-sha-256" | sudo tee -a /var/lib/postgres/data/pg_hba.conf
   ```
4. Запустите службу и создайте пользователя базы данных (на примере Gitea/Forgejo):
   ```bash
   sudo systemctl enable --now postgresql
   
   sudo -u postgres psql -c "CREATE USER ваш_пользователь WITH PASSWORD 'Ваш_Пароль';"
   sudo -u postgres psql -c "CREATE DATABASE ваша_бд OWNER ваш_пользователь;"
   ```

---

## Этап 5. Установка и конфигурация MongoDB

1. Установите необходимые пакеты из пользовательского репозитория (AUR) и задайте права:
   ```bash
   yay -S mongodb-bin mongodb-tools-bin
   sudo chown -R mongodb:mongodb /var/lib/mongodb
   ```
2. Отредактируйте конфигурационный файл сети:
   ```bash
   sudo nano /etc/mongodb.conf
   ```
   *В секции `net:` укажите выделенные IP-адреса:*
   ```yaml
   net:
     port: 27017
     bindIp: 172.20.50.11,172.21.6.255
   ```
3. Запустите службу и создайте пользователя базы данных через встроенную оболочку:
   ```bash
   sudo systemctl enable --now mongodb
   
   mongosh --host 172.20.50.11 --eval "db.getSiblingDB('ваша_бд').createUser({user: 'ваш_пользователь', pwd: 'Ваш_Пароль', roles: [{role: 'readWrite', db: 'ваша_бд'}]})"
   ```

---

## Этап 6. Установка и конфигурация Valkey (Кэш)

Valkey используется в качестве открытой альтернативы in-memory хранилищу Redis.

1. Установите пакет и задайте права на ранее созданный раздел ZFS:
   ```bash
   sudo pacman -S valkey
   sudo chown -R valkey:valkey /var/lib/valkey
   ```
2. Отредактируйте конфигурационный файл:
   ```bash
   sudo nano /etc/valkey/valkey.conf
   ```
   *Замените стандартный адрес прослушивания на выделенные IP (через пробел):*
   ```text
   bind 172.20.50.12 172.21.6.255
   ```
   *Раскомментируйте параметр пароля и задайте свой:*
   ```text
   requirepass Ваш_Пароль
   ```
3. Добавьте службу в автозагрузку и запустите:
   ```bash
   sudo systemctl enable --now valkey
   ```

---

## Этап 7. Установка и конфигурация MinIO (S3-совместимое хранилище)

MinIO обеспечивает работу с объектами через REST API, заменяя классические файловые протоколы.

1. Установите пакет из AUR и задайте права на корневой каталог данных:
   ```bash
   yay -S minio
   sudo chown -R minio:minio /srv/minio
   ```
2. Создайте и заполните файл переменных окружения:
   ```bash
   sudo nano /etc/minio/minio.conf
   ```
   *Вставьте конфигурацию, указав учетные данные администратора:*
   ```bash
   MINIO_ROOT_USER="admin"
   MINIO_ROOT_PASSWORD="Ваш_Сложный_Пароль"
   MINIO_VOLUMES="/srv/minio/data"
   MINIO_OPTS="--address 172.20.50.13:9000 --console-address 172.20.50.13:9001"
   ```
3. Ограничьте права доступа к файлу конфигурации и запустите службу:
   ```bash
   sudo chown minio:minio /etc/minio/minio.conf
   sudo chmod 600 /etc/minio/minio.conf
   
   sudo systemctl enable --now minio.service
   ```

**Готово!** Инфраструктура баз данных развернута. Сервисы изолированы, а ZFS сконфигурирована с учетом особенностей механизмов ввода-вывода каждой СУБД.