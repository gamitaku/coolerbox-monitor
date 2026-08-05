# ota_updater.py
import urequests
import machine
import time
import config

# OTAで監視・更新する対象ファイル一覧
TARGET_FILES = ["ota_updater.py", "main.py"]
LOG_FILE = "ota_log.txt"

def write_log(message):
    timestamp = time.ticks_ms()
    log_msg = f"[{timestamp}] [{config.DEVICE_NAME}] {message}\n"
    print(log_msg.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_msg)
    except Exception:
        pass

def fetch_file_with_retry(filename, max_retries=3):
    """ 指定したファイルをGitHubからリトライ付きで取得 """
    url = f"https://raw.githubusercontent.com/{config.GITHUB_USER}/{config.GITHUB_REPO}/{config.GITHUB_BRANCH}/{filename}"
    headers = {
        "User-Agent": "Pico-OTA-Client",
        "Authorization": f"token {config.GITHUB_TOKEN}"
    }

    for attempt in range(1, max_retries + 1):
        try:
            write_log(f"OTA: DL試行 [{filename}] ({attempt}/{max_retries})...")
            res = urequests.get(url, headers=headers)
            if res.status_code == 200:
                content = res.text
                res.close()
                return content
            res.close()
        except Exception as e:
            write_log(f"OTA: [{filename}] 通信エラー ({e})")
        
        time.sleep(2 ** (attempt - 1)) # 指数バックオフ待機 (1s, 2s, 4s...)
    return None

def check_and_update():
    """ 全対象ファイルの更新チェック＆適用 """
    write_log("OTA: 一括更新チェック開始")
    updated_any = False

    for target in TARGET_FILES:
        new_code = fetch_file_with_retry(target)
        if new_code is None:
            write_log(f"OTA: [{target}] の取得に失敗。このファイルはスキップします。")
            continue

        # ローカルファイルの読み込み
        current_code = ""
        try:
            with open(target, "r") as f:
                current_code = f.read()
        except Exception:
            pass

        # 差分比較と書き換え
        if new_code != current_code:
            write_log(f"OTA: [{target}] の新しいバージョンを検出。更新中...")
            try:
                # バックアップ作成
                with open(f"{target}.bak", "w") as f_bak:
                    f_bak.write(current_code)
                
                # 本体更新
                with open(target, "w") as f_main:
                    f_main.write(new_code)
                
                updated_any = True
                write_log(f"OTA: [{target}] の更新完了。")
            except Exception as e:
                write_log(f"OTA: [{target}] 書き込みエラー ({e})")

    # いずれかのファイルが更新されていればハードウェア再起動
    if updated_any:
        write_log("OTA: 更新が適用されました。再起動を実行します。")
        time.sleep(1)
        machine.reset()
    else:
        write_log("OTA: 全ファイル最新状態です。")
