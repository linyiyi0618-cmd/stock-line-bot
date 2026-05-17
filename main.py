import os, time, requests, json
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import quote
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer, ImageMessage
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

SEP = "─" * 20

# ── 精選台股池（流動性佳，適合單沖）────────────────────
STOCK_POOL = [
    # 半導體
    "2330","2454","2379","2303","2308","2317","2357","3711","2344","3034",
    "2337","2351","2449","3533","6770","3661","2385","2376","2388","6415",
    # 電子
    "2382","2395","2392","2353","2356","2360","2365","2367","2369","2371",
    "2372","2373","2374","2375","2377","2383","2387","2390","2393","2399",
    # 金融
    "2882","2881","2880","2886","2884","2885","2887","2888","2889","2891",
    "2892","5880","2883","2890","2801","2809","2812","2820","5876","6005",
    # 傳產/食品
    "1301","1303","1326","1402","1216","1101","1102","1301","2002","2105",
    "1314","1319","1321","1323","1325","1326","1402","1434","1440","1504",
    # ETF
    "0050","0056","006208","00878","00919","00929","00713","00720B","00687B","0052",
    # 生技醫療
    "4763","4726","1787","4174","6547","4174","6202","4968","6456","4743",
    # 航運
    "2603","2609","2615","2618","2609","2610","2606","2607","2608","2616",
    # 其他熱門
    "2912","2801","9904","9910","1590","2207","2204","2201","2105","2103",
    "5871","5876","5880","6116","6121","6122","6125","6126","6127","6128",
    "3008","3016","3019","3021","3022","3024","3025","3026","3027","3028",
    "2408","2409","2412","2448","2456","2458","2459","2460","2461","2462",
]
# 去重
STOCK_POOL = list(dict.fromkeys(STOCK_POOL))

# ── Yahoo Finance ─────────────────────────────────────
def fetch_yahoo(symbol):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?interval=1d&range=5d"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10, verify=False)
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
    for suffix in [".TW", ".TWO"]:
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/" + stock_id + suffix + "?interval=1d&range=60d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=10, verify=False)
            data = r.json()
            result = data["chart"]["result"]
            if not result: continue
            q = result[0]["indicators"]["quote"][0]
            timestamps = result[0]["timestamp"]
            closes  = q.get("close", [])
            volumes = q.get("volume", [])
            opens   = q.get("open", [])
            highs   = q.get("high", [])
            rows = []
            for ts, c, v, o, h in zip(timestamps, closes, volumes, opens, highs):
                if None in [c, v, o, h]: continue
                rows.append({
                    "date":   datetime.fromtimestamp(ts).strftime("%m/%d"),
                    "close":  round(c, 2),
                    "volume": int(v),
                    "open":   round(o, 2),
                    "high":   round(h, 2)
                })
            if rows:
                return rows
        except Exception as e:
            print("[history] " + stock_id + suffix + ": " + str(e))
    return None

def get_price(stock_id):
    for suffix, market in [(".TW", "上市"), (".TWO", "上櫃")]:
        try:
            result = fetch_yahoo(stock_id + suffix)
            if result:
                result["id"] = stock_id
                result["market"] = market
                return result
        except Exception as e:
            print("[price] " + stock_id + ": " + str(e))
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
        print("[special] " + keyword + ": " + str(e))
    return None

def is_trading():
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 60 + now.minute
    return 9*60 <= hm < 13*60+30

