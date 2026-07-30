import network
import time
from machine import Pin, ADC, reset
import onewire, ds18x20
import urequests
import ujson
import socket

# ==================================================
# 0. config.py から設定情報・機密情報・校正値を読み込み
# ==================================================
# 機体ごとに異なる設定値（Wi-Fi情報、APIキー、VSYS校正係数等）は
# ローカルの config.py から読み込み、コードの共通化（GitHub/OTA管理）を図ります。
try:
    import config
    DEVICE_NAME = getattr(config, "DEVICE_NAME", "保冷BOX本機 (Pico W)")
    WIFI_SSID = config.WIFI_SSID
    WIFI_PASS = config.WIFI_PASS
    UBIDOTS_TOKEN = config.UBIDOTS_TOKEN
    GITHUB_TOKEN = getattr(config, "GITHUB_TOKEN", "")
    
    # 機器固有のVSYS電圧校正係数を読み込み
    # （例: 本機 Pico W = 0.00014793 / 検証機 Pico 2W = 0.00012234）
    VSYS_COEFF = getattr(config, "VSYS_COEFF", 0.00014793)
except ImportError:
    # config.py が読み込めない場合の安全用デフォルト値設定
    print("[エラー] config.py が見つかりません。デフォルト設定で動作します。")
    DEVICE_NAME = "保冷BOX (未設定)"
    WIFI_SSID = ""
    WIFI_PASS = ""
    UBIDOTS_TOKEN = ""
    GITHUB_TOKEN = ""
    VSYS_COEFF = 0.00014793

# ==================================================
# 1. システム設定・機体プロファイル判定
# ==================================================
# GitHub上の最新ソースコードの取得元URL（OTA用）
OTA_UPDATE_URL = "https://api.github.com/repos/gamitaku/coolerbox-monitor/contents/main.py"

# DEVICE_NAME の文字列から Ubidots 送信先のターゲットラベルを自動判別
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

# ハードウェアピン配置定義
DS_PIN_NUM = 15     # GP15: DS18B20 1-Wireデータピン
VSYS_ADC_NUM = 3    # GP29 / ADC3: VSYS電源電圧計測ピン
LED_PIN_NUM = "LED" # Pico W / Pico 2W 内蔵オンボードLED

# ==================================================
# 2. ハードウェア初期化 & グローバル状態管理
# ==================================================
led = Pin(LED_PIN_NUM, Pin.OUT)
ow = onewire.OneWire(Pin(DS_PIN_NUM))
ds = ds18x20.DS18X20(ow)
vsys_adc = ADC(VSYS_ADC_NUM)

# Webサーバー表示およびAPI応答用メッセージキャッシュ
latest_data = {
    "temp": None,
    "vsys": None,
    "rssi": None
}

# 起動〜計測〜送信工程の中で1つでもエラーが発生したかを保持するフラグ
has_error = False

# ==================================================
# 3. LED サイン制御関数
# ==================================================
# オンボード単色LEDの点滅スピードやパターンを変えることで、
# 画面を見なくても端末の動作状態（正常・処理中・エラー）を視認可能にします。
def set_led(mode):
    if mode == "ON":
        # 起動中・通信中・更新処理中（常時点灯）
        led.value(1)
    elif mode == "OFF":
        # 消灯
        led.value(0)
    elif mode == "BLINK_ONCE":
        # Webサーバーへのアクセス受信時に1回チカッと点滅
        led.value(1)
        time.sleep_ms(80)
        led.value(0)
    elif mode == "HEARTBEAT":
        # 正常運用中（待機中）を示す「心拍風」2回点滅（ポン・ポン）
        # 常時点灯を避けることで省電力化しつつ、フリーズしていないことを表示
        led.value(1)
        time.sleep_ms(60)
        led.value(0)
        time.sleep_ms(100)
        led.value(1)
        time.sleep_ms(60)
        led.value(0)
    elif mode == "ERROR_LOOP":
        # 各種障害発生時（Wi-Fi断、センサー異常、Ubidotsエラー）の非常警報表示
        # 高速点滅（チカチカチカ…）で目視異常検知を可能にする
        print(f"[{DEVICE_NAME}] [警告] システムエラー検出のため警告点滅を実行中...")
        for _ in range(50):  # 約10秒間高速点滅
            led.value(not led.value())
            time.sleep_ms(100)
        led.value(0)

