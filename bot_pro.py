import os
import io
import re
import json
import time
import base64
import datetime
import logging
import asyncio
import requests
from dotenv import load_dotenv

from flask import Flask, request
from telegram import Update, InputFile, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import firebase_admin
from firebase_admin import credentials, db

# ------------------ Load environment ------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BOT_SECRET = os.getenv("BOT_SECRET", "super_secret_string")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDS_JSON")
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = os.getenv("SEARCH_ENGINE_ID")
ADMIN_USER_IDS = set([s.strip() for s in (os.getenv("ADMIN_USER_IDS") or "").split(",") if s.strip()])
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5"))
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "10"))
DEFAULT_MONTHLY_CAP = int(os.getenv("MONTHLY_GLOBAL_CAP", "100"))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ------------------ Initialize Firebase ------------------
FIREBASE_READY = False
try:
    if not firebase_admin._apps:
        if FIREBASE_CREDS_JSON and FIREBASE_DB_URL:
            cred_dict = json.loads(FIREBASE_CREDS_JSON)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            FIREBASE_READY = True
            logger.info("Firebase initialized from ENV variable")
        else:
            logger.warning("Firebase credentials or DB URL missing. Firebase features disabled.")
except Exception as e:
    logger.exception("Firebase init failed: %s", e)
    FIREBASE_READY = False

# ------------------ Async helpers ------------------
async def _async_post(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.post(url, **kwargs))

async def _async_get(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.get(url, **kwargs))

async def _async_put(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.put(url, **kwargs))

# ------------------ Safe math ------------------
import numexpr

def safe_math(expr: str):
    if not isinstance(expr, str): return None
    if not re.match(r'^[0-9+\-*/().\s]+$', expr): return None
    try:
        val = numexpr.evaluate(expr)
        try:
            return val.item()
        except Exception:
            return float(val)
    except Exception:
        return None

# ------------------ Firebase helpers ------------------
def _today_key():
    return datetime.date.today().isoformat()

def _month_key():
    d = datetime.date.today()
    return f"{d.year}-{d.month:02d}"

async def get_usage(user_id: str):
    if not FIREBASE_READY:
        return {"count": 0, "last_ts": 0.0}
    url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}.json"
    resp = await _async_get(url)
    if resp.status_code == 200 and resp.json():
        data = resp.json()
        return {"count": int(data.get("count", 0)), "last_ts": float(data.get("last_ts", 0.0))}
    return {"count": 0, "last_ts": 0.0}

async def set_usage(user_id: str, count: int, last_ts: float):
    if not FIREBASE_READY: return
    url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}.json"
    await _async_put(url, json={"count": count, "last_ts": last_ts})

async def increment_usage(user_id: str):
    if not FIREBASE_READY: return
    day_count_url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}/count.json"
    resp = await _async_get(day_count_url)
    cur = int(resp.json()) if resp.status_code == 200 and resp.json() is not None else 0
    await _async_put(day_count_url, json=cur+1)

    ts_url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}/last_ts.json"
    await _async_put(ts_url, json=time.time())

    month_key = _month_key()
    month_url = f"{FIREBASE_DB_URL}/usage_images/{month_key}/total_count.json"
    resp2 = await _async_get(month_url)
    curm = int(resp2.json()) if resp2.status_code == 200 and resp2.json() is not None else 0
    await _async_put(month_url, json=curm+1)

async def get_daily_limit(user_id: str):
    if not FIREBASE_READY: return DEFAULT_DAILY_LIMIT
    url = f"{FIREBASE_DB_URL}/limits/{user_id}/daily.json"
    resp = await _async_get(url)
    if resp.status_code == 200 and resp.json() is not None:
        return int(resp.json())
    return DEFAULT_DAILY_LIMIT

async def get_monthly_total():
    if not FIREBASE_READY: return 0
    url = f"{FIREBASE_DB_URL}/usage_images/{_month_key()}/total_count.json"
    resp = await _async_get(url)
    if resp.status_code == 200 and resp.json() is not None:
        return int(resp.json())
    return 0

