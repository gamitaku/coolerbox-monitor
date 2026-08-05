import network
import time
from machine import Pin, ADC, reset
import onewire, ds18x20
import urequests
import config
import ota_updater  # Pico内の ota_updater.py を呼び出す

# ==================================================
# 1. システム設定・機体選択
# ==================================================
DEVICE_NAME = config.DEVICE_NAME

# config.py の WIFI_AP_LIST を直接取得（後換性フォールバック付き）
if hasattr(config, "WIFI_AP_LIST"):
    AP_LIST = config.WIFI_AP_LIST
else:
    AP_LIST = [{"ssid": getattr(config, "WIFI_SSID", ""), "pass": getattr(config, "WIFI_PASS", "")}]

UBIDOTS_TOKEN = config.UBIDOTS_TOKEN

# 🌐 通信・ロケーション判定パラメータ
LOW_RSSI_THRESHOLD = -45     # -45 dBm を下回ったら再スキャン＆接続
RSSI_DELTA_THRESHOLD = 10    # ±10 dBm 以上の急変でロケーション変動とみなす
MAX_RETRY = 3                # 連続失敗数の上限
COOL_DOWN_MINUTES = 15      # 3回失敗時の待機時間（分）

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

# グローバル状態保持
last_rssi = None
fail_count = 0

# ==================================================
# 2. ハードウェア初期化＆ログ補助
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)

def log(msg, level="INFO"):
    """ 後日解析用 タイムスタンプ付き詳細ログ関数 """
    t = time.localtime()
    time_str = "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])
    print(f"[{time_str}] [{DEVICE_NAME}] [{level}] {msg}")

# ==================================================
# 3. センサー計測処理
# ==================================================
def read_temperature():
    """ DS18B20から温度を取得 """
    try:
        roms = ds.scan()
        if not roms:
            log("DS18B20未検出 -> 配線を確認してください", "警告")
            return None
        ds.convert_temp()
        time.sleep_ms(750)
        temp = ds.read_temp(roms[0])
        return round(temp, 2)
    except Exception as e:
        log(f"温度計測失敗: {e}", "エラー")
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
        log(f"VSYS raw: {raw}, calc: {voltage:.2f}V", "DEBUG")

        return round(voltage, 2)
    except Exception as e:
        log(f"VSYS計測失敗: {e}", "エラー")
        return None

# ==================================================
# 4. Wi-Fi接続制御 (複数AP対応・判定ロジック組み込み)
# ==================================================
def scan_and_connect_best():
    """ 周辺スキャンを実施し、登録済みAPを電波強度順に試行して接続 """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    # チップ初期化の安定待ち
    time.sleep(1)
    
    try:
        wlan.config(pm=0xa11154) # CYW43 省電力OFF
    except Exception:
        pass

    log("🔍 周辺Wi-Fiスキャン中...", "DIAG")
    try:
        scanned = wlan.scan()
    except Exception as e:
        log(f"スキャン失敗: {e}", "エラー")
        return False, None

    if not scanned:
        log("⚠️ 周囲に2.4GHz帯のWi-Fiが見つかりません（0件検出）", "WARN")
        return False, None

    # スキャン結果から「登録済みAP」のみを抽出
    candidate_aps = []
    for net in scanned:
        ssid = net[0].decode('utf-8')
        rssi = net[3]
        
        for ap in AP_LIST:
            target_ssid = ap.get("ssid") or ap.get("SSID")
            if ssid and target_ssid and ssid == target_ssid:
                log(f" 📌 登録AP検出: '{ssid}' (強度: {rssi}dBm)", "DIAG")
                candidate_aps.append({
                    "ssid": target_ssid,
                    "pass": ap.get("pass") or ap.get("PASS"),
                    "rssi": rssi
                })

    if not candidate_aps:
        log("❌ 周囲に登録済みのWi-Fiアクセスポイントが見つかりませんでした", "警告")
        return False, None

    # RSSI（電波強度）が強い順にソート（第1優先、第2優先...）
    candidate_aps.sort(key=lambda x: x["rssi"], reverse=True)

    # 強い順に順番に接続を試行
    for idx, target in enumerate(candidate_aps, 1):
        target_ssid = target["ssid"]
        target_pass = target["pass"]
        target_rssi = target["rssi"]

        log(f"🎯 [試行 {idx}/{len(candidate_aps)}] 選択AP: '{target_ssid}' (強度: {target_rssi}dBm) に接続試行...", "INFO")
        
        # 明示的な切断とウェイト（CYW43ハング防止）
        wlan.disconnect()
        time.sleep(1)
        
        wlan.connect(target_ssid, target_pass)

        # 15秒間 接続完了を監視
        timeout = 15
        while timeout > 0:
            status = wlan.status()
            # isconnected() か status == 3 (CYW43のSTAT_GOT_IP) で成功判定
            if wlan.isconnected() or status == 3:
                ifconfig = wlan.ifconfig()
                log(f"✅ Wi-Fi接続成功! IP: {ifconfig[0]}, GW: {ifconfig[2]}", "INFO")
                return True, target_rssi
            elif status < 0: # エラー状態
                log(f"⚠️ 接続エラーを検知 (status: {status})", "WARN")
                break
                
            time.sleep(1)
            timeout -= 1

        log(f"❌ '{target_ssid}' への接続タイムアウト/失敗。次のAPを試します...", "WARN")

    log("❌ すべての登録済みアクセスポイントへの接続に失敗しました", "警告")
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
        log(f"Ubidots送信例外 (ソケット/DNSエラー): {e}", "エラー")
        return None