# ==================================================
# 4. センサー計測処理関数の定義
# ==================================================
def read_temperature():
    """DS18B20から温度（℃）を取得。断線や未接続時は error フラグを立てる"""
    global has_error
    try:
        roms = ds.scan()
        if not roms:
            print(f"[{DEVICE_NAME}] [エラー] DS18B20が見つかりません（断線または未接続）")
            has_error = True
            return None
        
        # DS18B20に温度変換指示を出し、変換完了待ち（最大750ms必要）
        ds.convert_temp()
        time.sleep_ms(750)
        temp = ds.read_temp(roms[0])
        return round(temp, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] 温度計測処理で例外が発生: {e}")
        has_error = True
        return None

def read_vsys():
    """VSYS分圧回路(ADC3)から電源電圧（V）を取得し、機体個別の校正係数を適用して算出"""
    global has_error
    try:
        # PicoのADC3(GP29)はVSYS分圧出力に接続されているため入力モードに設定
        Pin(29, Pin.IN)
        time.sleep_ms(10)
        
        # ノイズ軽減のため10回サンプリングして平均値を算出
        total_raw = 0
        for _ in range(10):
            total_raw += vsys_adc.read_u16()
            time.sleep_ms(2)
        raw = total_raw // 10
        
        # 読み取り値が異常に低い場合は測定不能と判断
        if raw < 1000:
            voltage = 4.80
        else:
            # config.py から取得した各機体専用の校正係数を掛けて実効電圧を計算
            voltage = raw * VSYS_COEFF

        return round(voltage, 2)
    except Exception as e:
        print(f"[{DEVICE_NAME}] [エラー] VSYS電圧計測で例外が発生: {e}")
        has_error = True
        return None

