import os, time, requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import threading, schedule

app = Flask(__name__)
TOKEN  = os.environ["LINE_TOKEN"]
SECRET = os.environ["LINE_SECRET"]
MY_ID  = os.environ["LINE_USER_ID"]
NETLIFY_URL = os.environ.get("NETLIFY_URL", "")

configuration = Configuration(access_token=TOKEN)
handler = WebhookHandler(SECRET)

# ── 自選股 & 警示（記憶體）──────────────────────────────
watchlist = ["2330", "0050"]
alerts = {}  # {stock_id: threshold_%}

# ── 抓股價 ────────────────────────────────────────────
def get_price(stock_id):
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw&_={int(time.time())}"
        r = requests.get(url, timeout=8)
        item = r.json()["msgArray"][0]
        price = float(item.get("z") or item.get("y", 0))
        prev  = float(item.get("y", price))
        name  = item.get("n", stock_id)
        chg   = round(price - prev, 2)
        pct   = round((chg / prev) * 100, 2) if prev else 0
        return {"id": stock_id, "name": name, "price": price, "change": chg, "pct": pct}
    except:
        return None

def is_trading():
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 60 + now.minute
    return 9*60 <= hm < 13*60+30

# ── 警示檢查 ──────────────────────────────────────────
triggered_today = set()

def check_alerts():
    if not is_trading(): return
    from datetime import date
    today = str(date.today())
    for sid, thresh in list(alerts.items()):
        key = f"{today}_{sid}"
        if key in triggered_today: continue
        s = get_price(sid)
        if not s: continue
        if abs(s["pct"]) >= thresh:
            triggered_today.add(key)
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            msg = (f"🔔 股價警示！\n"
                   f"{s['id']} {s['name']}\n"
                   f"{arrow} {abs(s['pct']):.2f}%　現價 {s['price']}\n"
                   f"門檻：±{thresh}%")
            push(msg)

# ── 每日收盤報告 ──────────────────────────────────────
def daily_report():
    lines = ["📊 今日收盤報告\n"]
    for sid in watchlist:
        s = get_price(sid)
        if not s: continue
        arrow = "🔺" if s["pct"] > 0 else "🔻"
        lines.append(f"{arrow} {s['id']} {s['name']}\n   {s['price']}  {s['change']:+.2f} ({s['pct']:+.2f}%)")
    push("\n".join(lines))

def push(msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_ID, messages=[TextMessage(text=msg)])
        )

# ── LINE Webhook ──────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    sig  = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try: handler.handle(body, sig)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_msg(event):
    text = event.message.text.strip()
    uid  = event.source.user_id

    if text == "報告":
        lines = ["📊 自選股即時行情\n"]
        for sid in watchlist:
            s = get_price(sid)
            if not s: continue
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            lines.append(f"{arrow} {s['id']} {s['name']}\n   {s['price']}  {s['change']:+.2f} ({s['pct']:+.2f}%)")
        reply(event, "\n".join(lines))

    elif text == "網頁":
        reply(event, f"📈 台股追蹤網頁\n{NETLIFY_URL}")

    elif text.isdigit() and len(text) == 4:
        s = get_price(text)
        if s:
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            reply(event, f"{arrow} {s['id']} {s['name']}\n現價：{s['price']}\n漲跌：{s['change']:+.2f} ({s['pct']:+.2f}%)")
        else:
            reply(event, f"找不到 {text}")

    elif text.startswith("警示 "):
        parts = text.split()
        if len(parts) == 3:
            sid, thresh = parts[1], float(parts[2])
            alerts[sid] = thresh
            reply(event, f"🔔 已設定 {sid} 漲跌超過 ±{thresh}% 通知")
        else:
            reply(event, "格式：警示 2330 5")

    else:
        reply(event, "指令說明：\n• 報告 → 自選股行情\n• 網頁 → 開啟追蹤網頁\n• 2330 → 查詢股價\n• 警示 2330 5 → 設定警示")

def reply(event, msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=msg)])
        )

# ── 排程 ──────────────────────────────────────────────
def run_schedule():
    schedule.every(15).seconds.do(check_alerts)
    schedule.every().day.at("13:35").do(daily_report)
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=run_schedule, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
