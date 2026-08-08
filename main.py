import network
import time
import json
import os
from machine import Pin, ADC
import onewire, ds18x20
import urequests
import config  # ★ config.py を読み込み

# ==================================================
# 1. システム設定・機体選択
# ==================================================
DEVICE_NAME = config.DEVICE_NAME
UBIDOTS_TOKEN = config.UBIDOTS_TOKEN
BUFFER_FILE = "unsent_buffer.json"  # 未送信データ保持用ファイル
MAX_BUFFER_SIZE = 500              # 最大バッファ件数（Flash圧迫防止）
INTERVAL_SEC = 300                 # 5分（300秒）周期

# config.py の AP_LIST (複数AP対応) または 単一 AP へのフォールバック処理
AP_LIST = getattr(config, "AP_LIST", None)
if not AP_LIST:
    # 古い config.py (単一AP記述) 互換用
    AP_LIST = [{"ssid": config.WIFI_SSID, "pass": config.WIFI_PASS}]

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
# 3. ログ・センサー計測処理
# ==================================================
def log(msg, level="INFO"):
    """ 標準ログ出力関数 """
    print(f"[{DEVICE_NAME}] [{level}] {msg}")

def read_temperature():
    """ DS18B20から温度を取得 """
    try:
        roms = ds.scan()
        if not roms:
            log("警告: DS18B20未検出 -> 配線を確認してください", "WARN")
            return None
        ds.convert_temp()
        time.sleep_ms(750)
        temp = ds.read_temp(roms[0])
        return round(temp, 2)
    except Exception as e:
        log(f"温度計測失敗: {e}", "ERROR")
        return None

def read_vsys():
    """ VSYS電源電圧を計測 (Pico 2 W 安定化版) """
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
        log(f"DEBUG VSYS raw: {raw}, calc: {voltage:.2f}V", "DEBUG")
        return round(voltage, 2)
    except Exception as e:
        log(f"VSYS計測失敗: {e}", "ERROR")
        return None

# ==================================================
# 4. 通信処理 (動的AP選択 & Ubidots 送信)
# ==================================================
def scan_and_connect_best():
    """ 周辺スキャンを実施し、登録済みAPを電波強度順に試行して接続 """
    wlan = network.WLAN(network.STA_IF)
    
    # CYW43チップ強制リセット (soft reboot時のハング対策)
    wlan.active(False)
    time.sleep_ms(300)
    wlan.active(True)
    time.sleep(1)
    
    try:
        wlan.config(pm=0xa11154) # 省電力OFF (パフォーマンス優先)
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
        except Exception:
            time.sleep(1)

    if not scanned:
        log("⚠️ 周囲に2.4GHz帯のWi-Fiが見つかりません", "WARN")
        return False, None

    # スキャン結果から「登録済みAP」のみ抽出
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
        log("⚠️ 周囲に登録済みのWi-Fiが見つかりません", "WARN")
        return False, None

    # RSSI（電波強度）が強い順にソート（第1優先、第2優先...）
    candidate_aps.sort(key=lambda x: x["rssi"], reverse=True)

    # 強い順に接続試行
    for idx, target in enumerate(candidate_aps, 1):
        target_ssid = target["ssid"]
        target_pass = target["pass"]
        target_rssi = target["rssi"]

        log(f"🎯 選択AP ({idx}/{len(candidate_aps)}): '{target_ssid}' ({target_rssi}dBm) 接続試行...", "INFO")
        
        wlan.disconnect()
        time.sleep(1)
        wlan.connect(target_ssid, target_pass)

        timeout = 15
        while timeout > 0:
            status = wlan.status()
            if wlan.isconnected() or status == 3:
                ip = wlan.ifconfig()[0]
                log(f"✅ Wi-Fi接続成功! IP: {ip}", "INFO")
                return True, target_rssi
            elif status < 0:
                break
            time.sleep(1)
            timeout -= 1

        log(f"❌ '{target_ssid}' 接続失敗。次のAPへ...", "WARN")

    return False, None

def send_to_ubidots(payload):
    """ UbidotsへHTTP POSTで送信 """
    url = f"http://industrial.api.ubidots.com/api/v1.6/devices/{DEVICE_LABEL}"
    headers = {
        "X-Auth-Token": UBIDOTS_TOKEN,
        "Content-Type": "application/json"
    }
    
    try:
        response = urequests.post(url, json=payload, headers=headers)
        status = response.status_code
        response.close()
        return status in (200, 201)
    except Exception as e:
        log(f"Ubidots送信例外: {e}", "ERROR")
        return False