# ------------------ Image args parser ------------------
def parse_image_args(args_list):
    text = " ".join(args_list)
    size = None
    seed = None
    negative = None
    m = re.search(r"--size\s+(512|768|1024)", text)
    if m:
        size = m.group(1)
        text = re.sub(r"--size\s+(512|768|1024)", "", text)
    m = re.search(r"--seed\s+(\d+)", text)
    if m:
        seed = int(m.group(1))
        text = re.sub(r"--seed\s+\d+", "", text)
    m = re.search(r"--no\s+([^\n]+)", text)
    if m:
        negative = m.group(1).strip()
        text = re.sub(r"--no\s+[^\n]+", "", text)
    return text.strip(), size, seed, negative

# ------------------ Vertex AI image generation ------------------
SIZE_MAP = {"512":"512x512","768":"768x768","1024":"1024x1024"}

async def vertex_generate_image(prompt: str, size: str=None, seed: int=None, negative: str=None):
    if not (VERTEX_PROJECT_ID and GEMINI_API_KEY and VERTEX_LOCATION):
        logger.error("Vertex config missing")
        return None
    url = f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{VERTEX_PROJECT_ID}/locations/{VERTEX_LOCATION}/publishers/google/models/imagegeneration:predict?key={GEMINI_API_KEY}"
    parameters = {"sampleCount":1,"imageSize":SIZE_MAP.get(size or "1024","1024x1024")}
    if seed: parameters["seed"]=seed
    final_prompt = prompt
    if negative:
        parameters["negativePrompt"] = negative
        final_prompt = f"{prompt}. Avoid: {negative}"
    payload = {"instances":[{"prompt":final_prompt}],"parameters":parameters}
    headers = {"Content-Type":"application/json"}
    try:
        resp = await _async_post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        enc = None
        if isinstance(data.get("predictions"), list) and data["predictions"]:
            pred = data["predictions"][0]
            enc = pred.get("bytesBase64Encoded") or pred.get("b64") or pred.get("imageBytes")
        if not enc:
            logger.error("No image base64 in response")
            return None
        return base64.b64decode(enc)
    except Exception as e:
        logger.exception("Vertex image gen failed: %s", e)
        return None

# ------------------ Cooldown check ------------------
async def check_and_update_cooldown(user_id: str, min_gap: int = COOLDOWN_SECONDS):
    usage = await get_usage(user_id)
    now = time.time()
    last = usage.get("last_ts",0.0)
    if now - last < min_gap: return False
    await set_usage(user_id, usage.get("count",0), now)
    return True

# ------------------ Telegram command handlers ------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "✨ *Surfer Bot — Help* ✨\n\n" \
          "• /ask `<question>` — Ask AI.\n" \
          "• /search `<query>` — Safe math or Google.\n" \
          "• /image `<prompt>` [--size 512|768|1024 --seed <n> --no <neg>]\n" \
          "• /quota — your daily usage.\n" \
          "• /resetquota `<user_id>` — admin only.\n" \
          "• /setlimit `<user_id> <n>` — admin only.\n" \
          "• /resetmonth — admin only.\n" \
          "• /checkquota — admin or anyone.\n" \
          "• /stats — admin only."
    await update.message.reply_markdown(txt)

# --- Other handlers (ask/search/image/quota/etc) can follow the same cleaned pattern ---

# ------------------ Flask + webhook ------------------
app = Flask(__name__)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

@app.route(f"/{BOT_SECRET}", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json(force=True)
        if not update_data: return "no data", 400
        async def main_async():
            await application.process_update(Update.de_json(update_data, application.bot))
        asyncio.run(main_async())
        return "ok"
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return "error", 500

@app.get("/")
def health(): return "ok"

# Post init to set Telegram commands
async def post_init(apply): 
    await apply.bot.set_my_commands([
        BotCommand("help","Show help"),
        BotCommand("ask","Ask AI"),
        BotCommand("search","Safe math or Google"),
        BotCommand("image","Generate AI image"),
        BotCommand("quota","Show usage")
    ])
application.post_init = post_init
