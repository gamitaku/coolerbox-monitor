import network
import time
from machine import Pin, ADC, reset
import onewire, ds18x20
import urequests
import ujson

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
# OTA更新用URL (GitHub Raw URL)
OTA_UPDATE_URL = "https://raw.githubusercontent.com/gamitaku/coolerbox-monitor/main/main.py"

# --------------------------------------------------
# プロファイル設定 (Ubidots上のデバイス識別名)
# --------------------------------------------------
DEVICE_PROFILES = {
    "保冷BOX本機": {
        "DEVICE_LABEL": "pico-box-01"   # 本機の識別ラベル
    },
    "検証機": {
        "DEVICE_LABEL": "pico-test-01"  # 検証機の識別ラベル
    }
}

active_profile = DEVICE_PROFILES["保冷BOX本機"]
for name_key in DEVICE_PROFILES:
    if name_key in DEVICE_NAME:
        active_profile = DEVICE_PROFILES[name_key]
        break

DEVICE_LABEL = active_profile["DEVICE_LABEL"]

# ピン配置
DS_PIN_NUM = 15     # GP15 (20番)
VSYS_ADC_NUM = 3    # GP29 / ADC3 (34番)
LED_PIN_NUM = "LED"

# ==================================================
# 2. ハードウェア初期化
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)
vsys_adc = ADC(VSYS_ADC_NUM)

# ==================================================
# 3. センサー計測処理
# ==================================================
def read_temperature():
    """ DS18B20から温度を取得 """
    try:
        roms = ds.scan()
        if not roms:
            print(f"[{DEVICE_NAME}] [警告] DS18B20未検出 -> 配線を確認してください")
            return None
        
        ds.convert_temp()
        time.sleep_ms(750)
        
        temp = ds.read_temp(roms[0])
        return round(temp, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] 温度計測失敗: {e}")
        return None

def read_vsys():
    """ VSYS電源電圧を計測 (実測 4.80V 基準補正) """
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
            print(f"[{DEVICE_NAME}] [DEBUG] VSYS raw: {raw} (仮値適用)")
        else:
            voltage = raw * 0.00014793  # 4.8V用補正係数
            print(f"[{DEVICE_NAME}] [DEBUG] VSYS raw: {raw}, calc: {voltage:.2f}V")

        return round(voltage, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] VSYS計測失敗: {e}")
        return None

# ==================================================
# 4. 通信処理 (Wi-Fi 接続 & OTA)
# ==================================================
def connect_wifi():
    """ Wi-Fi接続処理 """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    try:
        wlan.config(pm=0xa11154) # 省電力モードOFF
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
        return True, rssi
    else:
        print(f"[{DEVICE_NAME}] [エラー] Wi-Fi接続失敗")
        return False, None

def check_and_update_ota():
    """ WEBから最新のmain.pyを取得して自動更新する処理 """
    if not OTA_UPDATE_URL:
        return

    print(f"[{DEVICE_NAME}] [OTA] アップアップデート確認中...")
    try:
        # 非公開リポジトリ用に GitHub Token をヘッダーに付与
        headers = {}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
            headers["User-Agent"] = "PicoW"

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
# 5. メイン実行シーケンス
# ==================================================
def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス")
    print(f"  Ubidotsターゲットラベル: {DEVICE_LABEL}")
    print("=" * 50)
    
    # 1. Wi-Fi 接続
    wifi_ok, rssi = connect_wifi()
    if not wifi_ok:
        return

    # 2. OTAコード更新チェック
    check_and_update_ota()

    # LED点灯
    led.value(1)
    
    # 3. センサー計測
    temp = read_temperature()
    vsys = read_vsys()
    
    print(f"[{DEVICE_NAME}] 計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm")
    
    # 4. Ubidots送信データ作成
    send_payload = {}
    if temp is not None:
        send_payload["temp"] = temp
    if vsys is not None:
        send_payload["vsys"] = vsys
    if rssi is not None:
        send_payload["rssi"] = rssi

    # 5. Ubidots送信処理
    url = f"https://industrial.api.ubidots.com/api/v1.6/devices/{DEVICE_LABEL}"
    headers = {
        "X-Auth-Token": UBIDOTS_TOKEN,
        "Content-Type": "application/json"
    }
    
    for attempt in range(1, 4):
        print(f"[{DEVICE_NAME}] Ubidots送信試行 ({attempt}/3)...")
        try:
            res = urequests.post(url, data=ujson.dumps(send_payload), headers=headers)
            print(f"[{DEVICE_NAME}] レスポンスコード: {res.status_code}")
            if res.status_code in (200, 201):
                print(f"[{DEVICE_NAME}] ★ Ubidotsデータ送信成功! Target: {DEVICE_LABEL}")
                res.close()
                break
            res.close()
        except Exception as e:
            print(f"[{DEVICE_NAME}] [送信例外] {e}")
            
        time.sleep(1)
    
    led.value(0)

if __name__ == "__main__":
    main()
