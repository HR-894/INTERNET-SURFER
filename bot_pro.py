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
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")                # e.g. https://project-id.firebaseio.com
FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDS_JSON")        # service account JSON as one-line string (preferred for Vercel)
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")                  # used for Generative Language / Vertex REST key
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
    """Return dict with 'count' and 'last_ts' for today for given user."""
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
    """Increment user's daily count and monthly global total."""
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
        # common key:
        enc = None
        if isinstance(data.get("predictions"), list) and data["predictions"]:
            pred = data["predictions"][0]
            enc = pred.get("bytesBase64Encoded") or pred.get("b64") or pred.get("imageBytes") or None
            if not enc:
                # search for string-like value
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

# ------------- Cooldown check (per-user via Firebase last_ts) -------------
async def check_and_update_cooldown(user_id: str, min_gap: int = COOLDOWN_SECONDS):
    usage = await get_usage(user_id)
    now = time.time()
    last = usage.get("last_ts", 0.0)
    if now - last < min_gap:
        return False
    # update last_ts (without incrementing count)
    await set_usage(user_id, usage.get("count", 0), now)
    return True

# ------------- Telegram command handlers -------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✨ *Surfer Bot — Help & Commands* ✨\n\n"
        "Text commands (unlimited):\n"
        "• /ask `<question>` — Ask the AI (Gemini).\n"
        "• /search `<math or query>` — Safe math first, else Google search (if configured).\n\n"
        "Image generation (daily/monthly quotas + friendly cooldown):\n"
        "• /image `<prompt>` [options]\n"
        "  Options: `--size 512|768|1024`, `--seed <int>`, `--no <negative prompt>`\n"
        "  Example: `/image a cinematic dragon --size 1024 --seed 42 --no watermark`\n\n"
        "Quota & admin:\n"
        "• /quota — show your daily usage\n"
        "• /resetquota `<user_id>` — admin only\n"
        "• /setlimit `<user_id> <n>` — admin only, set daily limit\n"
        "• /resetmonth — admin only, reset monthly global counter\n"
        "• /checkquota — admin or anyone (shows month usage)\n"
        "• /stats — admin only, today usage summary\n\n"
        "Friendly notes:\n"
        "• Image generation has a per-user cooldown (5s) and friendly messages.\n"
        "• Text commands are not counted against image quota.\n"
    )
    await update.message.reply_markdown(txt)

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Example: /ask What is GPT?")
        return
    query = " ".join(context.args)
    await update.message.reply_text("🧠 Thinking... (Gemini)")
    if GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": query}]}]}
        headers = {"Content-Type": "application/json"}
        try:
            resp = await _async_post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            ans = data["candidates"][0]["content"]["parts"][0]["text"]
            await update.message.reply_text(ans)
            return
        except Exception as e:
            logger.exception("Gemini failed: %s", e)
    # fallback echo
    await update.message.reply_text(f"💬 (fallback) You asked: {query}")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Example: /search (5*4)/2 or /search Taj Mahal")
        return
    query = " ".join(context.args)
    m = safe_math(query)
    if m is not None:
        await update.message.reply_html(f"🧮 <b>{query} = {m}</b>")
        return
    if GOOGLE_API_KEY and SEARCH_ENGINE_ID:
        await update.message.reply_text(f"🔎 Searching Google for: {query}")
        api_url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": GOOGLE_API_KEY, "cx": SEARCH_ENGINE_ID, "q": query, "num": 3}
        try:
            resp = await _async_get(api_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])[:3]
            if not items:
                await update.message.reply_text("Kuch khaas nahi mila.")
                return
            for it in items:
                title = it.get("title", "No Title")
                link = it.get("link", "#")
                snippet = (it.get("snippet") or "").replace("\n", " ")
                await update.message.reply_html(f"▶️ <b>{title}</b>\n📝 <i>{snippet}</i>\n<a href='{link}'>Read more</a>")
            return
        except Exception as e:
            logger.exception("Google search failed: %s", e)
            await update.message.reply_text("Google search me problem aa rahi hai.")
            return
    await update.message.reply_text("Google keys missing and not a math expression.")