# ==================================================
# 5. ローカルバッファ制御処理 (オフライン対策)
# ==================================================
def save_to_buffer(payload_item):
    """ 未送信データを Flash 内の json に一時保存 """
    buffer = []
    try:
        with open(BUFFER_FILE, "r") as f:
            buffer = json.load(f)
    except Exception:
        buffer = []

    buffer.append(payload_item)

    # 件数オーバー時は古いデータを押し出し
    if len(buffer) > MAX_BUFFER_SIZE:
        buffer.pop(0)

    try:
        with open(BUFFER_FILE, "w") as f:
            json.dump(buffer, f)
        log(f"💾 データをローカルバッファに保存しました (全{len(buffer)}件)", "WARN")
    except Exception as e:
        log(f"バッファ保存エラー: {e}", "ERROR")

def flush_buffer():
    """ Wi-Fi復帰時に溜まったバッファを一括送信 """
    try:
        with open(BUFFER_FILE, "r") as f:
            buffer = json.load(f)
    except Exception:
        return

    if not buffer:
        return

    log(f"📤 蓄積バッファ ({len(buffer)}件) のフラッシュ送信を開始...", "INFO")
    remaining = []

    for idx, item in enumerate(buffer, 1):
        success = send_to_ubidots(item)
        if success:
            log(f"  └ 成功 ({idx}/{len(buffer)})", "INFO")
            time.sleep_ms(200)  # 連投時の負荷軽減
        else:
            log(f"  └ 再送信失敗。残りを保持します", "WARN")
            remaining.extend(buffer[idx-1:])
            break

    if remaining:
        with open(BUFFER_FILE, "w") as f:
            json.dump(remaining, f)
        log(f"⚠️ 一部未送信あり。残り: {len(remaining)}件", "WARN")
    else:
        try:
            os.remove(BUFFER_FILE)
            log("✅ バッファデータをすべて正常送信・クリアしました！", "INFO")
        except Exception:
            pass

# ==================================================
# 6. メイン実行シーケンス (5分周期ループ)
# ==================================================
def run_one_cycle():
    """ 1回分の「計測 → 接続 → 送信/バッファ → 切断」シーケンス """
    # 1. センサー計測 (Wi-Fiオフの状態で計測)
    led.value(1)
    temp = read_temperature()
    vsys = read_vsys()
    led.value(0)

    # 2. Wi-Fi 接続
    wifi_ok, rssi = scan_and_connect_best()

    # 3. ペイロード作成
    payload = {}
    if temp is not None:
        payload["temperature"] = temp
    if vsys is not None:
        payload["vsys_voltage"] = vsys
    if rssi is not None:
        payload["rssi"] = rssi

    # 4. 送信またはバッファリング
    if wifi_ok and payload:
        log(f"計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm", "INFO")
        flush_buffer()  # 過去の未送信データをクリア
        
        log("Ubidotsへ最新データを送信中...", "INFO")
        if send_to_ubidots(payload):
            log("★ 最新データの送信成功!", "INFO")
        else:
            log("❌ 送信失敗。バッファへ退避します", "WARN")
            save_to_buffer(payload)
    else:
        log(f"オフライン計測 -> 温度: {temp}℃ / VSYS: {vsys}V", "WARN")
        if payload:
            save_to_buffer(payload)
            
    # 5. 次のサイクルに向けてWi-Fiを明示的に切断 (接続切り残しハング防止)
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.disconnect()
        wlan.active(False)
    except Exception:
        pass

def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス (5分周期・自律バッファ＆動的APモード)")
    print(f"  デバイスラベル: {DEVICE_LABEL}")
    print("=" * 50)

    while True:
        start_time = time.time()
        
        try:
            run_one_cycle()
        except Exception as e:
            log(f"メインループ内例外発生: {e}", "ERROR")

        # 処理にかかった時間を差し引いて正確に 5分（300秒）間隔を維持
        elapsed = time.time() - start_time
        sleep_time = max(1, INTERVAL_SEC - elapsed)
        
        log(f"⏳ 処理時間: {elapsed}秒 ➔ 次の計測まで {sleep_time}秒 待機します", "INFO")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
