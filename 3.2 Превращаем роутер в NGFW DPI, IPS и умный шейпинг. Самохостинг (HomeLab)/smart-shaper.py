#!/usr/bin/env python3
import subprocess
import time
import re

# === НАСТРОЙКИ ===
WAN_IFACE = "enp6s0"       # Твой внешний порт
IFB_IFACE = "ifb0"         # Виртуальный порт для Download
MAX_SPEED = 800.0          # Мбит/с
MIN_SPEED = 30.0           # Мбит/с
PING_THRESH = 50.0         # Порог лага (мс)

# Интервалы
CHECK_INTERVAL = 10        # Частота проверки (сек)
INCREASE_INTERVAL = 30     # Как часто повышать скорость (сек)

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
current_up = MAX_SPEED
current_down = MAX_SPEED
last_increase_time = time.time()

def run(cmd):
    subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_gateway():
    try:
        res = subprocess.check_output("ip route show default", shell=True).decode()
        match = re.search(r"default via ([\d\.]+)", res)
        return match.group(1) if match else "8.8.8.8"
    except:
        return "8.8.8.8"

def get_ping(target):
    try:
        cmd = f"ping -c 3 -W 1 -i 0.2 {target}"
        res = subprocess.check_output(cmd, shell=True).decode()
        match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", res)
        return float(match.group(1)) if match else 999.0
    except:
        return 999.0

def apply_limits(up_mb, down_mb):
    # Upload
    run(f"tc qdisc change dev {WAN_IFACE} root cake bandwidth {int(up_mb)}mbit")
    # Download
    run(f"tc qdisc change dev {IFB_IFACE} root cake bandwidth {int(down_mb)}mbit")
    print(f"[SHAPER] Applied: UP={up_mb:.1f}Mb | DOWN={down_mb:.1f}Mb")

def init_networking():
    print("[INIT] Setting up IFB and TC...")
    run(f"modprobe ifb")
    run(f"ip link set dev {IFB_IFACE} up")

    # Чистка
    run(f"tc qdisc del dev {WAN_IFACE} root")
    run(f"tc qdisc del dev {WAN_IFACE} ingress")
    run(f"tc qdisc del dev {IFB_IFACE} root")

    # Upload: CAKE на WAN
    run(f"tc qdisc add dev {WAN_IFACE} root handle 1: cake bandwidth {int(MAX_SPEED)}mbit besteffort nat")

    # Download: Redirect на IFB
    run(f"tc qdisc add dev {WAN_IFACE} handle ffff: ingress")
    run(f"tc filter add dev {WAN_IFACE} parent ffff: protocol ip u32 match u32 0 0 action mirred egress redirect dev {IFB_IFACE}")

    # Download: CAKE на IFB
    run(f"tc qdisc add dev {IFB_IFACE} root handle 1: cake bandwidth {int(MAX_SPEED)}mbit besteffort nat")

# === MAIN ===
init_networking()
gateway = get_gateway()
print(f"[INFO] Gateway: {gateway}")

while True:
    latency = get_ping(gateway)
    now = time.time()
    print(f"Ping: {latency}ms | Speed: {current_down:.1f}Mb")

    if latency > PING_THRESH:
        print("LAG! Dropping 10%...")
        current_up = max(MIN_SPEED, current_up * 0.90)
        current_down = max(MIN_SPEED, current_down * 0.90)
        apply_limits(current_up, current_down)
        time.sleep(1) # Пауза перед следующим замером
    else:
        if (now - last_increase_time) > INCREASE_INTERVAL:
            if current_up < MAX_SPEED:
                print("Probing +1%...")
                prev_up, prev_down = current_up, current_down
                target_up = min(MAX_SPEED, current_up * 1.01)
                target_down = min(MAX_SPEED, current_down * 1.01)

                apply_limits(target_up, target_down)
                time.sleep(2)

                if get_ping(gateway) > PING_THRESH:
                    print("Failed. Reverting.")
                    current_up, current_down = prev_up, prev_down
                    apply_limits(current_up, current_down)
                else:
                    print("Success.")
                    current_up, current_down = target_up, target_down

                last_increase_time = now

        time.sleep(CHECK_INTERVAL)
