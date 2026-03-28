#!/usr/bin/env python3
"""
🏏 T20 GFI Signal Bot — Telegram
Send a Sky Exchange Market Depth screenshot → get instant GFI betting signal
"""

import os, json, base64, logging, asyncio
from io import BytesIO
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert T20 cricket betting analyst specialising in exchange markets (Sky Exchange, Betfair).
Analyse the screenshot(s) from Sky Exchange Market Depth and return ONLY a valid JSON object. No markdown, no backticks. Pure JSON.

Required structure:
{
  "match": "Team A vs Team B",
  "phase": "pre-match|powerplay|middle|death|innings_break|chasing",
  "over": number or null,
  "score": "runs-wickets" or null,
  "crr": number or null,
  "rrr": number or null,
  "target": number or null,
  "team1": {"name": "...", "backPrice": number, "selectionVolume": number or null},
  "team2": {"name": "...", "backPrice": number, "selectionVolume": number or null},
  "totalMatched": number or null,
  "observations": ["observation 1", "observation 2", "observation 3"],
  "rlm_detected": true or false,
  "rlm_reasoning": "explanation or null",
  "volume_concentration_pct": number (0-100, % on favourite),
  "favourite_team": "team name",
  "price_trend": "shortening|drifting|stable|unknown"
}"""

# Store multi-screenshot sessions per user
user_sessions = {}

def compute_gfi(d):
    score = 0
    t1, t2 = d.get("team1", {}), d.get("team2", {})
    p1, p2 = t1.get("backPrice"), t2.get("backPrice")
    if not p1 or not p2:
        return 40
    fav_p = min(p1, p2)
    dog_p = max(p1, p2)
    # 1. Prob gap (0-20)
    score += min(20, round(((1/fav_p) - (1/dog_p)) * 80))
    # 2. Volume concentration (0-30)
    vc = d.get("volume_concentration_pct", 0) or 0
    score += 30 if vc >= 85 else 22 if vc >= 75 else 14 if vc >= 60 else 8 if vc >= 50 else 3
    # 3. Price trend (0-20)
    trend = d.get("price_trend", "unknown")
    score += 20 if trend == "shortening" else 12 if trend == "stable" else 4 if trend == "drifting" else 10
    # 4. Phase bonus (0-8)
    phase = d.get("phase", "")
    score += 8 if phase in ["death", "innings_break"] else 4
    # 5. Base (12)
    score += 12
    # 6. RLM penalty
    if d.get("rlm_detected"):
        score -= 22
    return max(0, min(100, round(score)))

def fmt_vol(v):
    if not v: return "–"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(round(v))

def gfi_emoji(g):
    if g >= 75: return "🔥"
    if g >= 55: return "✅"
    if g >= 35: return "⚠️"
    return "😱"

def verdict_text(g, d):
    fav = d.get("favourite_team", "Favourite")
    if g >= 75: return f"STRONG BET — Back {fav}"
    if g >= 55: return "LEAN — Moderate stake"
    if g >= 35: return "WAIT — Skip or paper trade"
    if d.get("rlm_detected"): return "FADE — RLM: Back the underdog"
    return "FADE / CONTRARIAN"

def build_signal_message(d, gfi):
    t1, t2 = d.get("team1", {}), d.get("team2", {})
    fav = d.get("favourite_team", "")
    vc = d.get("volume_concentration_pct", 0) or 0
    total = fmt_vol(d.get("totalMatched"))

    # GFI bar
    filled = round(gfi / 5)
    bar = "█" * filled + "░" * (20 - filled)

    # Team display
    def team_line(t):
        is_fav = t.get("name") == fav
        star = " ⭐FAV" if is_fav else ""
        price = t.get("backPrice", "–")
        vol = fmt_vol(t.get("selectionVolume"))
        prob = f"{100/price:.1f}%" if price else "–"
        return f"{'🟢' if is_fav else '⚪'} *{t.get('name','?')}*{star}\n   Price: `{price}` | Win: `{prob}` | Vol: `{vol}`"

    # Checkpoints
    pills = []
    if d.get("over"): pills.append(f"📍 Over {d['over']}")
    if d.get("score"): pills.append(f"📊 {d['score']}")
    if d.get("crr"): pills.append(f"CRR {d['crr']}")
    if d.get("rrr"): pills.append(f"RRR {d['rrr']}")
    if d.get("target"): pills.append(f"🎯 Target {d['target']}")
    if d.get("price_trend") == "shortening": pills.append("📉 Shortening")
    if d.get("price_trend") == "drifting": pills.append("📈 Drifting")
    if vc >= 80: pills.append(f"🔥 Sharp Vol {vc:.0f}%")
    if d.get("rlm_detected"): pills.append("⚡ RLM ALERT")

    checkpoints_str = "  ".join(pills) if pills else "–"

    # Observations
    obs = d.get("observations", [])
    obs_str = "\n".join(f"• {o}" for o in obs[:3]) if obs else "–"

    # RLM warning
    rlm_str = ""
    if d.get("rlm_detected"):
        rlm_str = f"\n\n⚡ *REVERSE LINE MOVEMENT*\n_{d.get('rlm_reasoning', 'Price drifting while volume surges. Fade the favourite.')}_"

    msg = f"""🏏 *GFI SIGNAL — {d.get('match', 'Live Match')}*
