# ==========================================
# 保冷BOX統合監視システム 認証・設定ファイル
# ==========================================

# 1. デバイス識別名
# "保冷BOX本機" または "検証機" を指定してください
DEVICE_NAME = "保冷BOX本機" 

# 2. Wi-Fi接続設定 (電波が強いものに自動接続します)
WIFI_AP_LIST = [
    {"ssid": "YOUR_WIFI_SSID_1", "pass": "YOUR_WIFI_PASSWORD_1"},
    {"ssid": "YOUR_WIFI_SSID_2", "pass": "YOUR_WIFI_PASSWORD_2"}
]

# 3. Google Apps Script (GAS) Web App エンドポイントURL
# ※デプロイして発行された /exec で終わるURLを貼り付けてください
GAS_URL = "https://script.google.com/macros/s/AKfycbwLSeqjUq8Lhq6YDKxeJG2CxzLLTI-0l1b_GYtU7jDQBGiaGvkMh4kNAWqFEEfDupPm/exec"

# 4. GitHub OTA (自動アップデート機能) 用設定
GITHUB_USER = "gamitaku"             # ご自身のGitHubユーザー名
GITHUB_REPO = "coolerbox-monitor"    # リポジトリ名
GITHUB_BRANCH = "main"               # 監視するブランチ
GITHUB_TOKEN = ""                    # パブリックリポジトリの場合は空欄でOK