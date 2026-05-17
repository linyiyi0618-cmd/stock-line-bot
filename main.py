import os, time, requests, json
from urllib.parse import quote
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
    ImageMessage
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

SPECIAL = {
    "台指期": ("IX0126.TW", "期貨指數"),
    "小台":   ("IX0126.TW", "期貨指數"),
    "加權":   ("^TWII",     "指數"),
    "那斯達克":("^IXIC",    "指數"),
    "道瓊":   ("^DJI",      "指數"),
    "標普":   ("^GSPC",     "指數"),
    "費半":   ("^SOX",      "指數"),
    "黃金":   ("GC=F",      "期貨"),
    "原油":   ("CL=F",      "期貨"),
    "美元":   ("DX-Y.NYB",  "指數"),
}

# ── Yahoo Finance ─────────────────────────────────────
def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()
    result = data["chart"]["result"]
    if not result: return None
    meta  = result[0]["meta"]
    price = round(meta.get("regularMarketPrice", 0), 2)
    prev  = round(meta.get("chartPreviousClose") or meta.get("previousClose", price), 2)
    name  = meta.get("longName") or meta.get("shortName") or symbol
    if price <= 0: return None
    chg = round(price - prev, 2)
    pct = round((chg / prev) * 100, 2) if prev else 0
    ts  = meta.get("regularMarketTime", time.time())
    src = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")
    return {"price": price, "change": chg, "pct": pct, "name": name, "source": src}

def fetch_history(stock_id):
    """抓近30天收盤價，回傳 {dates, closes}"""
    for suffix in [".TW", ".TWO"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?interval=1d&range=60d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            result = data["chart"]["result"]
            if not result: continue
            timestamps = result[0]["timestamp"]
            closes     = result[0]["indicators"]["quote"][0]["close"]
            dates, prices = [], []
            for ts, c in zip(timestamps, closes):
                if c is None: continue
                dates.append(datetime.fromtimestamp(ts).strftime("%m/%d"))
                prices.append(round(c, 2))
            if dates:
                return {"dates": dates[-30:], "closes": prices[-30:]}
        except Exception as e:
            print(f"[history] {stock_id}{suffix}: {e}")
    return None

def get_price(stock_id):
    for suffix, market in [(".TW", "上市"), (".TWO", "上櫃")]:
        try:
            result = fetch_yahoo(f"{stock_id}{suffix}")
            if result:
                result["id"] = stock_id
                result["market"] = market
                return result
        except Exception as e:
            print(f"[{suffix}] {stock_id}: {e}")
    return None

def get_special(keyword):
    symbol, market = SPECIAL[keyword]
    try:
        result = fetch_yahoo(symbol)
        if result:
            result["id"] = keyword
            result["market"] = market
            return result
    except Exception as e:
        print(f"[special] {keyword}: {e}")
    return None

def is_trading():
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 60 + now.minute
    return 9*60 <= hm < 13*60+30

# ── K線圖（QuickChart）────────────────────────────────
def make_chart_url(stock_id, name, hist):
    dates  = hist["dates"]
    closes = hist["closes"]
    up     = closes[-1] >= closes[0]
    color  = "rgb(46,204,113)" if up else "rgb(231,76,60)"
    fill   = "rgba(46,204,113,0.15)" if up else "rgba(231,76,60,0.15)"

    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [{
                "label": f"{stock_id} {name}",
                "data": closes,
                "borderColor": color,
                "backgroundColor": fill,
                "borderWidth": 2,
                "pointRadius": 0,
                "fill": True,
                "tension": 0.3
            }]
        },
        "options": {
            "plugins": {
                "legend": {"display": True, "labels": {"color": "#FFFFFF", "font": {"size": 14}}},
                "title": {
                    "display": True,
                    "text": f"{stock_id} {name}  近30日走勢",
                    "color": "#FFFFFF",
                    "font": {"size": 16}
                }
            },
            "scales": {
                "x": {
                    "ticks": {"color": "#AAAAAA", "maxTicksLimit": 8},
                    "grid": {"color": "rgba(255,255,255,0.08)"}
                },
                "y": {
                    "ticks": {"color": "#AAAAAA"},
                    "grid": {"color": "rgba(255,255,255,0.08)"}
                }
            },
            "backgroundColor": "#1A1A2E"
        }
    }
    config_str = json.dumps(chart_config, ensure_ascii=False)
    encoded    = quote(config_str)
    url = f"https://quickchart.io/chart?c={encoded}&width=800&height=400&backgroundColor=%231A1A2E"
    return url