# ── 隔日選股引擎 ─────────────────────────────────────
def screen_stocks(ids):
    results = {"爆量": [], "漲幅領先": [], "突破均線": [], "高檔低收": []}
    for sid in ids:
        rows = fetch_history(sid)
        if not rows or len(rows) < 21: continue
        closes  = [r["close"]  for r in rows]
        volumes = [r["volume"] for r in rows]
        highs   = [r["high"]   for r in rows]
        last = rows[-1]
        prev_row = rows[-2] if len(rows) >= 2 else None
        pct = round((last["close"] - prev_row["close"]) / prev_row["close"] * 100, 2) if prev_row else 0
        # 從 Yahoo Finance 直接取名稱，不依賴即時 API
        name = sid
        try:
            for suffix in [".TW", ".TWO"]:
                r2 = requests.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/" + sid + suffix + "?interval=1d&range=5d",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=8, verify=False
                )
                meta2 = r2.json()["chart"]["result"][0]["meta"]
                n2 = meta2.get("longName") or meta2.get("shortName", "")
                if n2:
                    name = n2
                    break
        except:
            pass
        entry = {"id": sid, "name": name, "close": last["close"], "pct": pct, "date": last["date"]}

        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
        if avg_vol > 0 and last["volume"] >= avg_vol * 3:
            e = dict(entry); e["vol_ratio"] = round(last["volume"] / avg_vol, 1)
            results["爆量"].append(e)

        if abs(pct) >= 3:
            results["漲幅領先"].append(dict(entry))

        if len(closes) >= 20:
            ma5  = round(sum(closes[-6:-1]) / 5, 2)
            ma20 = round(sum(closes[-21:-1]) / 20, 2)
            pc   = closes[-2] if len(closes) >= 2 else closes[-1]
            if pc < ma5 and last["close"] >= ma5:
                e = dict(entry); e["broke"] = "MA5"; results["突破均線"].append(e)
            elif pc < ma20 and last["close"] >= ma20:
                e = dict(entry); e["broke"] = "MA20"; results["突破均線"].append(e)

        if pct > 1:
            tail = round((last["high"] - last["close"]) / last["close"] * 100, 2)
            if tail < 1.0:
                e = dict(entry); e["tail"] = tail; results["高檔低收"].append(e)

    return results

# ── 全市場漲跌幅排行 ──────────────────────────────────
def get_market_movers(want_top=True, n=5):
    try:
        r = requests.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_&_=" + str(int(time.time())),
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15, verify=False
        )
        items = r.json().get("msgArray", [])
        stocks = []
        for item in items:
            try:
                z = item.get("z", "-")
                y = item.get("y", "-")
                if z in ["-", "", None] or y in ["-", "", None]: continue
                price = float(z); prev = float(y)
                if prev <= 0: continue
                chg = round(price - prev, 2)
                pct = round((chg / prev) * 100, 2)
                stocks.append({"id": item.get("c",""), "name": item.get("n",""),
                                "price": price, "change": chg, "pct": pct})
            except: continue
        if not stocks: return None
        stocks.sort(key=lambda x: x["pct"], reverse=want_top)
        return stocks[:n]
    except Exception as e:
        print("[movers] " + str(e))
        return None

# ── K線圖（QuickChart 短網址）────────────────────────
def make_chart_url(stock_id, name, hist):
    dates  = hist["dates"] if "dates" in hist else [r["date"] for r in hist]
    closes = hist["closes"] if "closes" in hist else [r["close"] for r in hist]
    up     = closes[-1] >= closes[0]
    color  = "rgb(46,204,113)" if up else "rgb(231,76,60)"
    fill   = "rgba(46,204,113,0.15)" if up else "rgba(231,76,60,0.15)"
    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [{"label": stock_id + " " + name, "data": closes,
                          "borderColor": color, "backgroundColor": fill,
                          "borderWidth": 2, "pointRadius": 0, "fill": True, "tension": 0.3}]
        },
        "options": {
            "plugins": {
                "legend": {"display": True, "labels": {"color": "#FFFFFF", "font": {"size": 14}}},
                "title": {"display": True, "text": stock_id + " " + name + "  近30日走勢",
                          "color": "#FFFFFF", "font": {"size": 16}}
            },
            "scales": {
                "x": {"ticks": {"color": "#AAAAAA", "maxTicksLimit": 8},
                      "grid": {"color": "rgba(255,255,255,0.08)"}},
                "y": {"ticks": {"color": "#AAAAAA"},
                      "grid": {"color": "rgba(255,255,255,0.08)"}}
            },
            "backgroundColor": "#1A1A2E"
        }
    }
    try:
        resp = requests.post(
            "https://quickchart.io/chart/create",
            json={"chart": chart_config, "width": 800, "height": 400, "backgroundColor": "#1A1A2E"},
            timeout=15, verify=False
        )
        data = resp.json()
        if data.get("success"):
            return data["url"]
    except Exception as e:
        print("[chart] " + str(e))
    return None

