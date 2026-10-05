import asyncio
import io
import logging
import re
import os
from contextlib import asynccontextmanager
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
    WebAppInfo,
    MenuButtonWebApp,
)

from config import ADMIN_IDS, BOT_TOKEN
from database import (
    activate_subscription,
    check_and_increment_daily,
    count_questions,
    count_users,
    create_referral,
    get_all_user_ids,
    get_or_create_user,
    get_question,
    get_random_question,
    get_user,
    get_user_stats,
    init_db,
    log_answer,
    reward_referrer_if_pending,
    seed_questions,
    set_subject,
)
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DAILY_LIMIT = 20
PRICE_MONTH_STARS = 349
PRICE_YEAR_STARS = 2490
REFERRAL_REWARD_DAYS = 7
BROADCAST_DELAY = 0.05
_bot_username = None

# ==========================================
# FASTAPI & WEB APP SETUP
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(dp.start_polling(bot))
    
    try:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        port = os.getenv("PORT", "8080")
        webapp_url = f"https://{domain}" if domain else f"http://localhost:{port}"
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🚀 Приложение", web_app=WebAppInfo(url=webapp_url)))
        logging.info(f"Web App URL set to: {webapp_url}")
    except Exception as e:
        logging.error(f"Failed to set menu button: {e}")
        
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    html_file = "index.html" if os.path.exists("index.html") else "webapp.html"
    try:
        with open(html_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Web App file not found.</h1>")

@app.get("/api/chart")
async def api_chart(user_id: int):
    stats = await get_user_stats(user_id)
    total = stats["total"]
    correct = stats["correct"]
    wrong = total - correct
    percent = round(correct / total * 100) if total else 0
    
    fig, ax = plt.subplots(figsize=(4, 4), dpi=120)    
    if total == 0:
        values, labels, colors = [1], ["Нет данных"], ["#cccccc"]
    else:
        values = [correct, wrong]
        labels = [f"Верно\n{correct}", f"Ошибки\n{wrong}"]
        colors = ["#2ecc71", "#e74c3c"]

    ax.pie(values, labels=labels, colors=colors, startangle=90, wedgeprops={"edgecolor": "white", "linewidth": 2})
    if total > 0:
        ax.text(0, 0, f"{percent}%", ha="center", va="center", fontsize=24, fontweight="bold")
    ax.axis("equal")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

@app.get("/api/stats")
async def api_stats(user_id: int):
    user = await get_user(user_id)
    if not user: 
        return JSONResponse({"status": "Гость"})
    
    status = f"💎 Premium до {user.subscription_until.strftime('%d.%m')}" if user.role == "premium" and user.subscription_until else f"🆓 Free ({DAILY_LIMIT}/день)"
    return JSONResponse({"status": status})

# ==========================================
# AIGRAM HANDLERS
# ==========================================
async def get_bot_username():
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username

def referral_link(user_id, bot_username):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def main_menu():
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    port = os.getenv("PORT", "8080")
    webapp_url = f"https://{domain}" if domain else f"http://localhost:{port}"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Приложение", web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton(text="🎯 Тренировка", callback_data="do_train")],
        [InlineKeyboardButton(text="📊 Мой прогресс", callback_data="show_stats")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="show_ref")],
        [InlineKeyboardButton(text="🔄 Сменить предмет", callback_data="change_subj")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support")],
        [InlineKeyboardButton(text="💎 Подписка", callback_data="show_subs")],
    ])