# ── Flex Message 卡片 ─────────────────────────────────
def make_flex_card(s):
    up    = s["pct"] >= 0
    color = "#27AE60" if up else "#E74C3C"
    bg    = "#F0FFF4" if up else "#FFF5F5"
    arrow = "▲" if up else "▼"
    sign  = "+" if up else ""
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "14px",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": str(s["id"]), "color": "#FFFFFF",
                     "size": "sm", "weight": "bold", "flex": 1},
                    {"type": "text", "text": s.get("market",""), "color": "#FFFFFF99",
                     "size": "xs", "align": "end"}
                ]},
                {"type": "text", "text": s["name"], "color": "#FFFFFF",
                 "size": "md", "weight": "bold", "margin": "sm", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "backgroundColor": bg, "paddingAll": "14px",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "現價", "color": "#888888", "size": "sm", "flex": 1},
                    {"type": "text", "text": f"{s['price']:,.2f}", "color": color,
                     "size": "xl", "weight": "bold", "align": "end"}
                ]},
                {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                    {"type": "text", "text": "漲跌", "color": "#888888", "size": "sm", "flex": 1},
                    {"type": "text",
                     "text": f"{arrow} {sign}{s['change']:.2f}  ({sign}{s['pct']:.2f}%)",
                     "color": color, "size": "sm", "weight": "bold", "align": "end"}
                ]},
                {"type": "separator", "margin": "md", "color": "#DDDDDD"},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "資料時間", "color": "#AAAAAA", "size": "xs", "flex": 1},
                    {"type": "text", "text": s.get("source","—"), "color": "#AAAAAA",
                     "size": "xs", "align": "end"}
                ]}
            ]
        }
    }
    return FlexMessage(
        alt_text=f"{s['id']} {s['name']} {s['price']}",
        contents=FlexContainer.from_dict(bubble)
    )

def make_report_flex(stocks):
    rows = []
    for s in stocks:
        up    = s["pct"] >= 0
        color = "#27AE60" if up else "#E74C3C"
        arrow = "▲" if up else "▼"
        sign  = "+" if up else ""
        rows.append({
            "type": "box", "layout": "horizontal", "paddingAll": "10px",
            "contents": [
                {"type": "box", "layout": "vertical", "flex": 2, "contents": [
                    {"type": "text", "text": s["id"], "size": "xs", "color": "#888888"},
                    {"type": "text", "text": s["name"], "size": "sm", "weight": "bold", "wrap": True}
                ]},
                {"type": "box", "layout": "vertical", "flex": 2, "contents": [
                    {"type": "text", "text": f"{s['price']:,.2f}", "size": "md",
                     "weight": "bold", "color": color, "align": "end"},
                    {"type": "text", "text": f"{arrow} {sign}{s['pct']:.2f}%",
                     "size": "xs", "color": color, "align": "end"}
                ]}
            ]
        })
        rows.append({"type": "separator", "color": "#EEEEEE"})
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A1A2E", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📊 自選股行情",
                 "color": "#FFFFFF", "size": "md", "weight": "bold"},
                {"type": "text", "text": datetime.now().strftime("%m/%d %H:%M"),
                 "color": "#FFFFFF88", "size": "xs", "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "0px",
            "contents": rows[:-1]
        }
    }
    return FlexMessage(alt_text="自選股行情報告",
                       contents=FlexContainer.from_dict(bubble))

def make_watchlist_flex():
    if not watchlist: return None
    rows = []
    for sid in watchlist:
        rows.append({
            "type": "box", "layout": "horizontal",
            "paddingAll": "10px",
            "contents": [
                {"type": "text", "text": sid, "size": "sm",
                 "weight": "bold", "flex": 1, "gravity": "center"},
                {"type": "button",
                 "action": {"type": "message", "label": "刪除", "text": f"刪除 {sid}"},
                 "style": "secondary", "height": "sm", "flex": 1, "color": "#FF6B6B"}
            ]
        })
        rows.append({"type": "separator", "color": "#EEEEEE"})
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A1A2E", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "⭐ 自選股清單",
                 "color": "#FFFFFF", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"共 {len(watchlist)} 檔",
                 "color": "#FFFFFF88", "size": "xs", "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "0px",
            "contents": rows[:-1]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "輸入「新增 2330」加入自選股",
                 "color": "#AAAAAA", "size": "xs", "align": "center"}
            ]
        }
    }
    return FlexMessage(alt_text=f"自選股清單（{len(watchlist)} 檔）",
                       contents=FlexContainer.from_dict(bubble))

# ── 警示 & 排程 ───────────────────────────────────────
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
            push_text(
                f"🔔 股價警示！\n{s['id']} {s['name']}\n"
                f"{arrow} {abs(s['pct']):.2f}%　現價 {s['price']}\n"
                f"門檻：±{thresh}%"
            )