━━━━━━━━━━━━━━━━━━━━

{team_line(t1)}

{team_line(t2)}

📊 *Market Stats*
Total Matched: `{total}`
Vol on Favourite: `{vc:.0f}%`
Phase: `{(d.get('phase') or '–').replace('_',' ').upper()}`

{checkpoints_str}

━━━━━━━━━━━━━━━━━━━━
🧠 *AI OBSERVATIONS*
{obs_str}
━━━━━━━━━━━━━━━━━━━━

📈 *GREED & FEAR INDEX*
`[{bar}]`
*GFI: {gfi}/100*

{gfi_emoji(gfi)} *{verdict_text(gfi, d)}*{rlm_str}
━━━━━━━━━━━━━━━━━━━━
_Send another screenshot to update the signal_"""
    return msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """🏏 *T20 GFI Signal Bot*

*How to use:*
1️⃣ Open Sky Exchange on your phone
2️⃣ Go to any live T20 match → Market Depth
3️⃣ Take a screenshot
4️⃣ Send it here → get instant GFI signal

📸 *Pro tip:* Send multiple screenshots (pre-match + 6ov + 10ov + 15ov) for a sharper read. I'll analyse all of them together.

*GFI Scale:*
🔥 75-100 = STRONG BET
✅ 55-74  = LEAN
⚠️ 35-54  = WAIT
😱 0-34   = FADE

Ready — send your screenshot now!"""
    await update.message.reply_text(msg, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # Download image
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode()

    # Add to session
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append(img_b64)

    count = len(user_sessions[user_id])

    # Show options after each image
    keyboard = [
        [InlineKeyboardButton(f"⚡ Analyse now ({count} screenshot{'s' if count>1 else ''})", callback_data="analyse")],
        [InlineKeyboardButton("📸 Add more screenshots first", callback_data="add_more")],
        [InlineKeyboardButton("🗑️ Clear & start over", callback_data="clear")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Screenshot {count} received!\n\n_Send more for a sharper signal, or tap Analyse now._",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "clear":
        user_sessions[user_id] = []
        await query.edit_message_text("🗑️ Cleared. Send a fresh screenshot to start.")
        return

    if query.data == "add_more":
        count = len(user_sessions.get(user_id, []))
        await query.edit_message_text(f"📸 Got {count} screenshot{'s' if count>1 else ''}. Send the next one!")
        return

    if query.data == "analyse":
        images = user_sessions.get(user_id, [])
        if not images:
            await query.edit_message_text("⚠️ No screenshots found. Send a Market Depth screenshot first.")
            return

        await query.edit_message_text(f"⏳ Analysing {len(images)} screenshot{'s' if len(images)>1 else ''}...\n_Reading market depth data_", parse_mode="Markdown")

        try:
            # Build content
            content = []
            for i, img_b64 in enumerate(images):
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}})
                content.append({"type": "text", "text": f"Screenshot {i+1} of {len(images)}"})
            content.append({"type": "text", "text": "Analyse all screenshots and return the JSON."})

            # Call Claude
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}]
            )

            raw = response.content[0].text
            parsed = json.loads(raw.replace("```json","").replace("```","").strip())
            gfi = compute_gfi(parsed)

            msg = build_signal_message(parsed, gfi)

            keyboard = [[
                InlineKeyboardButton("🗑️ Clear & analyse new match", callback_data="clear"),
                InlineKeyboardButton("📸 Add more screenshots", callback_data="add_more")
            ]]

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            # Clear session after analysis
            user_sessions[user_id] = []

        except json.JSONDecodeError:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Couldn't read the screenshot clearly. Please try a clearer Market Depth screenshot showing both teams."
            )
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Error: {str(e)[:100]}\nPlease try again."
            )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images sent as documents/files"""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text("Please send a screenshot image (JPG/PNG).")
        return
    # Treat as photo
    file = await context.bot.get_file(doc.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode()

    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append(img_b64)

    count = len(user_sessions[user_id])
    keyboard = [
        [InlineKeyboardButton(f"⚡ Analyse now ({count} screenshot{'s' if count>1 else ''})", callback_data="analyse")],
        [InlineKeyboardButton("📸 Add more", callback_data="add_more")],
        [InlineKeyboardButton("🗑️ Clear", callback_data="clear")]
    ]
    await update.message.reply_text(
        f"✅ Screenshot {count} received!", reply_markup=InlineKeyboardMarkup(keyboard)
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("🏏 GFI Signal Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