async def quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    usage = await get_usage(user_id)
    limit = await get_daily_limit(user_id)
    await update.message.reply_text(f"📦 Aaj tumne {usage['count']}/{limit} images use kiye hain. Baaki: {max(0, limit-usage['count'])}")

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)

    prompt_text, size_flag, seed_flag, negative_flag = parse_image_args(context.args)
    if not prompt_text:
        await update.message.reply_text("🖼 Example: /image a beautiful landscape --size 1024 --seed 42 --no watermark")
        return

    # cooldown (friendly)
    ok = await check_and_update_cooldown(user_id)
    if not ok:
        await update.message.reply_text(f"⏳ Chill karo yaar! 5 second ka traffic signal hai, fir dobara try karo 😜")
        return

    usage = await get_usage(user_id)
    daily_limit = await get_daily_limit(user_id)
    if usage["count"] >= daily_limit:
        await update.message.reply_text("🚫 Arre boss! Aaj ka daily image limit khatam ho gaya. Kal fir try karo 😅")
        return

    monthly_total = await get_monthly_total()
    if monthly_total >= DEFAULT_MONTHLY_CAP:
        await update.message.reply_text("🚫 Arre boss! Is mahine ka global image quota full ho gaya 😅 Next month fresh supply milegi.")
        return
    elif monthly_total >= 0.8 * DEFAULT_MONTHLY_CAP:
        await update.message.reply_text("⚠️ Heads-up: Global monthly quota 80% cross ho chuki hai. Jaldi use mat maar do!")

    await update.message.reply_text("🎨 Artist kaam shuru kar raha hai... thoda sa intezar karo 😉")

    img_bytes = await vertex_generate_image(prompt_text, size=size_flag, seed=seed_flag, negative=negative_flag)
    if not img_bytes:
        await update.message.reply_text("💥 Image banane me problem aayi. Ho sakta hai prompt safe na ho ya API busy ho.")
        return

    try:
        bio = io.BytesIO(img_bytes)
        bio.name = "ai_image.png"
        bio.seek(0)
        caption = f"{prompt_text}  (size={size_flag or '1024'}, seed={seed_flag or 'auto'})"
        await update.message.reply_photo(photo=InputFile(bio), caption=caption)
    except Exception as e:
        logger.exception("Sending image failed: %s", e)
        await update.message.reply_text("Image bhejne me problem aa gayi.")

    # increment counters
    await increment_usage(user_id)

# ------------- Admin commands -------------
def is_admin(user_id):
    return str(user_id) in ADMIN_USER_IDS

async def resetquota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if not is_admin(caller):
        await update.message.reply_text("❌ Tum admin nahi ho.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /resetquota <user_id>")
        return
    uid = context.args[0]
    await reset_user_daily(uid)
    await update.message.reply_text(f"✅ Reset daily quota for {uid}")

async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if not is_admin(caller):
        await update.message.reply_text("❌ Tum admin nahi ho.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setlimit <user_id> <daily_limit>")
        return
    uid = context.args[0]
    try:
        n = int(context.args[1])
    except ValueError:
        await update.message.reply_text("daily_limit must be an integer")
        return
    if FIREBASE_READY:
        url = f"{FIREBASE_DB_URL}/limits/{uid}/daily.json"
        await _async_put(url, json=n)
    await update.message.reply_text(f"✅ Set daily limit for {uid} to {n}")

async def resetmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if not is_admin(caller):
        await update.message.reply_text("❌ Tum admin nahi ho.")
        return
    await reset_monthly_total()
    await update.message.reply_text("✅ Monthly global quota reset done.")

async def checkquota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monthly_total = await get_monthly_total()
    cap = int(os.getenv("MONTHLY_GLOBAL_CAP", DEFAULT_MONTHLY_CAP))
    await update.message.reply_text(f"📅 This month: {monthly_total}/{cap} images used.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if not is_admin(caller):
        await update.message.reply_text("❌ Tum admin nahi ho.")
        return
    if not FIREBASE_READY:
        await update.message.reply_text("Firebase not ready.")
        return
    url = f"{FIREBASE_DB_URL}/usage.json"
    resp = await _async_get(url)
    total = 0
    lines = []
    if resp.status_code == 200 and resp.json():
        data = resp.json()
        today = _today_key()
        for uid, dates in (data.items() if isinstance(data, dict) else []):
            c = 0
            if isinstance(dates, dict) and today in dates and isinstance(dates[today], dict):
                c = int(dates[today].get("count", 0))
            if c:
                total += c
                lines.append(f"{uid}: {c}")
    out = f"📊 Today total images: {total}\n" + ("\n".join(lines) if lines else "No data")
    await update.message.reply_text(out)

# ------------- App & webhook setup -------------
app = Flask(__name__)

# Build the Telegram application (serverless-safe)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Register handlers
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("start", help_command))  # start -> show help
application.add_handler(CommandHandler("ask", ask_command))
application.add_handler(CommandHandler("search", search_command))
application.add_handler(CommandHandler("image", image_command))
application.add_handler(CommandHandler("quota", quota_command))
application.add_handler(CommandHandler("resetquota", resetquota_cmd))
application.add_handler(CommandHandler("setlimit", setlimit_cmd))
application.add_handler(CommandHandler("resetmonth", resetmonth_cmd))
application.add_handler(CommandHandler("checkquota", checkquota_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))

# Set bot commands (menu)
async def post_init(apply):
    await apply.bot.set_my_commands([
        BotCommand("help", "Show help & commands"),
        BotCommand("ask", "Ask a question to the AI"),
        BotCommand("search", "Safe math or Google search"),
        BotCommand("image", "Generate AI image (10/day default)"),
        BotCommand("quota", "Show today's image usage"),
    ])

# Add post_init to application
application.post_init = post_init

# Health endpoint
@app.get("/")
def health():
    return "ok"

# Webhook endpoint for Telegram updates — Vercel will POST here
@app.route(f"/{BOT_SECRET}", methods=["POST"])
def webhook():
    try:
        update_data = flask_request.get_json(force=True, silent=True)
        if not update_data:
            return "no data", 400
        update_obj = Update.de_json(update_data, application.bot)
        application.update_queue.put_nowait(update_obj)
        return "ok"
    except Exception as e:
        logger.exception("Webhook processing failed: %s", e)
        return "error", 500

# Local run (for testing)
# if __name__ == "__main__":
#     logger.info("Running bot in polling mode (local).")
#     application.run_polling()