def make_pie_chart(correct: int, wrong: int, percent: int) -> bytes:
    fig, ax = plt.subplots(figsize=(5, 5), dpi=130)
    if correct + wrong == 0:
        values, labels, colors = [1], ["Нет данных"], ["#cccccc"]
    else:
        values = [correct, wrong]
        labels = [f"Правильно\n{correct}", f"Неправильно\n{wrong}"]
        colors = ["#2ecc71", "#e74c3c"]

    ax.pie(values, labels=labels, colors=colors, startangle=90, wedgeprops={"edgecolor": "white", "linewidth": 2}, textprops={"fontsize": 13, "color": "#222222"})
    if correct + wrong > 0:
        ax.text(0, 0, f"{percent}%", ha="center", va="center", fontsize=26, fontweight="bold", color="#222222")
    ax.set_title("Твой прогресс", fontsize=16, fontweight="bold", color="#222222")
    ax.axis("equal")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    parts = message.text.split(maxsplit=1)
    referrer_id = None
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            referrer_id = int(parts[1].replace("ref_", ""))
        except ValueError:
            pass
            
    user = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    if referrer_id:
        created = await create_referral(message.from_user.id, referrer_id)
        if created:
            try:
                await bot.send_message(referrer_id, "🎁 По твоей ссылке пришёл новый друг! Как только он решит первый вопрос — ты получишь +7 дней Premium.")
            except Exception:
                pass

    await message.answer("Секунду…", reply_markup=ReplyKeyboardRemove())
    
    if user and user.subject:
        name = "Математика" if user.subject == "math" else "Русский язык"
        status = "💎 Premium" if user.role == "premium" else f"🆓 Free ({DAILY_LIMIT}/день)"
        await message.answer(
            f"С возвращением! Твой предмет: {name}.\nСтатус: {status}\n\nЖми кнопку ниже 👇",
            reply_markup=main_menu(),
        )
        return
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📐 Математика", callback_data="subj_math")],
        [InlineKeyboardButton(text="📖 Русский язык", callback_data="subj_rus")],
    ])
    await message.answer("Привет! Я помогу подготовиться к ЕГЭ/ОГЭ.\nВыбери предмет 👇", reply_markup=kb)

@dp.message(F.web_app_data)
async def on_web_app_data(message: Message):
    action = message.web_app_data.data
    if action == "action=train":
        await cmd_train(message)
    elif action == "action=subscribe":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Месяц — {PRICE_MONTH_STARS}⭐", callback_data="buy_month")],
            [InlineKeyboardButton(text=f"Год — {PRICE_YEAR_STARS}⭐ (−40%)", callback_data="buy_year")],
        ])
        await message.answer("Выбери подписку — оплата в Telegram Stars:", reply_markup=kb)

@dp.callback_query(F.data.startswith("subj_"))
async def choose_subject(call: CallbackQuery):
    subject = call.data.replace("subj_", "")
    await set_subject(call.from_user.id, subject)
    name = "Математика" if subject == "math" else "Русский язык"
    await call.message.edit_text(
        f"Отлично! Твой предмет: {name}.\n\nБесплатный лимит: {DAILY_LIMIT} вопросов в день.\nНажми /train, чтобы получить вопрос."
    )
    await call.answer()

@dp.callback_query(F.data == "do_train")
async def do_train(call: CallbackQuery):
    await call.answer()
    await cmd_train(call.message)

@dp.callback_query(F.data == "change_subj")
async def change_subj(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📐 Математика", callback_data="subj_math")],
        [InlineKeyboardButton(text="📖 Русский язык", callback_data="subj_rus")],
    ])
    await call.message.edit_text("Выбери новый предмет:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "show_ref")
async def show_ref(call: CallbackQuery):
    username = await get_bot_username()
    link = referral_link(call.from_user.id, username)
    await call.message.answer(
        f"🎁 Твоя реферальная ссылка:\n\n{link}\n\nЗа каждого друга, который решит хотя бы один вопрос, ты получаешь +{REFERRAL_REWARD_DAYS} дней Premium."
    )
    await call.answer()

@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    username = await get_bot_username()
    link = referral_link(message.from_user.id, username)
    await message.answer(
        f"🎁 Твоя реферальная ссылка:\n\n{link}\n\nЗа каждого друга, который решит хотя бы один вопрос, ты получаешь +{REFERRAL_REWARD_DAYS} дней Premium."
    )

@dp.callback_query(F.data == "support")
async def support_cb(call: CallbackQuery):
    await call.message.answer("💬 Напиши свой вопрос или проблему следующим сообщением. Мы ответим сюда же, в этот чат.")
    await call.answer()

async def forward_to_admins(message: Message):
    if not ADMIN_IDS:
        await message.answer("Поддержка временно недоступна.")
        return
    header = (
        f"📩 Сообщение от {message.from_user.full_name} (@{message.from_user.username or 'без_ника'})\n"
        f"ID: {message.from_user.id}"
    )
    sent_any = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"{header}\n\n{message.text}\n\n↩️ Ответь на это сообщение, и я перешлю твой ответ юзеру.")
            sent_any = True
        except Exception:
            pass
    if sent_any:
        await message.answer("✅ Отправлено в поддержку. Ответ придёт сюда.")
    else:
        await message.answer("Не удалось отправить. Попробуй позже.")

