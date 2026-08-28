import network
import time
import json
import os
import machine
from machine import Pin, ADC
import onewire, ds18x20
import urequests
import ntptime   # NTP時刻同期用
import config    # config.py を読み込み

# ==================================================
# 1. システム設定・機体選択
# ==================================================
VERSION = "2.0.0-GAS"
DEVICE_NAME = config.DEVICE_NAME
GAS_URL = getattr(config, "GAS_URL", None)
BUFFER_FILE = "unsent_buffer.json"  # 未送信データ保持用ファイル
MAX_BUFFER_SIZE = 500              # 最大バッファ件数
INTERVAL_SEC = 300                  # 5分（300秒）周期

# --------------------------------------------------
# config.py から APリストを柔軟に自動取得
# --------------------------------------------------
AP_LIST = getattr(config, "WIFI_AP_LIST", None) or getattr(config, "AP_LIST", None)
if not AP_LIST:
    wifi_ssid = getattr(config, "WIFI_SSID", None)
    wifi_pass = getattr(config, "WIFI_PASS", None)
    if wifi_ssid and wifi_pass:
        AP_LIST = [{"ssid": wifi_ssid, "pass": wifi_pass}]
    else:
        AP_LIST = []

# --------------------------------------------------
# プロファイル設定（複数台並列運用の自動マッピング）
# --------------------------------------------------
DEVICE_PROFILES = {
    "保冷BOX本機": {
        "DEVICE_LABEL": "pico-w-main",
        "VSYS_COEFF": 0.00014793
    },
    "検証機": {
        "DEVICE_LABEL": "pico-2w-test",
        "VSYS_COEFF": 0.00014943  # Pico 2 W 専用キャリブレーション値
    }
}

active_profile = DEVICE_PROFILES["検証機"]
for name_key in DEVICE_PROFILES:
    if name_key in DEVICE_NAME:
        active_profile = DEVICE_PROFILES[name_key]
        break

DEVICE_LABEL = active_profile["DEVICE_LABEL"]
VSYS_COEFF = active_profile["VSYS_COEFF"]

# ピン配置
DS_PIN_NUM = 15     # GP15 (1-Wire データ線)
VSYS_ADC_NUM = 29   # GP29 (ADC3)
LED_PIN_NUM = "LED"

# ==================================================
# 2. ハードウェア初期化
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)

# ==================================================
# 3. ログ・センサー計測処理
# ==================================================
def log(msg, level="INFO"):
    print(f"[{DEVICE_NAME}] [{level}] {msg}")

def sync_ntp_time():
    ntptime.host = "ntp.nict.jp"
    for retry in range(3):
        try:
            ntptime.settime()
            log("⏰ NTP時刻同期に成功しました", "INFO")
            return True
        except Exception as e:
            time.sleep(1)
    log("⚠️ NTP時刻同期に失敗しました", "WARN")
    return False

def read_temperatures():
    try:
        roms = ds.scan()
        if not roms:
            log("警告: DS18B20未検出 -> 配線を確認してください", "WARN")
            return None, None
        
        roms = sorted(roms)
        ds.convert_temp()
        time.sleep_ms(750)

        temp_box = round(ds.read_temp(roms[0]), 2)
        
        temp_house = None
        if len(roms) >= 2:
            temp_house = round(ds.read_temp(roms[1]), 2)
        else:
            temp_house = temp_box 

        return temp_box, temp_house
    except Exception as e:
        log(f"温度計測失敗: {e}", "ERROR")
        return None, None

def read_vsys():
    try:
        vsys_pin = Pin(VSYS_ADC_NUM, Pin.IN)
        vsys_adc = ADC(vsys_pin)
        time.sleep_ms(10)
        
        total_raw = 0
        for _ in range(10):
            total_raw += vsys_adc.read_u16()
            time.sleep_ms(2)
        raw = total_raw // 10
        
        voltage = raw * VSYS_COEFF
        return round(voltage, 2)
    except Exception as e:
        log(f"VSYS計測失敗: {e}", "ERROR")
        return None

# ==================================================
# 4. 通信処理 (動的AP選択 & GAS 送信)
# ==================================================
def scan_and_connect_best():
    wlan = network.WLAN(network.STA_IF)
    
    if not wlan.active():
        wlan.active(True)
        time.sleep_ms(500)
    
    try:
        wlan.config(pm=0xa11154)
    except Exception:
        pass

    log("🔍 周辺Wi-Fiスキャン中...", "DIAG")
    scanned = []
    for retry in range(1, 4):
        try:
            scanned = wlan.scan()
            if scanned:
                break
            time.sleep(1)
        except Exception as e:
            time.sleep(1)

    if not scanned:
        return False, None

    candidate_aps = []
    for net in scanned:
        ssid = net[0].decode('utf-8')
        rssi = net[3]
        
        for ap in AP_LIST:
            target_ssid = ap.get("ssid") or ap.get("SSID")
            if ssid and target_ssid and ssid == target_ssid:
                candidate_aps.append({
                    "ssid": target_ssid,
                    "pass": ap.get("pass") or ap.get("PASS"),
                    "rssi": rssi
                })

    if not candidate_aps:
        return False, None

    candidate_aps.sort(key=lambda x: x["rssi"], reverse=True)
    time.sleep_ms(1500)

    for idx, target in enumerate(candidate_aps, 1):
        target_ssid = target["ssid"]
        target_pass = target["pass"]
        target_rssi = target["rssi"]

        log(f"🎯 選択AP: '{target_ssid}' ({target_rssi}dBm) 接続試行...", "INFO")
        
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(1000)

        wlan.connect(target_ssid, target_pass)

        timeout = 20
        while timeout > 0:
            if wlan.isconnected():
                ip = wlan.ifconfig()[0]
                log(f"✅ Wi-Fi接続成功! IP: {ip}", "INFO")
                sync_ntp_time()
                return True, target_rssi
            
            st = wlan.status()
            if st in (-2, -3):
                break

            time.sleep(1)
            timeout -= 1

    return False, None

