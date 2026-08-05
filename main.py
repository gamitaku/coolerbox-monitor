import network
import time
from machine import Pin, ADC
import onewire, ds18x20
import urequests
import config
import ota_updater  # Pico内の ota_updater.py を呼び出す

# ==================================================
# 1. システム設定・機体選択
# ==================================================
DEVICE_NAME = config.DEVICE_NAME
WIFI_SSID = config.WIFI_SSID
WIFI_PASS = config.WIFI_PASS
UBIDOTS_TOKEN = config.UBIDOTS_TOKEN

DEVICE_PROFILES = {
    "保冷BOX本機": {
        "DEVICE_LABEL": "pico-w-main",
        "VSYS_COEFF": 0.00014793
    },
    "検証機": {
        "DEVICE_LABEL": "pico-2w-test",
        "VSYS_COEFF": 0.00014943  # 実測校正値
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
DS_PIN_NUM = 15     # GP15
VSYS_ADC_NUM = 3    # GP29 (ADC3)
LED_PIN_NUM = "LED"

# ==================================================
# 2. ハードウェア初期化
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)

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
    """ VSYS電源電圧を計測 (GP29 / ADC3) """
    try:
        vsys_pin = Pin(29, Pin.IN)
        vsys_adc = ADC(vsys_pin)
        time.sleep_ms(10)
        
        total_raw = 0
        for _ in range(10):
            total_raw += vsys_adc.read_u16()
            time.sleep_ms(2)
        raw = total_raw // 10
        
        voltage = raw * VSYS_COEFF
        print(f"[{DEVICE_NAME}] [DEBUG] VSYS raw: {raw}, calc: {voltage:.2f}V")

        return round(voltage, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] VSYS計測失敗: {e}")
        return None

# ==================================================
# 4. 通信処理 (Wi-Fi 接続 & Ubidots 送信)
# ==================================================
def connect_wifi():
    """ Wi-Fi接続処理 """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    try:
        wlan.config(pm=0xa11154) # CYW43 省電力OFF
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
        time.sleep_ms(100)
        try:
            rssi = wlan.status('rssi')
        except Exception:
            rssi = -99
            
        print(f"[{DEVICE_NAME}] Wi-Fi接続成功! IP: {ip}, RSSI: {rssi}dBm")
        return True, rssi
    else:
        print(f"[{DEVICE_NAME}] [エラー] Wi-Fi接続失敗")
        return False, None

def send_to_ubidots(payload):
    """ UbidotsへHTTP POSTでデータを送信 """
    url = f"http://industrial.api.ubidots.com/api/v1.6/devices/{DEVICE_LABEL}"
    headers = {
        "X-Auth-Token": UBIDOTS_TOKEN,
        "Content-Type": "application/json"
    }
    
    try:
        response = urequests.post(url, json=payload, headers=headers)
        status = response.status_code
        response.close()
        return status
    except Exception as e:
        print(f"[{DEVICE_NAME}] [Ubidots送信例外] {e}")
        return None

# ==================================================
# 5. メイン実行シーケンス
# ==================================================
def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス (Ubidotsモード)")
    print(f"  デバイスラベル: {DEVICE_LABEL}")
    print("=" * 50)
    
    # Wi-Fi接続
    wifi_ok, rssi = connect_wifi()
    if not wifi_ok:
        return

    # ★ 接続直後に OTA 自動更新チェック（main.py & ota_updater.py の一括監視）
    ota_updater.check_and_update()

    led.value(1)
    
    # センサー計測
    temp = read_temperature()
    vsys = read_vsys()
    
    print(f"[{DEVICE_NAME}] 計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm")
    
    # ペイロード作成
    payload = {}
    if temp is not None:
        payload["temperature"] = temp
    if vsys is not None:
        payload["vsys_voltage"] = vsys
    if rssi is not None:
        payload["rssi"] = rssi

    # Ubidots 送信
    for attempt in range(1, 4):
        print(f"[{DEVICE_NAME}] Ubidots送信試行 ({attempt}/3)...")
        status = send_to_ubidots(payload)
        
        if status in (200, 201):
            print(f"[{DEVICE_NAME}] ★ Ubidotsデータ送信成功! Status: {status}")
            break
        else:
            print(f"[{DEVICE_NAME}] [警告] 送信失敗 ステータスコード: {status}")
            
        time.sleep(1)

if __name__ == "__main__":
    main()
