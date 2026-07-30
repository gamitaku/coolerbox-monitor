import network
import time
from machine import Pin, ADC, reset
import onewire, ds18x20
import urequests
import ujson
import socket

# ==================================================
# config.py から設定情報・機密情報を読み込み
# ==================================================
try:
    import config
    DEVICE_NAME = getattr(config, "DEVICE_NAME", "保冷BOX本機 (Pico W)")
    WIFI_SSID = config.WIFI_SSID
    WIFI_PASS = config.WIFI_PASS
    UBIDOTS_TOKEN = config.UBIDOTS_TOKEN
    GITHUB_TOKEN = getattr(config, "GITHUB_TOKEN", "")
except ImportError:
    print("[エラー] config.py が見つかりません。デフォルト設定で動かします。")
    DEVICE_NAME = "保冷BOX (未設定)"
    WIFI_SSID = ""
    WIFI_PASS = ""
    UBIDOTS_TOKEN = ""
    GITHUB_TOKEN = ""

# ==================================================
# 1. システム設定・機体プロファイル判定
# ==================================================
OTA_UPDATE_URL = "https://api.github.com/repos/gamitaku/coolerbox-monitor/contents/main.py"

DEVICE_PROFILES = {
    "保冷BOX本機": {
        "DEVICE_LABEL": "pico-box-01"
    },
    "検証機": {
        "DEVICE_LABEL": "pico-test-01"
    }
}

active_profile = DEVICE_PROFILES["保冷BOX本機"]
for name_key in DEVICE_PROFILES:
    if name_key in DEVICE_NAME:
        active_profile = DEVICE_PROFILES[name_key]
        break

DEVICE_LABEL = active_profile["DEVICE_LABEL"]

# ピン配置
DS_PIN_NUM = 15     # GP15
VSYS_ADC_NUM = 3    # GP29 / ADC3
LED_PIN_NUM = "LED"

# ==================================================
# 2. ハードウェア初期化
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)
vsys_adc = ADC(VSYS_ADC_NUM)

# 最新の計測値を保持するグローバル変数
latest_data = {
    "temp": None,
    "vsys": None,
    "rssi": None,
    "last_update": "未計測"
}

# ==================================================
# 3. センサー計測処理
# ==================================================
def read_temperature():
    try:
        roms = ds.scan()
        if not roms:
            print(f"[{DEVICE_NAME}] [警告] DS18B20未検出")
            return None
        
        ds.convert_temp()
        time.sleep_ms(750)
        temp = ds.read_temp(roms[0])
        return round(temp, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] 温度計測失敗: {e}")
        return None

def read_vsys():
    try:
        Pin(29, Pin.IN)
        time.sleep_ms(10)
        total_raw = 0
        for _ in range(10):
            total_raw += vsys_adc.read_u16()
            time.sleep_ms(2)
        raw = total_raw // 10
        
        if raw < 1000:
            voltage = 4.80
        else:
            voltage = raw * 0.00014793

        return round(voltage, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] VSYS計測失敗: {e}")
        return None

