import os
import datetime
import json
import requests
import yfinance as yf

# GitHubのSecretsから安全に取得
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
USER_ID = os.environ.get("USER_ID")

WATCH_STOCKS = {
    "7513.T": ["コジマ", "買い物券3,000円分/年"],
    "4668.T": ["明光ネットワーク", "QUOカード500円分"],
    "3387.T": ["クリエイト・レストランツ", "食事券4,000円分/年"],
    "2687.T": ["シー・ヴイ・エス", "自社ホテル割引券"],
    "7085.T": ["カーブスHD", "QUOカード500円分"],
    "3048.T": ["ビックカメラ", "買い物券3,000円分/年"],
}

def fetch_stock_data():
    selected_stocks = []
    for code, info in WATCH_STOCKS.items():
        ticker = yf.Ticker(code)
        fast_info = ticker.fast_info
        info_dict = ticker.info

        current_price = fast_info.last_price
        min_cost = current_price * 100
        dividend_yield = info_dict.get("dividendYield", 0)
        dividend_yield_pct = (dividend_yield * 100) if dividend_yield else 0.0

        selected_stocks.append({
            "code": code.replace(".T", ""),
            "name": info[0],
            "price_fmt": f"約{int(min_cost / 10000)}.{int((min_cost % 10000) / 1000)}万円 ({int(current_price):,}円)",
            "yield_fmt": f"配当 {dividend_yield_pct:.1f}% + 優待",
            "detail": info[1]
        })
        if len(selected_stocks) >= 3:
            break
    return selected_stocks

def send_line_flex_message(stocks):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }

    weekday_str = ["月", "火", "水", "木", "金", "土", "日"][datetime.datetime.now().weekday()]
    
    bubbles = []
    for stock in stocks:
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"{stock['name']} ({stock['code']})", "weight": "bold", "size": "xl"},
                    {
                        "type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": f"最低買付額: {stock['price_fmt']}", "size": "sm", "color": "#555555"},
                            {"type": "text", "text": f"利回り: {stock['yield_fmt']}", "size": "sm", "color": "#1DB446", "weight": "bold"},
                            {"type": "text", "text": f"優待: {stock['detail']}", "size": "xs", "color": "#666666", "wrap": True}
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "uri", "label": "チャートを見る", "uri": f"https://finance.yahoo.co.jp/quote/{stock['code']}.T"}, "style": "link"}
                ]
            }
        }
        bubbles.append(bubble)

    payload = {
        "to": USER_ID,
        "messages": [{
            "type": "flex",
            "altText": f"【{weekday_str}曜更新】本日のおすすめ株3選",
            "contents": {"type": "carousel", "contents": bubbles}
        }]
    }
    requests.post(url, headers=headers, data=json.dumps(payload))

if __name__ == "__main__":
    send_line_flex_message(fetch_stock_data())