# ==================================================
# 5. メイン実行シーケンス
# ==================================================
def run_cycle():
    global last_rssi, fail_count

    wlan = network.WLAN(network.STA_IF)
    need_reconnect = False
    
    if not wlan.isconnected():
        log("Wi-Fi未接続状態を検知", "WARN")
        need_reconnect = True
    else:
        try:
            current_rssi = wlan.status('rssi')
        except Exception:
            current_rssi = -99

        # 条件1: -45 dBm 未満（電波低下）
        if current_rssi < LOW_RSSI_THRESHOLD:
            log(f"📉 電波低下検知 ({current_rssi} dBm < {LOW_RSSI_THRESHOLD} dBm)。再選択を実行します。", "WARN")
            wlan.disconnect()
            need_reconnect = True
        
        # 条件2: ±10 dBm 以上の急変（ロケーション変動）
        elif last_rssi is not None and abs(current_rssi - last_rssi) >= RSSI_DELTA_THRESHOLD:
            log(f"🚗 ロケーション変動検知 (前回: {last_rssi} dBm -> 今回: {current_rssi} dBm)。最適APを再検索します。", "INFO")
            wlan.disconnect()
            need_reconnect = True

    # 再接続処理が必要な場合
    if need_reconnect:
        wifi_ok, rssi = scan_and_connect_best()
        if not wifi_ok:
            fail_count += 1
            log(f"❌ 接続失敗 (連続失敗数: {fail_count}/{MAX_RETRY})", "ERROR")
            
            # 3回連続失敗時の15分冷却＆ハードリセット
            if fail_count >= MAX_RETRY:
                log(f"🛑 {MAX_RETRY}回連続失敗。通信スタックと判断し{COOL_DOWN_MINUTES}分待機後にハードリセットします。", "CRITICAL")
                time.sleep(COOL_DOWN_MINUTES * 60)
                log("🔄 15分経過。Picoをハードリセットして再起動します...", "SYSTEM")
                reset()
            return False
        else:
            fail_count = 0
            last_rssi = rssi
    else:
        last_rssi = wlan.status('rssi')
        rssi = last_rssi

    # OTA 自動更新チェック
    try:
        ota_updater.check_and_update()
    except Exception as e:
        log(f"OTA更新チェック例外: {e}", "WARN")

    led.value(1)
    
    # センサー計測
    temp = read_temperature()
    vsys = read_vsys()
    
    log(f"計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm", "INFO")
    
    # ペイロード作成
    payload = {}
    if temp is not None:
        payload["temperature"] = temp
    if vsys is not None:
        payload["vsys_voltage"] = vsys
    if rssi is not None:
        payload["rssi"] = rssi

    # Ubidots 送信 (最大3回試行)
    success = False
    for attempt in range(1, 4):
        log(f"Ubidots送信試行 ({attempt}/3)...", "INFO")
        status = send_to_ubidots(payload)
        
        if status in (200, 201):
            log(f"★ Ubidotsデータ送信成功! Status: {status}", "INFO")
            success = True
            break
        else:
            log(f"送信失敗 ステータスコード: {status}", "WARN")
            time.sleep(1)

    led.value(0)
    return success

def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス (動的AP選択・監視モード)")
    print(f"  デバイスラベル: {DEVICE_LABEL}")
    print("=" * 50)
    
    run_cycle()

if __name__ == "__main__":
    main()
