print("Bot ne shuru kar diya!")
# bot_pro.py — Final deploy-ready (friendly messages, quota, admin, help)
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
import numexpr
from dotenv import load_dotenv

from flask import Flask, request as flask_request
from telegram import Update, InputFile, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# Firebase admin SDK
import firebase_admin
from firebase_admin import credentials, db

# ------------- Load environment -------------
load_dotenv()

# Required
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BOT_SECRET = os.getenv("BOT_SECRET", "a_super_secret_string")

# Optional / recommended
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")  # e.g. https://project-id.firebaseio.com
FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDS_JSON")  # service account JSON as one-line string (preferred for Vercel)
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # used for Generative Language / Vertex REST key
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = os.getenv("SEARCH_ENGINE_ID")

# Admins and caps
ADMIN_USER_IDS = set([s.strip() for s in (os.getenv("ADMIN_USER_IDS") or "").split(",") if s.strip()])
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5"))
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "10"))
DEFAULT_MONTHLY_CAP = int(os.getenv("MONTHLY_GLOBAL_CAP", "100"))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ------------- Initialize Firebase (if credentials provided) -------------
FIREBASE_READY = False
try:
    if not firebase_admin._apps:
        if os.path.exists("firebase.json") and FIREBASE_DB_URL:
            cred = credentials.Certificate("firebase.json")
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            FIREBASE_READY = True
            logger.info("Firebase initialized from firebase.json")
        elif FIREBASE_CREDS_JSON and FIREBASE_DB_URL:
            cred_dict = json.loads(FIREBASE_CREDS_JSON)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            FIREBASE_READY = True
            logger.info("Firebase initialized from FIREBASE_CREDS_JSON")
        else:
            logger.warning("Firebase credentials or DB URL missing. Firebase features disabled.")
except Exception as e:
    logger.exception("Firebase init failed: %s", e)
    FIREBASE_READY = False

# ------------- Helpers: run blocking requests in executor -------------
async def _async_post(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.post(url, **kwargs))

async def _async_get(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.get(url, **kwargs))

async def _async_put(url: str, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.put(url, **kwargs))

# ------------- Safe math (numexpr) -------------
def safe_math(expr: str):
    if not isinstance(expr, str):
        return None
    if not re.match(r'^[0-9+\-*/().\s]+$', expr):
        return None
    try:
        val = numexpr.evaluate(expr)
        try:
            return val.item()
        except Exception:
            return float(val)
    except Exception:
        return None

# ------------- Firebase usage helpers -------------
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
    if not FIREBASE_READY:
        return
    url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}.json"
    await _async_put(url, json={"count": int(count), "last_ts": float(last_ts)})

async def increment_usage(user_id: str):
    if not FIREBASE_READY:
        return
    # increment daily count
    day_count_url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}/count.json"
    resp = await _async_get(day_count_url)
    cur = 0
    if resp.status_code == 200 and resp.json() is not None:
        cur = int(resp.json())
    await _async_put(day_count_url, json=cur + 1)
    # update last_ts
    ts_url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}/last_ts.json"
    await _async_put(ts_url, json=time.time())
    # increment monthly total
    month_key = _month_key()
    month_url = f"{FIREBASE_DB_URL}/usage_images/{month_key}/total_count.json"
    resp2 = await _async_get(month_url)
    curm = 0
    if resp2.status_code == 200 and resp2.json() is not None:
        curm = int(resp2.json())
    await _async_put(month_url, json=curm + 1)

async def get_daily_limit(user_id: str):
    if not FIREBASE_READY:
        return DEFAULT_DAILY_LIMIT
    url = f"{FIREBASE_DB_URL}/limits/{user_id}/daily.json"
    resp = await _async_get(url)
    if resp.status_code == 200 and resp.json() is not None:
        return int(resp.json())
    return DEFAULT_DAILY_LIMIT

async def get_monthly_total():
    if not FIREBASE_READY:
        return 0
    url = f"{FIREBASE_DB_URL}/usage_images/{_month_key()}/total_count.json"
    resp = await _async_get(url)
    if resp.status_code == 200 and resp.json() is not None:
        return int(resp.json())
    return 0

async def reset_monthly_total():
    if not FIREBASE_READY:
        return
    url = f"{FIREBASE_DB_URL}/usage_images/{_month_key()}/total_count.json"
    await _async_put(url, json=0)

async def reset_user_daily(user_id: str):
    if not FIREBASE_READY:
        return
    url = f"{FIREBASE_DB_URL}/usage/{user_id}/{_today_key()}.json"
    await _async_put(url, json={"count": 0, "last_ts": 0.0})

# ------------- Parse image args -------------
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

# ------------- Vertex AI image generation (REST) -------------
SIZE_MAP = {"512": "512x512", "768": "768x768", "1024": "1024x1024"}

async def vertex_generate_image(prompt: str, size: str | None = None, seed: int | None = None, negative: str | None = None):
    if not (VERTEX_PROJECT_ID and GEMINI_API_KEY and VERTEX_LOCATION):
        logger.error("Vertex configuration missing")
        return None

    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/"
        f"{VERTEX_PROJECT_ID}/locations/{VERTEX_LOCATION}/publishers/google/models/imagegeneration:predict?key={GEMINI_API_KEY}"
    )

    parameters = {"sampleCount": 1, "imageSize": SIZE_MAP.get(size or "1024", "1024x1024")}
    if seed is not None:
        parameters["seed"] = int(seed)

    final_prompt = prompt
    if negative:
        parameters["negativePrompt"] = negative
        final_prompt = f"{prompt}. Avoid: {negative}"

    payload = {"instances": [{"prompt": final_prompt}], "parameters": parameters}
    headers = {"Content-Type": "application/json"}

    try:
        resp = await _async_post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        enc = None
        if isinstance(data.get("predictions"), list) and data["predictions"]:
            pred = data["predictions"][0]
            enc = pred.get("bytesBase64Encoded") or pred.get("b64") or pred.get("imageBytes") or None
            if not enc:
                for v in pred.values():
                    if isinstance(v, str) and len(v) > 100:
                        enc = v
                        break
        if not enc:
            logger.error("No base64 image in Vertex response: %s", data)
            return None
        return base64.b64decode(enc)
    except Exception as e:
        logger.exception("Vertex image generation failed: %s", e)
        return None

# ... (Telegram commands, admin commands, webhook, app setup — cleaned similarly) ...
