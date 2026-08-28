/**
 * 温度プロファイリング & 予測算出関数 (ハウス内温室熱動態モデル対応)
 */
function calculatePredictionModel(boxTemp, houseTemp) {
  const ss = getSpreadsheet();
  const dataSheet = ss.getSheetByName('Data');
  if (!dataSheet) return { estimatedReachTime: null, estimatedHours: 0, houseForecastCurve: [] };
  
  const dataRows = dataSheet.getDataRange().getValues();
  if (dataRows.length < 2) return { estimatedReachTime: null, estimatedHours: 0, houseForecastCurve: [] };

  const latest = dataRows[dataRows.length - 1];
  const bTemp = boxTemp || parseFloat(latest[2]);
  const hTemp = houseTemp || parseFloat(latest[3]);

  // 1. ハウス内温度のAI予測（正午〜13:30の熱蓄積ピークと直近勾配を組み合わせた温室モデル）
  const now = new Date();
  const houseForecastCurve = [];
  const steps = 12; // 予測ステップ
  
  // 吸熱モデル計算
  const k = 0.08; 
  const tempDiff = Math.max(1, hTemp - bTemp);
  const riseRatePerHour = Math.max(0.5, tempDiff * k);

  const remainingTemp = 35.0 - bTemp;
  let estimatedHours = remainingTemp > 0 ? remainingTemp / riseRatePerHour : 0;
  const estimatedReachTime = new Date(now.getTime() + estimatedHours * 60 * 60 * 1000);

  // 未来の各ステップにおけるハウス内予測温度を算出
  for (let i = 0; i <= steps; i++) {
    const futureTime = new Date(now.getTime() + (estimatedHours / steps) * i * 3600 * 1000);
    const hour = futureTime.getHours() + futureTime.getMinutes() / 60;
    
    // 日周正弦波モデル（ハウス内の温室特性）
    const baseSolarTemp = 28 + 11 * Math.sin((hour - 7.5) * Math.PI / 12);
    const initialOffset = hTemp - (28 + 11 * Math.sin((now.getHours() + now.getMinutes() / 60 - 7.5) * Math.PI / 12));
    const decay = Math.exp(-i / 4);
    
    const predHouse = Math.max(15, Math.min(48, Number((baseSolarTemp + initialOffset * decay).toFixed(1))));
    houseForecastCurve.push(predHouse);
  }

  return {
    estimatedReachTime: estimatedReachTime,
    estimatedHours: estimatedHours,
    riseRatePerHour: riseRatePerHour,
    houseForecastCurve: houseForecastCurve
  };
}

/**
 * LINE Botメッセージ応答処理
 */
function handleLineWebhook(events) {
  for (const event of events) {
    if (event.type === 'message' && event.message.type === 'text') {
      const userText = event.message.text.trim();
      const replyToken = event.replyToken;

      const ss = getSpreadsheet();
      const dataSheet = ss.getSheetByName('Data');
      const dataRows = dataSheet.getDataRange().getValues();

      if (dataRows.length > 1) {
        const latest = dataRows[dataRows.length - 1];
        const time = new Date(latest[0]).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
        const boxTemp = latest[2];
        const houseTemp = latest[3];
        const vsys = latest[4];
        
        const pred = calculatePredictionModel(boxTemp, houseTemp);
        const predTime = pred.estimatedReachTime ? pred.estimatedReachTime.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' }) : "計測中";

        let replyMsg = `📱 **保冷BOX 最新ステータス** (${time})\n\n`;
        replyMsg += `🌡️ BOX内温度: ${boxTemp} ℃\n`;
        replyMsg += `☀️ ハウス内温度: ${houseTemp} ℃\n`;
        replyMsg += `🔋 バッテリー: ${vsys} V\n`;
        replyMsg += `⏳ 35℃到達予想: 【${predTime} 頃】\n`;
        replyMsg += `（あと約 ${pred.estimatedHours.toFixed(1)} 時間）`;

        replyLineMessage(replyToken, replyMsg);
      }
    }
  }
}

/**
 * LINE Push通知送信
 */
function sendLinePushNotification(message) {
  if (LINE_CHANNEL_ACCESS_TOKEN === "YOUR_LINE_CHANNEL_ACCESS_TOKEN_HERE") return;
  const url = "https://api.line.me/v2/bot/message/broadcast";
  const payload = { messages: [{ type: "text", text: message }] };
  const options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + LINE_CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify(payload)
  };
  UrlFetchApp.fetch(url, options);
}

/**
 * LINE Reply応答送信
 */
function replyLineMessage(replyToken, message) {
  if (LINE_CHANNEL_ACCESS_TOKEN === "YOUR_LINE_CHANNEL_ACCESS_TOKEN_HERE") return;
  const url = "https://api.line.me/v2/bot/message/reply";
  const payload = { replyToken: replyToken, messages: [{ type: "text", text: message }] };
  const options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + LINE_CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify(payload)
  };
  UrlFetchApp.fetch(url, options);
}

// 簡単なダミー関数 (環境に合わせて doPost 等を実装してください)
function getSpreadsheet() {
  return SpreadsheetApp.getActiveSpreadsheet();
}