@dp.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply(message: Message):
    source = message.reply_to_message
    if not source or not source.text: return
    match = re.search(r"ID:\s*(\d+)", source.text)
    if not match: return
    user_id = int(match.group(1))
    try:
        await bot.send_message(user_id, f"💬 <b>Ответ поддержки:</b>\n\n{message.text}", parse_mode="HTML")
        await message.answer("✅ Ответ доставлен.")
    except Exception as e:
        await message.answer(f"❌ Не удалось доставить: {e}")
@dp.callback_query(F.data == "show_stats")
async def show_stats_cb(call: CallbackQuery):
    await call.answer()
    await send_stats(call.message, call.from_user.id)

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    await send_stats(message, message.from_user.id)

async def send_stats(message: Message, user_id: int):
    user = await get_user(user_id)
    if not user:
        await message.answer("Сначала напиши /start")
        return
        
    stats = await get_user_stats(user_id)
    total = stats["total"]
    correct = stats["correct"]
    wrong = total - correct
    percent = round(correct / total * 100) if total else 0
    
    png_bytes = make_pie_chart(correct, wrong, percent)
    photo = BufferedInputFile(png_bytes, filename="stats.png")
    
    lines = []
    if total == 0:
        lines.append("Ты пока не решил ни одного вопроса. Жми /train! 🚀")
    else:
        weak = [t for t in stats["topics"] if t["total"] >= 1 and (t["correct"] / t["total"]) < 0.7]
        weak.sort(key=lambda t: t["correct"] / t["total"])
        if weak:
            lines.append("⚠️ <b>Слабые темы:</b>")
            for t in weak[:5]:
                tp = round(t["correct"] / t["total"] * 100)
                lines.append(f"• {t['topic']} — {tp}% ({t['correct']}/{t['total']})")
        else:
            lines.append("👍 Слабых тем нет. Так держать!")
            
    lines.append("")
    if user.role == "premium" and user.subscription_until:
        days_left = (user.subscription_until - datetime.utcnow()).days
        lines.append(f"💎 Premium, осталось дней: <b>{max(days_left, 0)}</b>")
    else:
        lines.append(f"🆓 Free, лимит {DAILY_LIMIT} вопросов в день")
        
    await message.answer_photo(photo, caption="\n".join(lines), parse_mode="HTML")

