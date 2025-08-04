import os
import requests
from flask import Flask, request
from pytrends.request import TrendReq
from threading import Thread
import time

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("Telegram error:", e)

def fetch_trending():
    while True:
        try:
            pytrends = TrendReq()
            trending = pytrends.trending_searches(pn="united_states")
            top = trending.head(10)[0].tolist()

            message = "🔥 أهم الترندات الآن:\n"
            for item in top:
                message += f"• {item}\n"

            send_message(message)
        except Exception as e:
            print("Trend error:", e)

        time.sleep(3600)  # كل ساعة

@app.route("/")
def home():
    return "Trending bot is alive ✅"

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if "message" not in data:
        return "ok"

    text = data["message"].get("text", "").lower()
    if "/start" in text:
        send_message("🤖 أهلاً بك! سأرسل أهم الترندات العالمية تلقائيًا كل ساعة.")
    return "ok"

# 🔁 بدء حلقة الترند عند التشغيل
Thread(target=fetch_trending, daemon=True).start()