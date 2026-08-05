# ota_updater.py
import urequests
import os
import machine
import config

def check_and_update():
    """ GitHubから最新の main.py をダウンロードして更新する """
    print(f"[{config.DEVICE_NAME}] OTA: 最新プログラムの確認中...")
    
    # Rawコンテンツ取得URL
    url = f"https://raw.githubusercontent.com/{config.GITHUB_USER}/{config.GITHUB_REPO}/{config.GITHUB_BRANCH}/main.py"
    headers = {
        "User-Agent": "Pico-OTA-Client",
        "Authorization": f"token {config.GITHUB_TOKEN}"
    }

    try:
        res = urequests.get(url, headers=headers)
        if res.status_code == 200:
            new_code = res.text
            res.close()
            
            # 現在の main.py を読み込み比較
            current_code = ""
            try:
                with open("main.py", "r") as f:
                    current_code = f.read()
            except Exception:
                pass

            # 差分がある場合のみ更新して再起動
            if new_code != current_code:
                print(f"[{config.DEVICE_NAME}] OTA: 新しい main.py を検出しました。更新中...")
                with open("main.py", "w") as f:
                    f.write(new_code)
                print(f"[{config.DEVICE_NAME}] OTA: 更新完了。再起動します...")
                machine.reset()
            else:
                print(f"[{config.DEVICE_NAME}] OTA: すでに最新バージョンです。")
                return True
        else:
            print(f"[{config.DEVICE_NAME}] OTA: チェック失敗 (HTTP {res.status_code})")
            res.close()
            return False
    except Exception as e:
        print(f"[{config.DEVICE_NAME}] OTA エラー: {e}")
        return False