@dp.message(Command("train"))
async def cmd_train(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user.subject:
        await message.answer("Сначала выбери предмет командой /start")
        return
        
    allowed, count = await check_and_increment_daily(message.from_user.id, limit=DAILY_LIMIT)
    if not allowed:
        await show_subscription_offer(message, count)
        return
        
    referrer = await reward_referrer_if_pending(message.from_user.id)
    if referrer:
        try:
            await bot.send_message(referrer.id, f"🎉 Твой друг решил первый вопрос! Тебе начислено +{REFERRAL_REWARD_DAYS} дней Premium. Подписка активна до {referrer.subscription_until.strftime('%d.%m.%Y')}.")
        except Exception:
            pass
        await message.answer(f"🎁 Приятный бонус: твой друг пришёл по твоей ссылке — он получил +{REFERRAL_REWARD_DAYS} дней Premium!")

    q = await get_random_question(user.subject)
    if not q:
        await message.answer("Пока нет вопросов по этому предмету.")
        return
        
    text = (
        f"❓ {q.text}\n\n"
        f"A) {q.option_a}\n"
        f"B) {q.option_b}\n"
        f"C) {q.option_c}\n"
        f"D) {q.option_d}\n\n"
        f"<i>Вопрос {count}/{DAILY_LIMIT} на сегодня</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="A", callback_data=f"ans_{q.id}_A"), InlineKeyboardButton(text="B", callback_data=f"ans_{q.id}_B")],
        [InlineKeyboardButton(text="C", callback_data=f"ans_{q.id}_C"), InlineKeyboardButton(text="D", callback_data=f"ans_{q.id}_D")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

async def show_subscription_offer(message: Message, count: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Месяц — {PRICE_MONTH_STARS}⭐", callback_data="buy_month")],
        [InlineKeyboardButton(text=f"Год — {PRICE_YEAR_STARS}⭐ (−40%)", callback_data="buy_year")],
    ])
    await message.answer(f"🔒 На сегодня лимит исчерпан: {count}/{DAILY_LIMIT}.\n\nОформи подписку — и занимайся без ограничений:", reply_markup=kb)

@dp.callback_query(F.data == "show_subs")
async def show_subs_cb(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Месяц — {PRICE_MONTH_STARS}⭐", callback_data="buy_month")],
        [InlineKeyboardButton(text=f"Год — {PRICE_YEAR_STARS}⭐ (−40%)", callback_data="buy_year")],
    ])
    await call.message.answer("Выбери подписку — оплата в Telegram Stars:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "buy_month")
async def buy_month(call: CallbackQuery):
    await send_sub_invoice(call, days=30, stars=PRICE_MONTH_STARS, title="Подписка на месяц")

@dp.callback_query(F.data == "buy_year")
async def buy_year(call: CallbackQuery):
    await send_sub_invoice(call, days=365, stars=PRICE_YEAR_STARS, title="Подписка на год")

async def send_sub_invoice(call: CallbackQuery, days: int, stars: int, title: str):
    prices = [LabeledPrice(label=title, amount=stars)]
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=f"Безлимитный доступ к тренировкам на {days} дней",
        payload=f"sub_{days}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def on_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        days = int(payload.replace("sub_", ""))
    except ValueError:
        days = 30
        
    until = await activate_subscription(message.from_user.id, days)
    if until:
        await message.answer(f"✅ Оплата получена! Подписка активирована до {until.strftime('%d.%m.%Y')}.\nЗанимайся без ограничений 🚀")
    else:
        await message.answer("✅ Оплата получена, но пользователь не найден. Напиши в поддержку.")

@dp.message(Command("sub"))
async def cmd_sub(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Месяц — {PRICE_MONTH_STARS}⭐", callback_data="buy_month")],
        [InlineKeyboardButton(text=f"Год — {PRICE_YEAR_STARS}⭐ (−40%)", callback_data="buy_year")],
    ])
    await message.answer("Выбери подписку:", reply_markup=kb)

@dp.callback_query(F.data.startswith("ans_"))
async def check_answer(call: CallbackQuery):
    parts = call.data.split("_")
    qid = int(parts[1])
    chosen = parts[2]
    q = await get_question(qid)
    if not q:
        await call.answer("Вопрос не найден", show_alert=True)
        return
        
    is_correct = q.correct.upper() == chosen.upper()
    await log_answer(call.from_user.id, q.id, q.topic, is_correct)
    
    head = "✅ Правильно!" if is_correct else f"❌ Неправильно. Верный ответ: {q.correct}"
    body = f"\n\n💡 Разбор: {q.explanation}" if q.explanation else ""
    await call.message.edit_text(f"{head}{body}")
    await call.answer()

@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    await seed_questions(force=True)
    total = await count_questions()
    await message.answer(f"✅ Вопросы перезагружены. Всего в базе: {total}")

@dp.message(Command("users_count"))
async def cmd_users_count(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    total = await count_users()
    await message.answer(f"👥 Пользователей в базе: {total}")

@dp.message(Command("stats_global"))
async def cmd_stats_global(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    users = await count_users()
    questions = await count_questions()
    await message.answer(f"🌍 <b>Глобальная статистика</b>\n\n👥 Юзеров: {users}\n❓ Вопросов: {questions}", parse_mode="HTML")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /broadcast текст сообщения")
        return
        
    text = parts[1].strip()
    user_ids = await get_all_user_ids()
    await message.answer(f"📤 Начинаю рассылку на {len(user_ids)} юзеров…")
    
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
        
    await message.answer(f"✅ Рассылка завершена.\nДоставлено: {sent}\nОшибок: {failed}")

@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /reply <user_id> <текст>\nПример: /reply 6895511324 Привет!")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("❌ user_id должен быть числом.")
        return
        
    text = parts[2].strip()
    if not text:
        await message.answer("❌ Текст пуст.")
        return
        
    try:
        await bot.send_message(user_id, f"💬 <b>Ответ поддержки:</b>\n\n{text}", parse_mode="HTML")
        await message.answer(f"✅ Ответ доставлен юзеру {user_id}.")
    except Exception as e:
        await message.answer(f"❌ Не удалось доставить: {e}")

@dp.message(F.text, ~F.text.startswith("/"))
async def fallback_text(message: Message):
    if message.from_user.id in ADMIN_IDS and message.reply_to_message:
        return
    if message.from_user.id not in ADMIN_IDS:
        await forward_to_admins(message)
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