# ==================================================
# 5. 通信・OTAアップデート処理関数の定義
# ==================================================
def connect_wifi():
    """Wi-Fiへの接続試行。失敗時は復旧用ループへ引き渡す"""
    global has_error
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    # 省電力モードを無効化（通信安定性の確保）
    try:
        wlan.config(pm=0xa11154)
    except Exception:
        pass

    if not wlan.isconnected():
        print(f"[{DEVICE_NAME}] Wi-Fi接続試行中 (SSID: {WIFI_SSID})...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        timeout = 15
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1
            
    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        rssi = wlan.status('rssi')
        print(f"[{DEVICE_NAME}] Wi-Fi接続成功! IPアドレス: {ip}, 電波強度: {rssi}dBm")
        return True, ip, rssi
    else:
        print(f"[{DEVICE_NAME}] [エラー] Wi-Fi接続に失敗しました")
        has_error = True
        return False, None, None

def check_and_update_ota():
    """GitHubリポジトリから最新の main.py を取得し、差分があれば自動上書き再起動"""
    if not OTA_UPDATE_URL:
        return

    print(f"[{DEVICE_NAME}] [OTA] GitHubから最新コードの更新確認中...")
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
            
            # 現在ローカルに保存されている main.py と比較
            try:
                with open("main.py", "r") as f:
                    current_code = f.read()
            except Exception:
                current_code = ""

            # GitHub側のコードに変更があればローカルを書き換えて再起動
            if new_code != current_code:
                print(f"[{DEVICE_NAME}] [OTA] ★ 新しいプログラムコードを検出! 上書き更新後、再起動します...")
                with open("main.py", "w") as f:
                    f.write(new_code)
                time.sleep(1)
                reset()  # リセット実行（以降は新コードで動作）
            else:
                print(f"[{DEVICE_NAME}] [OTA] 現在のプログラムコードは最新です")
        else:
            res.close()
            print(f"[{DEVICE_NAME}] [OTAエラー] 確認失敗 (HTTPステータス: {res.status_code})")
    except Exception as e:
        print(f"[{DEVICE_NAME}] [OTAエラー] 通信または書き込み失敗: {e}")

# ==================================================
# 6. Web サーバーレスポンス生成関数
# ==================================================
def make_html_response():
    """ブラウザ閲覧用ダッシュボードのHTML文字列を動的生成"""
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
# 7. メイン実行シーケンス & Web サーバーイベントループ
# ==================================================
def main():
    global has_error
    
    # 起動シーケンス開始時：オンボードLEDを常時点灯に設定
    set_led("ON")

    print("=" * 50)
    print(f"  {DEVICE_NAME} 起動シーケンス")
    print(f"  Ubidotsターゲットラベル: {DEVICE_LABEL}")
    print("=" * 50)
    
    # --------------------------------------------------
    # Step 1: Wi-Fi 接続
    # --------------------------------------------------
    wifi_ok, ip, rssi = connect_wifi()
    if not wifi_ok:
        # Wi-Fi接続失敗時はエラー点滅を出した上で5分間待機し、自動ソフトリセットで復旧を図る
        RETRY_WAIT_SEC = 300
        print(f"[{DEVICE_NAME}] [警告] Wi-Fi接続不可。5分後に再起動して自動リトライします...")
        set_led("ERROR_LOOP")
        reset()

    # --------------------------------------------------
    # Step 2: OTAプログラムコード更新チェック
    # --------------------------------------------------
    check_and_update_ota()

    # --------------------------------------------------
    # Step 3: 各種センサー値の計測
    # --------------------------------------------------
    temp = read_temperature()
    vsys = read_vsys()
    
    # キャッシュデータを更新（Webサーバー等から参照される）
    latest_data["temp"] = temp
    latest_data["vsys"] = vsys
    latest_data["rssi"] = rssi
    
    print(f"[{DEVICE_NAME}] 計測完了 -> 温度: {temp}℃ / VSYS: {vsys}V (適用係数:{VSYS_COEFF}) / RSSI: {rssi}dBm")
    
    # --------------------------------------------------
    # Step 4: Ubidots クラウドプラットフォームへのデータ送信
    # --------------------------------------------------
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
        if res.status_code != 200 and res.status_code != 201:
            print(f"[{DEVICE_NAME}] [エラー] Ubidotsへの送信が拒否されました (HTTP {res.status_code})")
            has_error = True
        res.close()
    except Exception as e:
        print(f"[{DEVICE_NAME}] [送信例外] Ubidotsへのデータ送信中にエラーが発生: {e}")
        has_error = True

    # 起動処理がすべて完了したため、常時点灯パターンを解除
    set_led("OFF")

    # ここまでの工程（計測・送信等）で1つでも障害を検知していれば警告点滅を挟む
    if has_error:
        set_led("ERROR_LOOP")

    # --------------------------------------------------
    # Step 5: ローカル HTTP Web サーバーの開始と常駐ループ
    # --------------------------------------------------
    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    print(f"[{DEVICE_NAME}] ★ Webサーバー起動完了: http://{ip}/ でアクセス可能")

    last_heartbeat = time.time()

    # 常駐イベントループ（Webリクエストの受信用）
    while True:
        try:
            # 【5秒周期のステータス表示】
            # 非同期でWebリクエストを待ちながら、正常運用中を示すハートビート点滅を行う
            if time.time() - last_heartbeat >= 5:
                if not has_error:
                    set_led("HEARTBEAT")
                else:
                    set_led("ERROR_LOOP")
                last_heartbeat = time.time()

            # Web受信待ちでプログラムが永久停止しないよう1秒のタイムアウトを設定
            s.settimeout(1.0)
            try:
                cl, client_addr = s.accept()
            except OSError:
                # リクエストがない場合はタイムアウトしてループの先頭（ハートビート判定）に戻る
                continue

            # クライアント（PC/スマホ）からのアクセスを受信したらLEDを1回点滅
            set_led("BLINK_ONCE")

            req = cl.recv(1024).decode('utf-8')
            
            # エンドポイント判定（JSONデータ返却用API または HTML画面）
            if "GET /api " in req:
                response_body = ujson.dumps(latest_data)
                cl.send('HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n')
                cl.send(response_body)
            else:
                response_body = make_html_response()
                cl.send('HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n')
                cl.send(response_body)
                
            cl.close()
        except Exception as e:
            print(f"[{DEVICE_NAME}] Webリクエスト処理中にエラーが発生: {e}")

if __name__ == "__main__":
    main()
