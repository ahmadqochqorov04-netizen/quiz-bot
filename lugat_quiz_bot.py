# -*- coding: utf-8 -*-
"""
Turkcha-o'zbekcha lug'at (A1) asosidagi Telegram quiz boti.
© Akhmad Axtamovich · Turkish Center

Imkoniyatlar:
  • Har bir mavzu uchun to'liq test: mavzudagi HAR BIR so'zga savol (yoki 10/25/50 ta tanlab).
  • Har bir savolga javob berish uchun 10 soniya (Telegram taymeri bilan).
  • Shaxsiy chatda ham, GURUHDA ham ishlaydi. Guruhda hamma birgalikda qatnashadi,
    oxirida (va har 10 savoldan keyin) natijalar jadvali chiqadi.

Ishga tushirish:
  1) pip install "python-telegram-bot>=20"
  2) Dasturni ishga tushirganda token so'raladi (u token.txt ga saqlanadi).
     Windows da start_bot.bat ni ikki marta bosing.
  3) questions.json shu fayl bilan bir papkada tursin.
"""
import asyncio
import json
import logging
import os
import random
import time

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      Poll, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, PollAnswerHandler)

# ---------------------------- SOZLAMALAR ----------------------------
TIME_LIMIT = 10        # har bir savolga javob berish vaqti (soniya)
GROUP_PAUSE = 2        # guruhda: savol yopilgandan keyingi pauza (soniya)
PRIVATE_PAUSE = 1.5    # shaxsiy chatda: javobdan keyingi pauza (soniya)
LEADER_EVERY = 10      # guruhda har necha savoldan keyin oraliq natija chiqsin
COUNT_CHOICES = (10, 25, 50, 100)
CREDIT = "© Akhmad Axtamovich · Turkish Center"
# --------------------------------------------------------------------

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "questions.json"), encoding="utf-8") as f:
    QUESTIONS = json.load(f)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lugat-quiz")

UNIT_NAMES = {
    "1": "1-mavzu · Tanışma", "2": "2-mavzu · Ailemiz", "3": "3-mavzu · Günlük Hayat",
    "4": "4-mavzu · Çevremiz", "5": "5-mavzu · Meslekler", "6": "6-mavzu · Ulaşım",
    "7": "7-mavzu · İletişim", "8": "8-mavzu · Tatil",
}
DIR_NAMES = {
    "tr2uz": "Turkcha → O'zbekcha",
    "uz2tr": "O'zbekcha → Turkcha",
    "mix": "Aralash",
}
MEDALS = ["🥇", "🥈", "🥉"]

SESSIONS = {}       # chat_id -> Session
POLL_TO_CHAT = {}   # poll_id -> chat_id


class Session:
    def __init__(self, chat_id, questions, is_group, starter_id, title):
        self.chat_id = chat_id
        self.qs = questions
        self.is_group = is_group
        self.starter_id = starter_id
        self.title = title
        self.scores = {}          # user_id -> {"name", "correct", "answered", "time"}
        self.cur = None           # joriy savol holati
        self.stop_event = asyncio.Event()
        self.task = None
        self.timeouts = 0
        self.mistakes = []        # shaxsiy chat uchun: xato qilingan so'zlar


# ------------------------------ YORDAMCHI ------------------------------
def is_group_chat(update):
    return update.effective_chat.type in ("group", "supergroup")


async def is_admin(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def can_control(update, context, owner_id=None):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        return True
    if owner_id is not None and user.id == owner_id:
        return True
    return await is_admin(context, chat.id, user.id)


def unit_kb():
    rows = [[InlineKeyboardButton(n, callback_data="u:" + k)] for k, n in UNIT_NAMES.items()]
    rows.append([InlineKeyboardButton("Barcha mavzular", callback_data="u:all")])
    return InlineKeyboardMarkup(rows)


def dir_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton(n, callback_data="d:" + k)] for k, n in DIR_NAMES.items()])