# ==================================================
# 4. 通信処理 (Wi-Fi 接続 & OTA)
# ==================================================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(pm=0xa11154)
    except Exception:
        pass

    if not wlan.isconnected():
        print(f"[{DEVICE_NAME}] Wi-Fi接続試行中 ({WIFI_SSID})...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        timeout = 15
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1
            
    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        rssi = wlan.status('rssi')
        print(f"[{DEVICE_NAME}] Wi-Fi接続成功! IP: {ip}, RSSI: {rssi}dBm")
        return True, ip, rssi
    else:
        print(f"[{DEVICE_NAME}] [エラー] Wi-Fi接続失敗")
        return False, None, None

def check_and_update_ota():
    if not OTA_UPDATE_URL:
        return

    print(f"[{DEVICE_NAME}] [OTA] アップデート確認中...")
    try:
        headers = {
            "User-Agent": "PicoW",
            "Accept": "application/vnd.github.v3.raw"
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        res = urequests.get(OTA_UPDATE_URL, headers=headers)
        if res.status_code == 200:
            new_code = res.text
            res.close()
            
            try:
                with open("main.py", "r") as f:
                    current_code = f.read()
            except Exception:
                current_code = ""

            if new_code != current_code:
                print(f"[{DEVICE_NAME}] [OTA] ★ 新しいコードを検出! 上書き更新して再起動します...")
                with open("main.py", "w") as f:
                    f.write(new_code)
                time.sleep(1)
                reset()
            else:
                print(f"[{DEVICE_NAME}] [OTA] 現在のコードは最新です")
        else:
            res.close()
            print(f"[{DEVICE_NAME}] [OTA] 確認失敗 (HTTP {res.status_code})")
    except Exception as e:
        print(f"[{DEVICE_NAME}] [OTAエラー] {e}")

# ==================================================
# 5. Web サーバーレスポンス生成
# ==================================================
def make_html_response():
    temp_str = f"{latest_data['temp']} ℃" if latest_data['temp'] is not None else "エラー"
    vsys_str = f"{latest_data['vsys']} V" if latest_data['vsys'] is not None else "エラー"
    rssi_str = f"{latest_data['rssi']} dBm" if latest_data['rssi'] is not None else "エラー"
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{DEVICE_NAME}</title>
    <style>
        body {{ font-family: sans-serif; background: #f0f4f8; margin: 20px; text-align: center; }}
        .card {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; }}
        h1 {{ color: #333; font-size: 20px; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        .metric {{ font-size: 28px; font-weight: bold; color: #007bff; margin: 15px 0; }}
        .info {{ font-size: 14px; color: #666; text-align: left; margin-top: 20px; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{DEVICE_NAME}</h1>
        <div class="metric">温度: {temp_str}</div>
        <div class="info">
            <p>🔋 <b>VSYS電圧:</b> {vsys_str}</p>
            <p>📶 <b>Wi-Fi電波強度:</b> {rssi_str}</p>
            <p>🏷️ <b>Ubidots Target:</b> {DEVICE_LABEL}</p>
        </div>
    </div>
</body>
</html>"""
    return html

# ==================================================
# 6. メイン実行シーケンス & Web サーバー
# ==================================================
def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス")
    print(f"  Ubidotsターゲットラベル: {DEVICE_LABEL}")
    print("=" * 50)
    
    # 1. Wi-Fi 接続
    wifi_ok, ip, rssi = connect_wifi()
    if not wifi_ok:
        RETRY_WAIT_SEC = 300
        print(f"[{DEVICE_NAME}] [警告] Wi-Fi接続不可。5分後に再起動してリトライします...")
        for _ in range(RETRY_WAIT_SEC * 2):
            led.value(not led.value())
            time.sleep_ms(500)
        led.value(0)
        reset()

    # 2. OTAコード更新チェック
    check_and_update_ota()

    # 3. センサー計測
    temp = read_temperature()
    vsys = read_vsys()
    
    latest_data["temp"] = temp
    latest_data["vsys"] = vsys
    latest_data["rssi"] = rssi
    
    print(f"[{DEVICE_NAME}] 計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm")
    
    # 4. Ubidots送信
    send_payload = {}
    if temp is not None: send_payload["temp"] = temp
    if vsys is not None: send_payload["vsys"] = vsys
    if rssi is not None: send_payload["rssi"] = rssi

    url = f"https://industrial.api.ubidots.com/api/v1.6/devices/{DEVICE_LABEL}"
    headers = {
        "X-Auth-Token": UBIDOTS_TOKEN,
        "Content-Type": "application/json"
    }
    
    try:
        res = urequests.post(url, data=ujson.dumps(send_payload), headers=headers)
        print(f"[{DEVICE_NAME}] Ubidots送信レスポンス: {res.status_code}")
        res.close()
    except Exception as e:
        print(f"[{DEVICE_NAME}] [送信例外] {e}")

    # 5. HTTP Web サーバーの開始
    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    print(f"[{DEVICE_NAME}] ★ Webサーバー起動完了: http://{ip}/ でアクセス可能")

    # 接続受け入れループ
    while True:
        try:
            cl, client_addr = s.accept()
            req = cl.recv(1024).decode('utf-8')
            
            # APIリクエスト判定 (/api)
            if "GET /api " in req:
                response_body = ujson.dumps(latest_data)
                cl.send('HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n')
                cl.send(response_body)
            else:
                # 通常のブラウザアクセス
                response_body = make_html_response()
                cl.send('HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n')
                cl.send(response_body)
                
            cl.close()
        except Exception as e:
            print(f"[{DEVICE_NAME}] Webリクエスト処理エラー: {e}")

if __name__ == "__main__":
    main()
