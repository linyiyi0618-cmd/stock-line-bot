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
from datetime import datetime, date

app = Flask(__name__)
TOKEN       = os.environ["LINE_TOKEN"]
SECRET      = os.environ["LINE_SECRET"]
MY_ID       = os.environ["LINE_USER_ID"]
NETLIFY_URL = os.environ.get("NETLIFY_URL", "")

configuration = Configuration(access_token=TOKEN)
handler = WebhookHandler(SECRET)

watchlist = ["2330", "0050"]
alerts = {}

# ── 抓股價（Yahoo Finance 後端版，全天候可用）────────────
def get_price(stock_id):
    try:
        symbol = f"{stock_id}.TW"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        price    = round(meta.get("regularMarketPrice", 0), 2)
        prev     = round(meta.get("chartPreviousClose") or meta.get("previousClose", price), 2)
        name     = meta.get("longName") or meta.get("shortName") or stock_id
        chg      = round(price - prev, 2)
        pct      = round((chg / prev) * 100, 2) if prev else 0
        mkt_time = datetime.fromtimestamp(meta.get("regularMarketTime", time.time()))
        source   = mkt_time.strftime("%m/%d %H:%M")
        return {"id": stock_id, "name": name, "price": price,
                "change": chg, "pct": pct, "source": source}
    except Exception as e:
        print(f"[get_price error] {stock_id}: {e}")
        return None

def is_trading():
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 60 + now.minute
    return 9*60 <= hm < 13*60+30

# ── 警示檢查 ──────────────────────────────────────────
triggered_today = set()

def check_alerts():
    if not is_trading(): return
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

    if text == "報告":
        lines = ["📊 自選股行情\n"]
        for sid in watchlist:
            s = get_price(sid)
            if not s: continue
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            lines.append(
                f"{arrow} {s['id']} {s['name']}\n"
                f"   現價 {s['price']}  {s['change']:+.2f} ({s['pct']:+.2f}%)\n"
                f"   [{s.get('source','')}]"
            )
        reply(event, "\n".join(lines) if len(lines) > 1 else "無法取得資料")

    elif text == "網頁":
        reply(event, f"📈 台股追蹤網頁\n{NETLIFY_URL}")

    elif text.isdigit() and len(text) == 4:
        s = get_price(text)
        if s:
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            reply(event,
                f"{arrow} {s['id']} {s['name']}\n"
                f"現價：{s['price']}\n"
                f"漲跌：{s['change']:+.2f} ({s['pct']:+.2f}%)\n"
                f"時間：{s.get('source','')}"
            )
        else:
            reply(event, f"找不到 {text}，請確認代號正確")

    elif text.startswith("警示 "):
        parts = text.split()
        if len(parts) == 3:
            sid, thresh = parts[1], float(parts[2])
            alerts[sid] = thresh
            reply(event, f"🔔 已設定 {sid} 漲跌超過 ±{thresh}% 通知")
        else:
            reply(event, "格式：警示 2330 5")

    elif text in ["指令", "help", "選單"]:
        reply(event,
            "📋 指令說明\n"
            "──────────\n"
            "• 2330 → 查詢股價\n"
            "• 報告 → 自選股行情\n"
            "• 網頁 → 開啟追蹤網頁\n"
            "• 警示 2330 5 → 漲跌超過5%通知\n"
            "• 指令 → 顯示此說明"
        )

    else:
        reply(event,
            "輸入股票代號查詢，例如：2330\n"
            "或輸入「指令」查看所有功能"
        )

def reply(event, msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
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
