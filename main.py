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
    """ 標準ログ出力関数 """
    print(f"[{DEVICE_NAME}] [{level}] {msg}")

def sync_ntp_time():
    """ Wi-Fi接続時にNTPサーバーから時刻を取得してRTCを更新 """
    ntptime.host = "ntp.nict.jp"  # NICTのNTPサーバー
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
    """ DS18B20から温度を2軸分取得（ROM ID昇順で識別） """
    try:
        roms = ds.scan()
        if not roms:
            log("警告: DS18B20未検出 -> 配線を確認してください", "WARN")
            return None, None
        
        # ROM IDを昇順ソートして常に同じセンサーを特定
        roms = sorted(roms)

        ds.convert_temp()
        time.sleep_ms(750)

        temp_box = round(ds.read_temp(roms[0]), 2)  # ROM[0] = BOX内温度
        
        temp_house = None
        if len(roms) >= 2:
            temp_house = round(ds.read_temp(roms[1]), 2)  # ROM[1] = ハウス内温度
        else:
            temp_house = temp_box  # 1台のみ接続時は同一値を補填

        return temp_box, temp_house
    except Exception as e:
        log(f"温度計測失敗: {e}", "ERROR")
        return None, None

def read_vsys():
    """ VSYS電源電圧を計測 """
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
        log(f"DEBUG VSYS raw: {raw}, calc: {voltage:.2f}V", "DEBUG")
        return round(voltage, 2)
    except Exception as e:
        log(f"VSYS計測失敗: {e}", "ERROR")
        return None

# ==================================================
# 4. 通信処理 (動的AP選択 & GAS 送信)
# ==================================================
def scan_and_connect_best():
    """ 周辺スキャンを実施し、登録済みAPを電波強度順に試行して接続 """
    wlan = network.WLAN(network.STA_IF)
    
    if not wlan.active():
        wlan.active(True)
        time.sleep_ms(500)
    
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
        except Exception as e:
            log(f"スキャン一時失敗 (retry {retry}): {e}", "DEBUG")
            time.sleep(1)

    if not scanned:
        log("⚠️ 周囲に2.4GHz帯のWi-Fiが見つかりません", "WARN")
        return False, None

    # 抽出処理
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
        log("⚠️ 周囲に登録済みのWi-Fiが見つかりません", "WARN")
        return False, None

    # 電波強度順にソート
    candidate_aps.sort(key=lambda x: x["rssi"], reverse=True)

    # スキャン後の安定化待機
    time.sleep_ms(1500)

    # 接続試行
    for idx, target in enumerate(candidate_aps, 1):
        target_ssid = target["ssid"]
        target_pass = target["pass"]
        target_rssi = target["rssi"]

        log(f"🎯 選択AP ({idx}/{len(candidate_aps)}): '{target_ssid}' ({target_rssi}dBm) 接続試行...", "INFO")
        
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(1000)

        wlan.connect(target_ssid, target_pass)

        # 接続確認ループ
        timeout = 20
        while timeout > 0:
            if wlan.isconnected():
                ip = wlan.ifconfig()[0]
                log(f"✅ Wi-Fi接続成功! IP: {ip}", "INFO")
                # Wi-Fi接続成功時に NTP で時刻同期
                sync_ntp_time()
                return True, target_rssi
            
            st = wlan.status()
            if st in (-2, -3):
                log(f"DEBUG: 明らかなエラー検出 (status={st})", "DEBUG")
                break

            time.sleep(1)
            timeout -= 1

        log(f"❌ '{target_ssid}' 接続失敗。次のAPへ...", "WARN")

    return False, None

def send_to_gas(payload):
    """ GAS Web AppへHTTP POSTでデータ送信 """
    if not GAS_URL:
        log("config.py に GAS_URL が設定されていません", "ERROR")
        return False

    headers = {"Content-Type": "application/json"}
    try:
        response = urequests.post(GAS_URL, json=payload, headers=headers)
        status = response.status_code
        if status != 200:
            log(f"GAS送信エラー (Status: {status}): {response.text}", "WARN")
        response.close()
        return status == 200
    except Exception as e:
        log(f"GAS送信例外: {e}", "ERROR")
        return False