# ── Flex Message 卡片 ─────────────────────────────────
def make_flex_card(s):
    up    = s["pct"] >= 0
    color = "#27AE60" if up else "#E74C3C"
    bg    = "#F0FFF4" if up else "#FFF5F5"
    arrow = "▲" if up else "▼"
    sign  = "+" if up else ""
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": color, "paddingAll": "14px",
                   "contents": [
                       {"type": "box", "layout": "horizontal", "contents": [
                           {"type": "text", "text": str(s["id"]), "color": "#FFFFFF", "size": "sm", "weight": "bold", "flex": 1},
                           {"type": "text", "text": s.get("market",""), "color": "#FFFFFF99", "size": "xs", "align": "end"}
                       ]},
                       {"type": "text", "text": s["name"], "color": "#FFFFFF", "size": "md", "weight": "bold", "margin": "sm", "wrap": True}
                   ]},
        "body": {"type": "box", "layout": "vertical", "backgroundColor": bg, "paddingAll": "14px",
                 "contents": [
                     {"type": "box", "layout": "horizontal", "contents": [
                         {"type": "text", "text": "現價", "color": "#888888", "size": "sm", "flex": 1},
                         {"type": "text", "text": "{:,.2f}".format(s["price"]), "color": color, "size": "xl", "weight": "bold", "align": "end"}
                     ]},
                     {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                         {"type": "text", "text": "漲跌", "color": "#888888", "size": "sm", "flex": 1},
                         {"type": "text", "text": arrow + " " + sign + "{:.2f}".format(s["change"]) + "  (" + sign + "{:.2f}".format(s["pct"]) + "%)",
                          "color": color, "size": "sm", "weight": "bold", "align": "end"}
                     ]},
                     {"type": "separator", "margin": "md", "color": "#DDDDDD"},
                     {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                         {"type": "text", "text": "資料時間", "color": "#AAAAAA", "size": "xs", "flex": 1},
                         {"type": "text", "text": s.get("source","—"), "color": "#AAAAAA", "size": "xs", "align": "end"}
                     ]}
                 ]}
    }
    return FlexMessage(alt_text=s["id"] + " " + s["name"] + " " + str(s["price"]),
                       contents=FlexContainer.from_dict(bubble))

def make_report_flex(stocks):
    rows = []
    for s in stocks:
        up    = s["pct"] >= 0
        color = "#27AE60" if up else "#E74C3C"
        arrow = "▲" if up else "▼"
        sign  = "+" if up else ""
        rows.append({"type": "box", "layout": "horizontal", "paddingAll": "10px",
                     "contents": [
                         {"type": "box", "layout": "vertical", "flex": 2, "contents": [
                             {"type": "text", "text": s["id"], "size": "xs", "color": "#888888"},
                             {"type": "text", "text": s["name"], "size": "sm", "weight": "bold", "wrap": True}
                         ]},
                         {"type": "box", "layout": "vertical", "flex": 2, "contents": [
                             {"type": "text", "text": "{:,.2f}".format(s["price"]), "size": "md", "weight": "bold", "color": color, "align": "end"},
                             {"type": "text", "text": arrow + " " + sign + "{:.2f}".format(s["pct"]) + "%", "size": "xs", "color": color, "align": "end"}
                         ]}
                     ]})
        rows.append({"type": "separator", "color": "#EEEEEE"})
    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A1A2E", "paddingAll": "14px",
                   "contents": [
                       {"type": "text", "text": "📊 自選股行情", "color": "#FFFFFF", "size": "md", "weight": "bold"},
                       {"type": "text", "text": datetime.now().strftime("%m/%d %H:%M"), "color": "#FFFFFF88", "size": "xs", "margin": "sm"}
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": rows[:-1]}
    }
    return FlexMessage(alt_text="自選股行情報告", contents=FlexContainer.from_dict(bubble))

