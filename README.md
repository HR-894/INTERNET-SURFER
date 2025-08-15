# 🤖 Surfer Bot Pro – AI Powered Telegram Bot

Ye bot ek **multi-tool AI assistant** hai jo Gemini text, Safe Math, Google Search aur Vertex AI image generation sab handle karta hai.

---

## 🚀 Features
✅ **/ask** → Gemini se sawal ka jawab  
✅ **/search** → Safe math ya Google search  
✅ **/image** → Vertex AI se high-quality image generation  
✅ **Monthly Quota** → Firebase DB me track hota hai  
✅ **Admin Tools** → `/resetquota`, `/setlimit`, `/stats`  
✅ **Friendly messages** + per-user cooldown

---

## 🛠 Setup Guide

### 1. Clone/Upload Files
Vercel project me upload karo:
- `bot_pro.py`
- `requirements.txt`
- `vercel.json`
- `README.md`

---

### 2. Add Environment Variables  
Vercel dashboard → Settings → Environment Variables me ye add karo:

| Key | Value |
|-----|-------|
| TELEGRAM_TOKEN | (Tumhara Telegram bot token from @BotFather) |
| BOT_SECRET | hrrocks (ya koi bhi random string) |
| GEMINI_API_KEY | (Tumhara Gemini API key) |
| GOOGLE_API_KEY | (Tumhara Google Custom Search API key) |
| SEARCH_ENGINE_ID | (Tumhara Google Search Engine ID) |
| FIREBASE_DB_URL | https://<yourid>.firebaseio.com |
| VERTEX_PROJECT_ID | Tumhara GCP project ID |
| VERTEX_LOCATION | us-central1 |
| FIREBASE_CREDS_JSON | (Tumhara Firebase service account JSON pura ek line me) |

---

### 3. Deploy on Vercel
`requirements.txt` aur `vercel.json` ke saath deploy karo.

---

### 4. Set Telegram Webhook
Deploy hone ke baad, browser me ye URL open karo:

