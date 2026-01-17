import os
import asyncio
import uuid
import html
from datetime import datetime, timedelta
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)

import engine

# ===== Config =====
TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID") or 0)  # opcijsko; 0 pomeni onemogočeno

RATE_LIMIT_MAX = 3
RATE_LIMIT_WINDOW_MIN = 10

# Conversation states
TIME, LOCATION = range(2)

# pair_id -> { location, when, a, b, votes }
pending_pairs = {}

# user_id -> list časov (datetime) za /start (anti-spam)
recent_starts: Dict[int, List[datetime]] = {}


def _rate_limited(user_id: int) -> bool:
    now = datetime.now()
    window_start = now - timedelta(minutes=RATE_LIMIT_WINDOW_MIN)

    times = recent_starts.get(user_id, [])
    # obdrži samo čase znotraj okna
    times = [t for t in times if t >= window_start]

    if len(times) >= RATE_LIMIT_MAX:
        recent_starts[user_id] = times
        return True

    times.append(now)
    recent_starts[user_id] = times
    return False


async def reset_user_state(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove user from waiting queue and from any pending confirmation pairs."""

    # Remove from waiting queue
    try:
        engine.cancel_wait(user_id)
    except Exception:
        pass

    # Remove from any pending pairs and notify the other user
    to_remove = []
    for pid, pair in pending_pairs.items():
        a = pair.get("a", {})
        b = pair.get("b", {})
        if user_id in (a.get("user_id"), b.get("user_id")):
            to_remove.append(pid)

    for pid in to_remove:
        pair = pending_pairs.get(pid)
        if not pair:
            continue
        a = pair.get("a", {})
        b = pair.get("b", {})

        other = b if user_id == a.get("user_id") else a
        other_chat = other.get("chat_id")
        if other_chat:
            try:
                await context.bot.send_message(
                    other_chat,
                    "Druga oseba je začela znova (/start), zato je ujemanje preklicano. Poskusi znova z /start.",
                )
            except Exception:
                pass

        pending_pairs.pop(pid, None)


# ===== Helpers =====

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def display_user(u: dict) -> str:
    """Return a clickable reference to a Telegram user.

    - If username exists: link to https://t.me/<username>
    - Else: use tg://user?id=<user_id> deep link
    """

    name = html.escape(u.get("name") or "uporabnik")
    username = u.get("username")
    user_id = u.get("user_id")

    if username:
        username = str(username).strip().lstrip("@")
        safe_username = html.escape(username)
        return f'<a href="https://t.me/{safe_username}">{name}</a>'

    if user_id is not None:
        try:
            uid = int(user_id)
            return f'<a href="tg://user?id={uid}">{name}</a>'
        except Exception:
            pass

    return name


# ===== Handlers =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # anti-spam: omeji pogostost /start
    if _rate_limited(update.effective_user.id):
        await update.message.reply_text(
            "Preveč poskusov v kratkem času. Poskusi znova čez nekaj minut 🙂"
        )
        return ConversationHandler.END

    # /start naj vedno resetira stanje
    context.user_data.clear()
    await reset_user_state(update.effective_user.id, context)

    keyboard = [
        [InlineKeyboardButton("🍽 Zdaj", callback_data="t:0")],
        [InlineKeyboardButton("⏰ Čez 30 min", callback_data="t:30")],
        [InlineKeyboardButton("🕐 Čez 1 uro", callback_data="t:60")],
    ]
    await update.message.reply_text(
        "Kdaj želiš na bone?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TIME


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    offset = int(q.data.split(":")[1])
    context.user_data["offset"] = offset

    keyboard = [
        [InlineKeyboardButton("Center", callback_data="l:Center")],
        [InlineKeyboardButton("Rožna", callback_data="l:Rožna")],
        [InlineKeyboardButton("Bežigrad", callback_data="l:Bežigrad")],
        [InlineKeyboardButton("Šiška", callback_data="l:Šiška")],
        [InlineKeyboardButton("Vič", callback_data="l:Vič")],
        [InlineKeyboardButton("Drugo", callback_data="l:Drugo")],
    ]

    await q.edit_message_text(
        "Kje želiš jest?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return LOCATION


async def location_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    location = q.data.split(":")[1]
    offset = int(context.user_data.get("offset", 0))

    user = q.from_user
    when = datetime.now() + timedelta(minutes=offset)

    match = engine.add_request(
        user_id=user.id,
        chat_id=q.message.chat_id,
        location=location,
        when=when,
        username=user.username,
        name=user.full_name,
    )

    if not match:
        await q.edit_message_text(
            f"OK — še ni nikogar za {location} okoli {fmt_time(when)}.\n"
            "Ko se pojavi match, ti napišem.\n"
            "Prekličeš lahko z /cancel."
        )
        return ConversationHandler.END

    # match found → confirmation flow
    pair_id = uuid.uuid4().hex[:8]
    pending_pairs[pair_id] = {
        "location": match["location"],
        "when": match["when"],
        "a": match["a"],
        "b": match["b"],
        "votes": {},
    }

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ DA", callback_data=f"yes:{pair_id}"),
            InlineKeyboardButton("❌ NE", callback_data=f"no:{pair_id}"),
        ]
    ])

    text = (
        f"Našel sem nekoga za {location} okoli {fmt_time(match['when'])}. Greš?\n\n"
        "Če klikneš ✅ DA, bo druga oseba videla tvoj Telegram profil.\n"
        "Predlog: dobita se na javnem mestu (menza/bonomat)."
    )

    for p in (match["a"], match["b"]):
        try:
            await context.bot.send_message(p["chat_id"], text, reply_markup=kb)
        except Exception:
            pass

    await q.edit_message_text("Našel sem ujemanje — preveri potrditev.")
    return ConversationHandler.END


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    decision, pair_id = q.data.split(":")
    pair = pending_pairs.get(pair_id)

    if not pair:
        await q.edit_message_text("Ujemanje ni več aktivno.")
        return

    user_id = q.from_user.id
    pair["votes"][user_id] = decision == "yes"

    a, b = pair["a"], pair["b"]

    if False in pair["votes"].values():
        # someone said NO
        for p in (a, b):
            try:
                await context.bot.send_message(p["chat_id"], "Ujemanje je bilo preklicano.")
            except Exception:
                pass
        pending_pairs.pop(pair_id, None)
        return

    if pair["votes"].get(a["user_id"]) and pair["votes"].get(b["user_id"]):
        msg = (
            "Super! Dogovorita se direktno tukaj v Telegramu:\n"
            f"• {display_user(a)}\n"
            f"• {display_user(b)}\n\n"
            "Tip: napiši 'Hej, greva na bone?'"
        )
        for p in (a, b):
            try:
                await context.bot.send_message(
                    p["chat_id"],
                    msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        pending_pairs.pop(pair_id, None)
        return

    await q.edit_message_text("Zabeleženo — čakam še na drugo osebo …")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    removed = engine.cancel_wait(update.effective_user.id)
    if removed:
        await update.message.reply_text("Preklicano — ne čakaš več na match.")
    else:
        await update.message.reply_text("Trenutno ne čakaš na match.")


# ===== Help Command =====
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍽️ *BoniBuddy – kako deluje*\n\n"
        "▶️ *Kako začneš*\n"
        "1️⃣ Klikni /start\n"
        "2️⃣ Izbereš, kdaj želiš jest\n"
        "3️⃣ Izbereš lokacijo\n\n"
        "🔄 *Kaj se zgodi potem*\n"
        "• Če se najde match, oba potrdita ✅ DA/❌ NE\n"
        "• Ko klikneš ✅ DA, druga oseba vidi tvoj Telegram profil\n"
        "• Povežeta se in gresta jest\n\n"
        "🛡️ *Varnost*\n"
        "• Dobita se na javnem mestu\n"
        "• Če ti ni OK, prekliči z /cancel\n\n"
        "🚩 Če je kdo neprimeren: /report <opis>",
        parse_mode="Markdown",
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uporabnik lahko prijavi težavo ali neprimerno vedenje.

    Za MVP: pošlje adminu (če je nastavljen ADMIN_CHAT_ID).
    """

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Napiši: /report <kaj se je zgodilo>\n"
            "Primer: /report Uporabnik je bil nesramen po matchu"
        )
        return

    u = update.effective_user
    who = f"{u.full_name} (@{u.username})" if u.username else u.full_name

    if not ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Hvala! Report sem prejel, ampak admin ID še ni nastavljen.\n"
            "Za zdaj mi pošlji screenshot ali opis neposredno."  
        )
        return

    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🚩 REPORT\nOd: {who}\nuser_id: {u.id}\n\n{text}",
        )
        await update.message.reply_text("Hvala — report je poslan. 🙏")
    except Exception:
        await update.message.reply_text("Ups — reporta nisem uspel poslati. Poskusi znova kasneje.")


# ===== App =====

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN ni nastavljen")

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TIME: [CallbackQueryHandler(time_selected, pattern=r"^t:")],
            LOCATION: [CallbackQueryHandler(location_selected, pattern=r"^l:")],
        },
        allow_reentry=True,
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(confirm, pattern=r"^(yes|no):"))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("report", report_cmd))

    print("Bot je zagnan …")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()