def make_watchlist_flex():
    if not watchlist: return None
    rows = []
    for sid in watchlist:
        rows.append({"type": "box", "layout": "horizontal", "paddingAll": "10px",
                     "contents": [
                         {"type": "text", "text": sid, "size": "sm", "weight": "bold", "flex": 1, "gravity": "center"},
                         {"type": "button", "action": {"type": "message", "label": "刪除", "text": "刪除 " + sid},
                          "style": "secondary", "height": "sm", "flex": 1, "color": "#FF6B6B"}
                     ]})
        rows.append({"type": "separator", "color": "#EEEEEE"})
    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A1A2E", "paddingAll": "14px",
                   "contents": [
                       {"type": "text", "text": "⭐ 自選股清單", "color": "#FFFFFF", "size": "md", "weight": "bold"},
                       {"type": "text", "text": "共 " + str(len(watchlist)) + " 檔", "color": "#FFFFFF88", "size": "xs", "margin": "sm"}
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": rows[:-1]},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "12px",
                   "contents": [{"type": "text", "text": "輸入「新增 2330」加入自選股", "color": "#AAAAAA", "size": "xs", "align": "center"}]}
    }
    return FlexMessage(alt_text="自選股清單（" + str(len(watchlist)) + " 檔）",
                       contents=FlexContainer.from_dict(bubble))

# ── 警示 & 排程 ───────────────────────────────────────
triggered_today = set()

def check_alerts():
    if not is_trading(): return
    today = str(date.today())
    for sid, thresh in list(alerts.items()):
        key = today + "_" + sid
        if key in triggered_today: continue
        s = get_price(sid)
        if not s: continue
        if abs(s["pct"]) >= thresh:
            triggered_today.add(key)
            arrow = "🔺" if s["pct"] > 0 else "🔻"
            push_text("🔔 股價警示！\n" + s["id"] + " " + s["name"] + "\n" +
                      arrow + " " + str(abs(s["pct"])) + "%　現價 " + str(s["price"]) +
                      "\n門檻：±" + str(thresh) + "%")

def daily_report():
    stocks = [get_price(sid) for sid in watchlist]
    stocks = [s for s in stocks if s]
    if stocks: push_flex(make_report_flex(stocks))

def push_text(msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_ID, messages=[TextMessage(text=msg)]))

