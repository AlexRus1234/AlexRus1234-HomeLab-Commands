import serial
import time

# --- НАСТРОЙКИ ---
ser = serial.Serial(
    "/dev/ttyUSB0",
    9600,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0.1
)

def make_packet(msg_type, tgt_addr, msg_id, msg_data):
    payload = bytearray()
    payload.append(msg_type)
    payload.append(tgt_addr)
    payload.extend(msg_id)
    payload.extend(msg_data)
    
    length = len(payload)
    l1, l2 = (length >> 8) & 0xFF, length & 0xFF
    raw = bytes([l1, l2]) + payload
    bcc = sum(raw) & 0xFF
    raw += bytes([bcc])
    
    stuffed = bytearray()
    for b in raw:
        if b == 0x02: stuffed.extend([0xFF, 0xF2])
        elif b == 0x03: stuffed.extend([0xFF, 0xF3])
        elif b == 0xFF: stuffed.extend([0xFF, 0xFF])
        else: stuffed.append(b)
        
    return b'\x02' + stuffed + b'\x03'

print(f"Opened {ser.port}")

try:
    captured_id = None
    
    # 1. Ждем, пока привод заговорит (он пока в Non-Polled, так что должен)
    print("Waiting for Drive to speak...")
    buffer = b""
    for _ in range(100):
        chunk = ser.read(64)
        if chunk:
            buffer += chunk
            if b'\xab\xff' in buffer: # Ищем Config Request
                idx = buffer.find(b'\xab\xff')
                if len(buffer) >= idx + 6:
                    captured_id = buffer[idx+2 : idx+6]
                    print(f"Captured ID: {captured_id.hex(' ')}")
                    break
        time.sleep(0.1)

    if not captured_id:
        print("Drive silent. Maybe already in Polled mode?")
        # Если он молчит, можно попробовать послать наугад, но лучше ребутнуть привод руками
        exit()

    # 2. Шлем ACK
    ser.write(b'\x06\x03')
    time.sleep(0.2)

    # 3. Шлем ФИНАЛЬНЫЙ КОНФИГ (как в инструкции)
    # Payload:
    # Ver(2) + Res(18) + SCSI(1) + Res(26) + Flags(1) + Res(10)
    
    # SCSI ADDR = 0x00 (Был 04, станет 0)
    # FLAGS     = 0x00 (Был 80, станет 0 - Polled Mode, Тихий режим)
    
    config_payload = b'\x02\x00' + \
                     (b'\x00' * 18) + \
                     b'\x00' + \
                     (b'\x00' * 26) + \
                     b'\x00' + \
                     (b'\x00' * 10)

    # Target Addr ставим 0x01 (как в байте AC 01 из твоей строки)
    packet = make_packet(0xAC, 0x01, captured_id, config_payload)
    
    print(f"TX > {packet.hex(' ')} (Set_Config: Addr 0, Polled Mode)")
    ser.write(packet)

    print("\n--- Config Sent. Drive should ACK and probably REBOOT. ---")
    print("NOTE: After this, the drive will be in POLLED MODE.")
    print("It will NOT send data automatically anymore.")
    
    while True:
        chunk = ser.read(64)
        if chunk:
            print(f"RX < {chunk.hex(' ')}")
        time.sleep(0.1)

except KeyboardInterrupt:
    print("Done")
finally:
    ser.close()