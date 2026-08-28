🚀 保冷BOX 統合熱動態＆通信残量監視システム

Raspberry Pi Pico W / Pico 2 W を使用し、ビニールハウス内に設置した保冷BOXの内部環境と、それを駆動するバッテリーの状態を自動監視・予測するシステムです。

🌟 主な機能

2セット4chのマルチ温度モニタリング:
1本のデータ線 (1-Wire) で2つのDS18B20センサー（BOX内温度・ハウス内温度）を並列接続し、正確に識別・計測します。

バッテリー降下率からの通信断 AI予測:
PicoのVSYS電圧降下を最小二乗法で常時計算し、Wi-Fi通信限界である 3.4V に達する時間を予測します。

35℃到達限界 AI熱結合モデル:
朝からの日照によるハウス内の温度上昇カーブと、保冷BOXの吸熱勾配を掛け合わせ、限界の35℃に到達する時間を動的に推測します。

Apple HIG ライクなUIダッシュボード:
ガラスモーフィズムを活用したモダンな index.html だけで完結するダッシュボード。Chart.jsによるズーム・パンに対応しています。

📂 ファイル構成

coolerbox-monitor/
├── index.html        # Webダッシュボード (2セット4ch対応UI)
├── main.py          # Pico用 MicroPythonメインコード
├── config.py        # Wi-Fi / GAS / GitHub OTA 設定ファイル
├── Code.gs          # Google Apps Script (GAS) バックエンド
└── README.md        # プロジェクト説明 (このファイル)