def push_flex(flex_msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_ID, messages=[flex_msg]))

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
        else: reply_text(event, "無法取得 " + text + " 資料")

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
            reply_text(event, "⚠️ " + sid + " 已在自選股中")
        else:
            s = get_price(sid)
            if s:
                watchlist.append(sid)
                reply_text(event, "✅ 已新增 " + sid + " " + s["name"] + " 到自選股\n現在共 " + str(len(watchlist)) + " 檔")
            else:
                reply_text(event, "找不到 " + sid + "，請確認代號正確")

    elif text.startswith("刪除 "):
        sid = text.replace("刪除 ", "").strip().upper()
        if sid in watchlist:
            watchlist.remove(sid)
            reply_text(event, "🗑️ 已移除 " + sid + "\n現在共 " + str(len(watchlist)) + " 檔")
        else:
            reply_text(event, "⚠️ " + sid + " 不在自選股中")

    elif text.startswith("昨日 ") or text.startswith("yesterday "):
        sid = text.split(" ", 1)[1].strip().upper()
        rows = fetch_history(sid)
        if rows and len(rows) >= 2:
            s_now = get_price(sid)
            name = s_now["name"] if s_now else sid
            last  = rows[-1]
            prev  = rows[-2]
            chg   = round(last["close"] - prev["close"], 2)
            pct   = round((chg / prev["close"]) * 100, 2) if prev["close"] else 0
            arrow = "▲" if pct > 0 else "▼"
            sign  = "+" if pct > 0 else ""
            avg_vol = round(sum([r["volume"] for r in rows[-21:-1]]) / 20) if len(rows) >= 21 else 0
            vol_ratio = round(last["volume"] / avg_vol, 1) if avg_vol > 0 else 0
            parts = [
                "📅 " + sid + " " + name + " 昨日行情",
                SEP,
                "日期：" + last["date"],
                "收盤：" + str(last["close"]),
                "漲跌：" + arrow + " " + sign + str(chg) + " (" + sign + str(pct) + "%)",
                "開盤：" + str(last["open"]),
                "最高：" + str(last["high"]),
                SEP,
                "成交量：" + "{:,}".format(last["volume"]) + " 張",
                "均量(20日)：" + "{:,}".format(avg_vol) + " 張",
                "量比：" + str(vol_ratio) + "x",
                SEP,
                "前日收盤：" + str(prev["close"])
            ]
            reply_text(event, "\n".join(parts))
        else:
            reply_text(event, "找不到 " + sid + " 歷史資料")

    elif text.startswith("昨日 "):
        sid = text.split(" ", 1)[1].strip().upper()
        rows = fetch_history(sid)
        if rows and len(rows) >= 2:
            s_now = get_price(sid)
            name = s_now["name"] if s_now else sid
            last  = rows[-1]
            prev  = rows[-2]
            chg   = round(last["close"] - prev["close"], 2)
            pct   = round((chg / prev["close"]) * 100, 2) if prev["close"] else 0
            arrow = "▲" if pct > 0 else "▼"
            sign  = "+" if pct > 0 else ""
            avg_vol = round(sum([r["volume"] for r in rows[-21:-1]]) / 20) if len(rows) >= 21 else 0
            vol_ratio = round(last["volume"] / avg_vol, 1) if avg_vol > 0 else 0
            parts = [
                "📅 " + sid + " " + name + " 昨日行情",
                SEP,
                "日期：" + last["date"],
                "收盤：" + str(last["close"]),
                "漲跌：" + arrow + " " + sign + str(chg) + " (" + sign + str(pct) + "%)",
                "開盤：" + str(last["open"]),
                "最高：" + str(last["high"]),
                SEP,
                "成交量：" + "{:,}".format(last["volume"]) + " 張",
                "均量(20日)：" + "{:,}".format(avg_vol) + " 張",
                "量比：" + str(vol_ratio) + "x",
                SEP,
                "前日收盤：" + str(prev["close"])
            ]
            reply_text(event, "\n".join(parts))
        else:
            reply_text(event, "找不到 " + sid + " 歷史資料")

    elif text in ["昨日", "昨天"]:
        if not watchlist:
            reply_text(event, "自選股是空的，請先新增股票")
        else:
            lines_out = ["📅 自選股昨日收盤", SEP]
            last_date = ""
            for sid in watchlist:
                rows = fetch_history(sid)
                if not rows or len(rows) < 2: continue
                last = rows[-1]; prev = rows[-2]
                last_date = last["date"]
                chg = round(last["close"] - prev["close"], 2)
                pct = round((chg / prev["close"]) * 100, 2) if prev["close"] else 0
                arrow = "▲" if pct > 0 else "▼"
                sign  = "+" if pct > 0 else ""
                nd = get_price(sid)
                name = nd["name"] if nd else sid
                lines_out.append(arrow + " " + sid + " " + name)
                lines_out.append("   " + str(last["close"]) + "　" + sign + str(chg) + " (" + sign + str(pct) + "%)")
            lines_out.extend([SEP, "資料日期：" + last_date])
            reply_text(event, "\n".join(lines_out))

    elif text == "網頁":
        reply_text(event, "📈 台股追蹤網頁\n" + NETLIFY_URL)

    elif text.startswith("K ") or text.startswith("圖 ") or text.startswith("k "):
        sid = text.split(" ", 1)[1].strip().upper()
        rows = fetch_history(sid)
        if rows:
            s = get_price(sid)
            name = s["name"] if s else sid
            hist = {"dates": [r["date"] for r in rows[-30:]], "closes": [r["close"] for r in rows[-30:]]}
            chart_url = make_chart_url(sid, name, hist)
            if chart_url: reply_image(event, chart_url, chart_url)
            else: reply_text(event, "圖表產生失敗，請稍後再試")
        else:
            reply_text(event, "無法取得 " + sid + " 歷史資料")

    elif text.startswith("算 "):
        parts = text.split()
        if len(parts) == 4:
            try:
                buy  = float(parts[1])
                sell = float(parts[2])
                qty  = int(parts[3])
                fee_rate   = 0.001425 * 0.6
                tax_rate   = 0.003
                buy_fee    = round(buy  * qty * fee_rate, 0)
                sell_fee   = round(sell * qty * fee_rate, 0)
                sell_tax   = round(sell * qty * tax_rate, 0)
                gross      = round((sell - buy) * qty, 0)
                total_cost = buy_fee + sell_fee + sell_tax
                net        = gross - total_cost
                roi        = round((net / (buy * qty)) * 100, 2)
                arrow = "🟢 獲利" if net >= 0 else "🔴 虧損"
                msg = ("📊 單沖損益試算\n" + SEP + "\n"
                       + "買價：" + str(buy) + "　賣價：" + str(sell) + "　股數：" + "{:,}".format(qty) + "\n"
                       + SEP + "\n"
                       + "毛利：" + "{:+,.0f}".format(gross) + " 元\n"
                       + "買手續費：-" + "{:,.0f}".format(buy_fee) + " 元\n"
                       + "賣手續費：-" + "{:,.0f}".format(sell_fee) + " 元\n"
                       + "證交稅：-" + "{:,.0f}".format(sell_tax) + " 元\n"
                       + SEP + "\n"
                       + arrow + "：" + "{:+,.0f}".format(net) + " 元\n"
                       + "報酬率：" + "{:+.2f}".format(roi) + "%\n"
                       + SEP + "\n"
                       + "※ 手續費以六折計算")
                reply_text(event, msg)
            except:
                reply_text(event, "格式錯誤\n範例：算 100 105 3000\n（買價 賣價 股數）")
        else:
            reply_text(event, "格式：算 買價 賣價 股數\n範例：算 100 105 3000")

    elif text in ["選股", "明日選股", "隔日選股", "明天買什麼", "推薦"]:
        # 先回覆「分析中」，避免 reply token 過期
        user_id = event.source.user_id
        reply_text(event, "🔍 掃描全市場中\n約需30秒，結果將自動推播給你...")
        # 背景執行選股，完成後 push 結果
        def do_screen():
            try:
                stocks = screen_stocks(STOCK_POOL)
                last_trading = datetime.now().strftime("%m/%d")

                def stars(score):
                    if score >= 6: return "★★★ 強烈關注"
                    if score >= 4: return "★★☆ 值得關注"
                    return "★☆☆ 留意觀察"

                lines = [
                    "📋 明日潛力選股",
                    last_trading + " 分析  共 " + str(len(stocks)) + " 檔入選",
                    SEP
                ]
                if not stocks:
                    lines.append("本次無符合條件股票")
                    lines.append("可能為假日或市場偏弱")
                else:
                    for i, s in enumerate(stocks[:10], 1):
                        arrow = "▲" if s["pct"] > 0 else "▼"
                        sign  = "+" if s["pct"] > 0 else ""
                        lines.append("")
                        lines.append(str(i) + ". " + s["id"] + " " + s["name"])
                        lines.append("   " + arrow + sign + "{:.2f}".format(s["pct"]) + "%  收" + str(s["close"]))
                        lines.append("   " + stars(s["score"]))
                        lines.append("   " + " ".join(s["tags"]))
                lines.extend(["", SEP, "⚠️ 僅供參考，請自行判斷"])
                msg = "\n".join(lines)
            except Exception as e:
                msg = "❌ 選股發生錯誤：" + str(e)
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=msg)])
                )
        threading.Thread(target=do_screen, daemon=True).start()

    elif text in ["強勢", "弱勢", "強", "弱", "漲停", "跌停"]:
        want_strong = text in ["強勢", "強", "漲停"]
        label = "🔺 全市場漲幅前5名" if want_strong else "🔻 全市場跌幅前5名"
        result = get_market_movers(want_strong)
        if result:
            lines = [label, SEP]
            for i, s in enumerate(result, 1):
                arrow = "▲" if s["pct"] > 0 else "▼"
                lines.append(str(i) + ". " + s["id"] + " " + s["name"])
                lines.append("   現價 " + str(s["price"]) + "　" + arrow + " " + "{:+.2f}".format(s["pct"]) + "%")
            lines.append(SEP)
            lines.append("更新：" + datetime.now().strftime("%H:%M"))
            reply_text(event, "\n".join(lines))
        else:
            reply_text(event, "無法取得市場資料\n強弱勢排行需在盤中查詢（09:00-13:30）")

    elif len(text) >= 4 and len(text) <= 7 and text[0].isdigit() and text.replace("-","").isalnum():
        s = get_price(text)
        if s: reply_flex(event, make_flex_card(s))
        else: reply_text(event, "找不到 " + text + "，請確認代號正確")

    elif text.startswith("警示 "):
        parts = text.split()
        if len(parts) == 3:
            sid, thresh = parts[1], float(parts[2])
            alerts[sid] = thresh
            reply_text(event, "🔔 已設定 " + sid + " 漲跌超過 ±" + str(thresh) + "% 通知")
        else:
            reply_text(event, "格式：警示 2330 5")

    elif text in ["指令", "help", "選單"]:
        reply_text(event,
            "📋 指令說明\n" + SEP + "\n"
            "【自選股】\n"
            "• 自選股 → 查看清單\n"
            "• 新增 2330 → 加入自選股\n"
            "• 刪除 2330 → 移除自選股\n"
            "• 報告 → 自選股即時行情\n\n"
            "【股票查詢】\n"
            "• 2330 → 查詢股價\n"
            "• K 2330 → 近30日K線圖\n\n"
            "【選股工具】\n"
            "• 選股 → 隔日四策略選股\n"
            "• 強勢 / 弱勢 → 全市場排行\n\n"
            "【單沖工具】\n"
            "• 算 100 105 3000 → 損益計算\n\n"
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
        reply_text(event, "輸入股票代號查詢，例如：2330\n或輸入「指令」查看所有功能")

def reply_text(event, msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=msg)]))

