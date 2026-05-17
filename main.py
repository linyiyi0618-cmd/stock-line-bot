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
TOKEN  = os.environ["LINE_TOKEN"]
SECRET = os.environ["LINE_SECRET"]
MY_ID  = os.environ["LINE_USER_ID"]
NETLIFY_URL = os.environ.get("NETLIFY_URL", "")

configuration = Configuration(access_token=TOKEN)
handler = WebhookHandler(SECRET)

# ── 自選股 & 警示（記憶體）──────────────────────────────
watchlist = ["2330", "0050"]
alerts = {}  # {stock_id: threshold_%}

# ── 抓股價（盤中即時 + 盤後歷史備援）────────────────────
def get_price(stock_id):
    # 層1：TWSE 即時行情（盤中）
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw&_={int(time.time())}"
        r = requests.get(url, timeout=8)
        item = r.json()["msgArray"][0]
        name = item.get("n", stock_id)
        price = float(item.get("z") or 0)
        prev  = float(item.get("y") or 0)
        if price > 0 and prev > 0:
            chg = round(price - prev, 2)
            pct = round((chg / prev) * 100, 2)
            return {"id": stock_id, "name": name, "price": price,
                    "change": chg, "pct": pct, "source": "即時"}
        # 非盤中：z="-"，用昨收當現價
        if prev > 0:
            return {"id": stock_id, "name": name, "price": prev,
                    "change": 0, "pct": 0, "source": "昨收"}
    except:
        pass

    # 層2：TWSE 每月收盤資料（備援）
    try:
        now = datetime.now()
        for delta in range(3):
            m = now.month - delta
            y = now.year
            if m <= 0:
                m += 12
                y -= 1
            ym = f"{y}{m:02d}01"
            url2 = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ym}&stockNo={stock_id}&response=json"
            r2 = requests.get(url2, timeout=8)
            d = r2.json()
            if d.get("data") and len(d["data"]) > 0:
                rows = d["data"]
                row  = rows[-1]
                price = float(row[6].replace(",", ""))
                prev2 = float(rows[-2][6].replace(",", "")) if len(rows) > 1 else price
                chg   = round(price - prev2, 2)
                pct   = round((chg / prev2) * 100, 2) if prev2 else 0
                title = d.get("title", "")
                name  = title.split(" ")[2] if len(title.split(" ")) > 2 else stock_id
                return {"id": stock_id, "name": name, "price": price,
                        "change": chg, "pct": pct, "source": f"{row[0]} 收盤"}
    except:
        pass

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
        reply(event, "\n".join(lines) if len(lines)>1 else "無法取得資料")

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
                f"資料：{s.get('source','')}"
            )
        else:
            reply(event, f"找不到 {text}，請確認代號正確（僅支援上市股票）")

    elif text.startswith("警示 "):
        parts = text.split()
        if len(parts) == 3:
            sid, thresh = parts[1], float(parts[2])
            alerts[sid] = thresh
            reply(event, f"🔔 已設定 {sid} 漲跌超過 ±{thresh}% 通知")
        else:
            reply(event, "格式：警示 2330 5")

    elif text == "指令" or text == "help":
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