def daily_report():
    stocks = [get_price(sid) for sid in watchlist]
    stocks = [s for s in stocks if s]
    if stocks: push_flex(make_report_flex(stocks))

def push_text(msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_ID, messages=[TextMessage(text=msg)])
        )

def push_flex(flex_msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_ID, messages=[flex_msg])
        )

# ── Webhook ───────────────────────────────────────────
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

    if text in SPECIAL:
        s = get_special(text)
        if s: reply_flex(event, make_flex_card(s))
        else: reply_text(event, f"無法取得 {text} 資料")

    elif text == "報告":
        stocks = [get_price(sid) for sid in watchlist]
        stocks = [s for s in stocks if s]
        if stocks: reply_flex(event, make_report_flex(stocks))
        else: reply_text(event, "無法取得資料")

    elif text in ["自選股", "清單", "我的"]:
        if not watchlist:
            reply_text(event, "自選股是空的\n輸入「新增 2330」來加入")
        else:
            flex = make_watchlist_flex()
            if flex: reply_flex(event, flex)

    elif text.startswith("新增 "):
        sid = text.replace("新增 ", "").strip().upper()
        if sid in watchlist:
            reply_text(event, f"⚠️ {sid} 已在自選股中")
        else:
            s = get_price(sid)
            if s:
                watchlist.append(sid)
                reply_text(event, f"✅ 已新增 {sid} {s['name']} 到自選股\n現在共 {len(watchlist)} 檔")
            else:
                reply_text(event, f"找不到 {sid}，請確認代號正確")

    elif text.startswith("刪除 "):
        sid = text.replace("刪除 ", "").strip().upper()
        if sid in watchlist:
            watchlist.remove(sid)
            reply_text(event, f"🗑️ 已移除 {sid}\n現在共 {len(watchlist)} 檔")
        else:
            reply_text(event, f"⚠️ {sid} 不在自選股中")

    elif text == "網頁":
        reply_text(event, f"📈 台股追蹤網頁\n{NETLIFY_URL}")

    # K線圖：輸入「K 2330」或「圖 2330」
    elif text.startswith("K ") or text.startswith("圖 ") or text.startswith("k "):
        sid = text.split(" ", 1)[1].strip().upper()
        reply_text(event, f"📈 {sid} 圖表產生中，請稍候...")
        hist = fetch_history(sid)
        if hist:
            s = get_price(sid)
            name = s["name"] if s else sid
            chart_url = make_chart_url(sid, name, hist)
            reply_image(event, chart_url, chart_url)
        else:
            reply_text(event, f"無法取得 {sid} 歷史資料")

    elif len(text) >= 4 and len(text) <= 7 and text[0].isdigit() and text.replace("-","").isalnum():
        s = get_price(text)
        if s: reply_flex(event, make_flex_card(s))
        else: reply_text(event, f"找不到 {text}，請確認代號正確")

    elif text.startswith("警示 "):
        parts = text.split()
        if len(parts) == 3:
            sid, thresh = parts[1], float(parts[2])
            alerts[sid] = thresh
            reply_text(event, f"🔔 已設定 {sid} 漲跌超過 ±{thresh}% 通知")
        else:
            reply_text(event, "格式：警示 2330 5")

    elif text in ["指令", "help", "選單"]:
        reply_text(event,
            "📋 指令說明\n"
            "──────────\n"
            "【自選股】\n"
            "• 自選股 → 查看清單\n"
            "• 新增 2330 → 加入自選股\n"
            "• 刪除 2330 → 移除自選股\n"
            "• 報告 → 自選股即時行情\n\n"
            "【股票查詢】\n"
            "• 2330 → 查詢股價\n"
            "• K 2330 → 近30日K線圖\n\n"
            "【期貨/指數】\n"
            "• 台指期 / 加權 / 那斯達克\n"
            "• 道瓊 / 標普 / 費半\n"
            "• 黃金 / 原油 / 美元\n\n"
            "【其他】\n"
            "• 網頁 → 開啟追蹤網頁\n"
            "• 警示 2330 5 → 漲跌超過5%通知\n"
            "• 指令 → 顯示此說明"
        )

    else:
        reply_text(event,
            "輸入股票代號查詢，例如：2330\n"
            "輸入「K 2330」查看K線圖\n"
            "或輸入「指令」查看所有功能"
        )

def reply_text(event, msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=msg)])
        )

def reply_flex(event, flex_msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[flex_msg])
        )

def reply_image(event, image_url, preview_url):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[ImageMessage(
                                    original_content_url=image_url,
                                    preview_image_url=preview_url
                                )])
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