def reply_flex(event, flex_msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[flex_msg]))

def reply_image(event, image_url, preview_url):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[ImageMessage(
                                    original_content_url=image_url,
                                    preview_image_url=preview_url)]))

# ── 排程 ──────────────────────────────────────────────
def auto_screen():
    """每日收盤後自動推播選股結果"""
    def stars(score):
        if score >= 6: return "★★★ 強烈關注"
        if score >= 4: return "★★☆ 值得關注"
        return "★☆☆ 留意觀察"

    stocks = screen_stocks(STOCK_POOL)
    lines = [
        "📋 每日自動選股",
        datetime.now().strftime("%m/%d") + " 收盤後推播",
        "共 " + str(len(stocks)) + " 檔入選",
        SEP
    ]
    if not stocks:
        lines.extend(["今日無符合條件股票", "市場偏弱，注意風險"])
    else:
        for i, s in enumerate(stocks[:10], 1):
            arrow = "▲" if s["pct"] > 0 else "▼"
            sign  = "+" if s["pct"] > 0 else ""
            lines.append("")
            lines.append(str(i) + ". " + s["id"] + " " + s["name"])
            lines.append("   " + arrow + sign + "{:.2f}".format(s["pct"]) + "%  收" + str(s["close"]))
            lines.append("   " + stars(s["score"]))
            lines.append("   " + " ".join(s["tags"]))
    lines.extend(["", SEP, "⚠️ 僅供參考，請自行判斷"])
    push_text("\n".join(lines))

def run_schedule():
    schedule.every(15).seconds.do(check_alerts)
    schedule.every().day.at("13:35").do(daily_report)
    schedule.every().day.at("14:00").do(auto_screen)  # 收盤後自動選股推播
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=run_schedule, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