# ==================================================
# 5. GitHub OTA（自動更新）処理
# ==================================================
def check_github_ota():
    """ GitHub上の最新 main.py とローカルを比較し、差分があれば上書きして再起動 """
    user = getattr(config, "GITHUB_USER", None)
    repo = getattr(config, "GITHUB_REPO", None)
    branch = getattr(config, "GITHUB_BRANCH", "main")
    token = getattr(config, "GITHUB_TOKEN", None)

    if not (user and repo and token):
        log("OTAチェック定義が config.py に不十分なためスキップします", "WARN")
        return

    url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/main.py"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Pico-OTA"
    }

    log("☁️ GitHub の最新コード（main.py）を確認中...", "INFO")
    try:
        res = urequests.get(url, headers=headers)
        if res.status_code == 200:
            remote_code = res.text
            res.close()

            # ローカルの main.py と比較
            local_code = ""
            try:
                with open("main.py", "r") as f:
                    local_code = f.read()
            except Exception:
                pass

            if remote_code != local_code:
                # GAS記述が含まれていない旧コード（Ubidots版等）による誤上書きを防止
                if "GAS_URL" not in remote_code and "send_to_gas" not in remote_code:
                    log("⚠️ GitHub上のコードが旧バージョン(Ubidots版等)のため、OTA上書きを安全にスキップしました。", "WARN")
                    return

                log("🔄 GitHub上に新しい更新を検出しました！ 書き換えて再起動します...", "WARN")
                with open("main.py", "w") as f:
                    f.write(remote_code)
                time.sleep(1)
                machine.reset()  # 本体再起動
            else:
                log(f"✅ コードは最新状態です (Ver: {VERSION})", "INFO")
        else:
            log(f"GitHub取得エラー: ステータスコード {res.status_code}", "WARN")
            res.close()
    except Exception as e:
        log(f"OTA更新確認例外: {e}", "ERROR")

# ==================================================
# 6. ローカルバッファ制御処理 (オフライン対策)
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
        success = send_to_gas(item)
        if success:
            log(f"  └ 成功 ({idx}/{len(buffer)})", "INFO")
            time.sleep_ms(200)
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
# 7. メイン実行シーケンス (5分周期ループ)
# ==================================================
def run_one_cycle():
    """ 1回分の「計測 → 接続 → OTA確認 → 送信/バッファ → 切断」シーケンス """
    # 1. センサー計測
    led.value(1)
    temp_box, temp_house = read_temperatures()
    vsys = read_vsys()
    led.value(0)

    # 2. Wi-Fi 接続
    wifi_ok, rssi = scan_and_connect_best()

    # 3. 毎サイクル GitHub OTA チェックを実施 (Wi-Fi接続時)
    if wifi_ok:
        check_github_ota()

    # 4. ペイロード作成 (GAS送信仕様)
    payload = {
        "device_id": DEVICE_LABEL,
        "temp_box": temp_box,
        "temp_house": temp_house,
        "vsys_voltage": vsys,
        "rssi": rssi
    }

    # 5. 送信またはバッファリング
    if wifi_ok and temp_box is not None:
        log(f"計測完了 -> BOX: {temp_box}℃ / ハウス: {temp_house}℃ / VSYS: {vsys}V / RSSI: {rssi}dBm", "INFO")
        
        # 未送信バッファがあれば先に送信
        flush_buffer()
        
        log("GASへ最新データを送信中...", "INFO")
        if send_to_gas(payload):
            log("★ 最新データの送信成功!", "INFO")
        else:
            log("❌ 送信失敗。バッファへ退避します", "WARN")
            save_to_buffer(payload)
    else:
        log(f"オフライン計測 -> BOX: {temp_box}℃ / ハウス: {temp_house}℃ / VSYS: {vsys}V", "WARN")
        if temp_box is not None:
            save_to_buffer(payload)
            
    # 6. 通信切断（省電力化）
    try:
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            wlan.disconnect()
    except Exception:
        pass

def main():
    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス (5分周期・毎サイクルOTA/NTP/動的AP/バッファ対応)")
    print(f"  デバイスラベル: {DEVICE_LABEL}")
    print("=" * 50)

    while True:
        # NTP同期で時刻が補正されても影響を受けないミリ秒タイマーを使用
        start_ticks = time.ticks_ms()
        
        try:
            run_one_cycle()
        except Exception as e:
            log(f"メインループ内例外発生: {e}", "ERROR")

        # 経過ミリ秒を秒に変換して待機時間を計算
        elapsed_ms = time.ticks_diff(time.ticks_ms(), start_ticks)
        elapsed_sec = elapsed_ms / 1000.0
        
        sleep_time = max(1, int(INTERVAL_SEC - elapsed_sec))
        
        log(f"⏳ 処理時間: {elapsed_sec:.1f}秒 ➔ 次の計測まで {sleep_time}秒 待機します", "INFO")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()


### まとめと動作のポイント
* `main.py` を上記のコードに置き換えて保存することで、システムの GAS バックエンドと完全に対応した動作になります。
* Thonny等で実行し、ターミナルログで `BOX: XX℃ / ハウス: XX℃` のように両方の温度が正常取得され、`GASデータ送信成功!` が表示されるかご確認ください。