def count_kb(total):
    row = [InlineKeyboardButton(f"{c} ta", callback_data=f"n:{c}") for c in COUNT_CHOICES if c < total]
    rows = [row[i:i + 2] for i in range(0, len(row), 2)]
    rows.append([InlineKeyboardButton(f"Hammasi ({total} ta)", callback_data="n:all")])
    return InlineKeyboardMarkup(rows)


def pool_for(unit, direction):
    return [q for q in QUESTIONS
            if (unit == "all" or str(q["u"]) == unit) and (direction == "mix" or q["dir"] == direction)]


def leaderboard_lines(s, limit=15):
    ranked = sorted(s.scores.values(), key=lambda x: (-x["correct"], x["time"]))
    lines = []
    for i, st in enumerate(ranked[:limit]):
        medal = MEDALS[i] if i < 3 else f"{i + 1}."
        avg = f" · {st['time'] / st['correct']:.1f} s" if st["correct"] else ""
        lines.append(f"{medal} {st['name']} — {st['correct']} ta to'g'ri{avg}")
    return lines


async def wait_any(s, ev, timeout):
    """ev (javob berildi) yoki stop signali yoki timeout. ev ishlasa True qaytaradi."""
    t1 = asyncio.create_task(ev.wait())
    t2 = asyncio.create_task(s.stop_event.wait())
    done, pending = await asyncio.wait({t1, t2}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    return t1 in done


# ------------------------------ KOMANDALAR ------------------------------
HELP_TEXT = (
    "Bu bot turk tili A1 lug'ati bo'yicha quiz o'tkazadi.\n\n"
    "• /test — testni boshlash (mavzu, test turi va savollar sonini tanlaysiz)\n"
    "• /stop — testni to'xtatish\n"
    "• Har bir savolga 10 soniya beriladi.\n"
    "• Guruhda: botni guruhga qo'shing va /test yozing. Hamma birga qatnashadi, "
    "oxirida natijalar jadvali chiqadi.\n\n" + CREDIT
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text("Salom! Guruhda testni boshlash uchun /test yozing.\n\n" + CREDIT)
    else:
        await update.message.reply_text(
            "Salom! Bu — turk tili A1 lug'ati bo'yicha quiz bot.\nTestni boshlash uchun /test bosing yoki "
            "mavzuni tanlang:\n\n" + CREDIT, reply_markup=unit_kb())
        context.chat_data["setup"] = {"starter": update.effective_user.id}


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.id in SESSIONS:
        await update.message.reply_text("Hozir test davom etmoqda. To'xtatish uchun /stop.")
        return
    context.chat_data["setup"] = {"starter": update.effective_user.id}
    await update.message.reply_text("Mavzuni tanlang:", reply_markup=unit_kb())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = SESSIONS.get(chat.id)
    if not s:
        await update.message.reply_text("Hozir test yo'q. Boshlash uchun /test.")
        return
    if not await can_control(update, context, s.starter_id):
        await update.message.reply_text("Testni faqat uni boshlagan kishi yoki guruh admini to'xtata oladi.")
        return
    s.stop_event.set()


# ------------------------------ MENYU TUGMALARI ------------------------------
async def _guard(update, context):
    q = update.callback_query
    setup = context.chat_data.get("setup")
    if not setup:
        await q.answer("Menyu eskirgan. /test ni qayta bosing.", show_alert=True)
        return None
    if not await can_control(update, context, setup["starter"]):
        await q.answer("Bu tugmani faqat testni boshlagan kishi yoki guruh admini bosa oladi.", show_alert=True)
        return None
    await q.answer()
    return setup


async def cb_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = await _guard(update, context)
    if setup is None:
        return
    setup["unit"] = update.callback_query.data.split(":", 1)[1]
    title = UNIT_NAMES.get(setup["unit"], "Barcha mavzular")
    await update.callback_query.edit_message_text(f"{title}\nTest turini tanlang:", reply_markup=dir_kb())


async def cb_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = await _guard(update, context)
    if setup is None:
        return
    setup["dir"] = update.callback_query.data.split(":", 1)[1]
    total = len(pool_for(setup.get("unit", "all"), setup["dir"]))
    if total == 0:
        await update.callback_query.edit_message_text("Bu tanlov uchun savol topilmadi. /test ni qayta bosing.")
        return
    title = UNIT_NAMES.get(setup.get("unit", "all"), "Barcha mavzular")
    await update.callback_query.edit_message_text(
        f"{title} · {DIR_NAMES[setup['dir']]}\nNechta savol bo'lsin? (jami {total} ta savol bor)",
        reply_markup=count_kb(total))


async def cb_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = await _guard(update, context)
    if setup is None:
        return
    chat = update.effective_chat
    if chat.id in SESSIONS:
        await update.callback_query.answer("Test allaqachon boshlangan.", show_alert=True)
        return
    unit, direction = setup.get("unit", "all"), setup.get("dir", "mix")
    pool = pool_for(unit, direction)
    pool = random.sample(pool, len(pool))
    choice = update.callback_query.data.split(":", 1)[1]
    chosen = pool if choice == "all" else pool[:int(choice)]
    title = f"{UNIT_NAMES.get(unit, 'Barcha mavzular')} · {DIR_NAMES[direction]}"
    group = chat.type in ("group", "supergroup")
    s = Session(chat.id, chosen, group, setup["starter"], title)
    SESSIONS[chat.id] = s
    context.chat_data.pop("setup", None)
    extra = ("\nGuruhdagi hamma qatnashishi mumkin: har bir savolga javob bering!" if group else "")
    await update.callback_query.edit_message_text(
        f"Test boshlandi!\n{title}\nSavollar: {len(chosen)} ta · Har bir savolga {TIME_LIMIT} soniya{extra}")
    s.task = asyncio.create_task(run_session(context.application, s))


# ------------------------------ TEST JARAYONI ------------------------------
async def finish(app, s, stopped):
    bot = app.bot
    total_asked = s.cur["index"] if (stopped and s.cur) else len(s.qs)
    head = "Test to'xtatildi." if stopped else "Test tugadi!"
    if s.is_group:
        lines = leaderboard_lines(s)
        text = f"{head}\n{s.title}\nSavollar: {total_asked} ta · Ishtirokchilar: {len(s.scores)} ta\n\n"
        text += "\n".join(lines) if lines else "Hech kim javob bermadi."
        text += f"\n\nYangi test uchun /test\n\n{CREDIT}"
    else:
        st = next(iter(s.scores.values()), {"correct": 0, "answered": 0})
        correct = st["correct"]
        percent = round(100 * correct / total_asked) if total_asked else 0
        text = (f"{head}\n{s.title}\nNatija: {correct}/{total_asked} ({percent}%)\n"
                f"Javob berilmagan: {s.timeouts} ta\n")
        if s.mistakes:
            uniq = list(dict.fromkeys(s.mistakes))[:20]
            text += "\nQayta o'rganing: " + ", ".join(uniq) + "\n"
        text += f"\nYangi test uchun /test\n\n{CREDIT}"
    await bot.send_message(s.chat_id, text)


async def run_session(app, s):
    bot = app.bot
    stopped = False
    try:
        total = len(s.qs)
        if s.is_group:
            await asyncio.sleep(3)
        for i, q in enumerate(s.qs, 1):
            if s.stop_event.is_set():
                stopped = True
                break
            options = [q["correct"]] + list(q["wrong"])
            random.shuffle(options)
            cid = options.index(q["correct"])
            msg = await bot.send_poll(
                chat_id=s.chat_id,
                question=f"{i}/{total}. {q['q']}",
                options=options,
                type=Poll.QUIZ,
                correct_option_id=cid,
                is_anonymous=False,
                open_period=TIME_LIMIT,
                explanation=q["exp"],
            )
            ev = asyncio.Event()
            s.cur = {"poll_id": msg.poll.id, "cid": cid, "sent": time.monotonic(), "event": ev,
                     "answered": set(), "n_correct": 0, "index": i, "src": q.get("src", "")}
            POLL_TO_CHAT[msg.poll.id] = s.chat_id
            if s.is_group:
                if await wait_any(s, asyncio.Event(), TIME_LIMIT + GROUP_PAUSE):
                    pass
                if s.stop_event.is_set():
                    stopped = True
                    POLL_TO_CHAT.pop(msg.poll.id, None)
                    break
                await bot.send_message(
                    s.chat_id,
                    f"✅ To'g'ri javob: {q['correct']}  ({s.cur['n_correct']}/{len(s.cur['answered'])} kishi to'g'ri javob berdi)")
                if i % LEADER_EVERY == 0 and i < total:
                    lines = leaderboard_lines(s, limit=5)
                    if lines:
                        await bot.send_message(s.chat_id, f"Oraliq natija ({i}/{total}):\n" + "\n".join(lines))
            else:
                answered = await wait_any(s, ev, TIME_LIMIT + 1)
                if s.stop_event.is_set():
                    stopped = True
                    POLL_TO_CHAT.pop(msg.poll.id, None)
                    break
                if not answered:
                    s.timeouts += 1
                    s.mistakes.append(q.get("src", ""))
                    await bot.send_message(s.chat_id, f"⏰ Vaqt tugadi. To'g'ri javob: {q['correct']}")
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(PRIVATE_PAUSE)
            POLL_TO_CHAT.pop(msg.poll.id, None)
        await finish(app, s, stopped)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Test paytida xatolik")
        try:
            await bot.send_message(s.chat_id, "Testda xatolik yuz berdi. /test bilan qayta boshlang.")
        except Exception:
            pass
    finally:
        SESSIONS.pop(s.chat_id, None)


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = update.poll_answer
    chat_id = POLL_TO_CHAT.get(a.poll_id)
    s = SESSIONS.get(chat_id) if chat_id is not None else None
    if not s or not s.cur or s.cur["poll_id"] != a.poll_id or a.user is None:
        return
    if not a.option_ids or a.user.id in s.cur["answered"]:
        return
    uid = a.user.id
    s.cur["answered"].add(uid)
    st = s.scores.setdefault(uid, {"name": a.user.full_name, "correct": 0, "answered": 0, "time": 0.0})
    st["answered"] += 1
    if a.option_ids[0] == s.cur["cid"]:
        st["correct"] += 1
        st["time"] += time.monotonic() - s.cur["sent"]
        s.cur["n_correct"] += 1
    elif not s.is_group:
        s.mistakes.append(s.cur.get("src", ""))
    if not s.is_group:
        s.cur["event"].set()


# ------------------------------ ISHGA TUSHIRISH ------------------------------
def load_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    path = os.path.join(BASE, "token.txt")
    if not token and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        token = input("BotFather bergan tokenni shu yerga qo'ying va Enter bosing: ").strip()
        if token:
            with open(path, "w", encoding="utf-8") as f:
                f.write(token)
    return token


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("test", "Testni boshlash"),
        BotCommand("stop", "Testni to'xtatish"),
        BotCommand("help", "Yordam"),
    ])


def main():
    token = load_token()
    if not token:
        raise SystemExit("Token kiritilmadi.")
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["test", "quiz"], cmd_test))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler(["help", "yordam"], cmd_help))
    app.add_handler(CallbackQueryHandler(cb_unit, pattern=r"^u:"))
    app.add_handler(CallbackQueryHandler(cb_dir, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(cb_count, pattern=r"^n:"))
    app.add_handler(PollAnswerHandler(on_answer))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