def send_to_gas(payload):
    if not GAS_URL:
        return False

    url = GAS_URL.strip()
    try:
        proto, _, host, path = url.split("/", 3)
        path = "/" + path
        port = 443
        if ":" in host:
            host, port = host.split(":")
            port = int(port)

        data = json.dumps(payload)

        import socket
        import ssl

        ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        addr = ai[0][-1]
        s = socket.socket()
        s.settimeout(10.0)
        s.connect(addr)
        
        s = ssl.wrap_socket(s, server_hostname=host)

        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: Pico-W-GAS-Client\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n"
            f"{data}"
        )

        s.write(req.encode('utf-8'))
        response_line = s.readline().decode('utf-8')
        s.close()

        if any(code in response_line for code in [" 200 ", " 302 ", " 301 ", " 307 "]):
            return True
        else:
            return False
    except Exception as e:
        return False

# ==================================================
# 5. GitHub OTA（自動更新）処理
# ==================================================
def check_github_ota():
    user = getattr(config, "GITHUB_USER", None)
    repo = getattr(config, "GITHUB_REPO", None)
    branch = getattr(config, "GITHUB_BRANCH", "main")
    token = getattr(config, "GITHUB_TOKEN", None)

    if not (user and repo and token):
        return

    url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/main.py"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Pico-OTA"
    }

    try:
        res = urequests.get(url, headers=headers)
        if res.status_code == 200:
            remote_code = res.text
            res.close()

            local_code = ""
            try:
                with open("main.py", "r") as f:
                    local_code = f.read()
            except Exception:
                pass

            if remote_code != local_code:
                if "GAS_URL" not in remote_code and "send_to_gas" not in remote_code:
                    return

                log("🔄 コードを書き換えて再起動します...", "WARN")
                with open("main.py", "w") as f:
                    f.write(remote_code)
                time.sleep(1)
                machine.reset()
        else:
            res.close()
    except Exception as e:
        pass

# ==================================================
# 6. ローカルバッファ制御処理 (オフライン対策)
# ==================================================
def save_to_buffer(payload_item):
    buffer = []
    try:
        with open(BUFFER_FILE, "r") as f:
            buffer = json.load(f)
    except Exception:
        buffer = []

    buffer.append(payload_item)
    if len(buffer) > MAX_BUFFER_SIZE:
        buffer.pop(0)

    try:
        with open(BUFFER_FILE, "w") as f:
            json.dump(buffer, f)
    except Exception as e:
        pass

def flush_buffer():
    try:
        with open(BUFFER_FILE, "r") as f:
            buffer = json.load(f)
    except Exception:
        return

    if not buffer:
        return

    log(f"📤 蓄積バッファ ({len(buffer)}件) を送信中...", "INFO")
    remaining = []

    for idx, item in enumerate(buffer, 1):
        success = send_to_gas(item)
        if success:
            time.sleep_ms(200)
        else:
            remaining.extend(buffer[idx-1:])
            break

    if remaining:
        with open(BUFFER_FILE, "w") as f:
            json.dump(remaining, f)
    else:
        try:
            os.remove(BUFFER_FILE)
            log("✅ バッファデータをすべて正常送信しました", "INFO")
        except Exception:
            pass

# ==================================================
# 7. メイン実行シーケンス
# ==================================================
def run_one_cycle():
    led.value(1)
    temp_box, temp_house = read_temperatures()
    vsys = read_vsys()
    led.value(0)

    wifi_ok, rssi = scan_and_connect_best()

    if wifi_ok:
        check_github_ota()

    payload = {
        "device_id": DEVICE_LABEL,
        "temp_box": temp_box,
        "temp_house": temp_house,
        "vsys_voltage": vsys,
        "rssi": rssi
    }

    if wifi_ok and temp_box is not None:
        log(f"計測完了 -> BOX: {temp_box}℃ / ハウス: {temp_house}℃ / VSYS: {vsys}V", "INFO")
        flush_buffer()
        if send_to_gas(payload):
            log("★ GASへの送信成功!", "INFO")
        else:
            save_to_buffer(payload)
    else:
        if temp_box is not None:
            save_to_buffer(payload)
            
    try:
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            wlan.disconnect()
    except Exception:
        pass

def main():
    while True:
        start_ticks = time.ticks_ms()
        try:
            run_one_cycle()
        except Exception as e:
            pass

        elapsed_ms = time.ticks_diff(time.ticks_ms(), start_ticks)
        elapsed_sec = elapsed_ms / 1000.0
        
        sleep_time = max(1, int(INTERVAL_SEC - elapsed_sec))
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()