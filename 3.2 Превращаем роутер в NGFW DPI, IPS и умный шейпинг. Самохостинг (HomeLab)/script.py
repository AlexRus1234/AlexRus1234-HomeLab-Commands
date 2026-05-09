

```python
#!/usr/bin/env python3
import subprocess
import time
import re

# ================= НАСТРОЙКИ =================
# Внешний интерфейс (куда воткнут кабель провайдера)
WAN_IFACE = "enp6s0"
# Виртуальный интерфейс для входящего трафика (не менять)
IFB_IFACE = "ifb0"

# Лимиты скорости (в Мбит/с)
MAX_SPEED = 900.0   # Твой тариф (или чуть меньше)
MIN_SPEED = 50.0    # Минимум, ниже которого не опускаться

# Порог лага (в мс). Если пинг выше - сбрасываем скорость.
PING_THRESH = 50.0

# Как часто проверять сеть (сек)
CHECK_INTERVAL = 2
# Как часто повышать скорость при хорошем пинге (сек)
RECOVERY_INTERVAL = 10 

# ============================================

current_up = MAX_SPEED
current_down = MAX_SPEED
last_recovery_time = time.time()

def run_cmd(cmd):
    """Выполняет команду в bash"""
    subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_gateway():
    """Автоматически находит IP шлюза провайдера"""
    try:
        res = subprocess.check_output("ip route show default", shell=True).decode()
        match = re.search(r"default via ([\d\.]+)", res)
        return match.group(1) if match else "8.8.8.8"
    except:
        return "8.8.8.8"

def get_avg_ping(target):
    """Делает 3 быстрых пинга и возвращает среднее"""
    try:
        # -c 3 (три пакета), -W 1 (таймаут 1с), -i 0.2 (интервал 200мс)
        cmd = f"ping -c 3 -W 1 -i 0.2 {target}"
        res = subprocess.check_output(cmd, shell=True).decode()
        # Парсим вывод ping (rtt min/avg/max/mdev)
        match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", res)
        return float(match.group(1)) if match else 999.0
    except:
        return 999.0 # Если сеть лежит

def apply_limits(up_mb, down_mb):
    """Применяет новые скорости через TC CAKE"""
    # Upload (на WAN)
    run_cmd(f"tc qdisc change dev {WAN_IFACE} root cake bandwidth {int(up_mb)}mbit besteffort nat")
    # Download (на IFB)
    run_cmd(f"tc qdisc change dev {IFB_IFACE} root cake bandwidth {int(down_mb)}mbit besteffort nat")
    print(f"[SHAPER] Applied: UP={up_mb:.1f}Mb | DOWN={down_mb:.1f}Mb")

def init_network():
    """Первоначальная настройка интерфейсов и очередей"""
    print("[INIT] Loading IFB module and configuring TC...")
    run_cmd("modprobe ifb")
    run_cmd(f"ip link set dev {IFB_IFACE} up")
    
    # Сброс старых правил
    run_cmd(f"tc qdisc del dev {WAN_IFACE} root")
    run_cmd(f"tc qdisc del dev {WAN_IFACE} ingress")
    run_cmd(f"tc qdisc del dev {IFB_IFACE} root")

    # 1. Upload: Вешаем CAKE прямо на WAN
    run_cmd(f"tc qdisc add dev {WAN_IFACE} root handle 1: cake bandwidth {int(MAX_SPEED)}mbit besteffort nat")

    # 2. Download: Перенаправляем входящий трафик на IFB (Mirred)
    run_cmd(f"tc qdisc add dev {WAN_IFACE} handle ffff: ingress")
    run_cmd(f"tc filter add dev {WAN_IFACE} parent ffff: protocol ip u32 match u32 0 0 action mirred egress redirect dev {IFB_IFACE}")
    
    # 3. Download: Вешаем CAKE на виртуальный IFB
    run_cmd(f"tc qdisc add dev {IFB_IFACE} root handle 1: cake bandwidth {int(MAX_SPEED)}mbit besteffort nat")

# === ЗАПУСК ===
if __name__ == "__main__":
    init_network()
    gateway = get_gateway()
    print(f"[INFO] Gateway detected: {gateway}")
    print(f"[INFO] Monitoring latency... Threshold: {PING_THRESH}ms")

    while True:
        latency = get_avg_ping(gateway)
        now = time.time()
        
        # Для отладки в journalctl
        # print(f"Ping: {latency}ms | Speed: {current_down:.1f}Mb")

        if latency > PING_THRESH:
            # === СЦЕНАРИЙ: ЛАГ (Снижаем скорость) ===
            print(f" High Latency ({latency}ms)! Dropping speed...")
            
            # Резко снижаем на 10%
            current_up = max(MIN_SPEED, current_up * 0.90)
            current_down = max(MIN_SPEED, current_down * 0.90)
            
            apply_limits(current_up, current_down)
            
            # Ждем немного, чтобы буфер успел очиститься
            time.sleep(1) 
            
        else:
            # === СЦЕНАРИЙ: НОРМА (Повышаем скорость) ===
            if (now - last_recovery_time) > RECOVERY_INTERVAL:
                # Если мы еще не на максимуме
                if current_up < MAX_SPEED:
                    # Повышаем на 2%
                    current_up = min(MAX_SPEED, current_up * 1.02)
                    current_down = min(MAX_SPEED, current_down * 1.02)
                    
                    apply_limits(current_up, current_down)
                    last_recovery_time = now
            
        time.sleep(CHECK_INTERVAL)
```

### Как установить:

1.  Создать файл:
    ```bash
    sudo nano /opt/smart-shaper.py
    # (Вставить код)
    ```
2.  Сделать исполняемым:
    ```bash
    sudo chmod +x /opt/smart-shaper.py
    ```
3.  Для автозапуска используйте `systemd` юнит из списка команд выше.