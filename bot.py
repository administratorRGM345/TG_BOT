import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ================= НАЛАШТУВАННЯ =================
load_dotenv()  # читає змінні з файлу .env поруч зі скриптом

TOKEN = os.getenv("BOT_TOKEN")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# Перевірка, що всі обов'язкові значення підвантажились з .env
_required = {
    "BOT_TOKEN": TOKEN,
    "APPS_SCRIPT_URL": APPS_SCRIPT_URL,
    "SECRET_KEY": SECRET_KEY,
    "ADMIN_PASSWORD": ADMIN_PASSWORD,
}
_missing = [name for name, value in _required.items() if not value]
if _missing:
    sys.exit(
        f"❌ Відсутні змінні середовища: {', '.join(_missing)}. "
        f"Перевірте файл .env поруч зі скриптом."
    )

TIMEZONE = timezone(timedelta(hours=3))
# ===============================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# ================= ТЕКСТИ КНОПОК (Reply Keyboard) =================
BTN_ROLE_USER = "👤 Я Користувач"
BTN_ROLE_ADMIN = "👨‍💻 Я Адмін (Оператор)"
BTN_NEW_TICKET = "➕ Створити новий запит"
BTN_TICKET_LIST = "📋 Список запитів"
BTN_MAIN_MENU = "🔙 Головне меню"


# ================= FSM СТАНИ =================
class AdminAuth(StatesGroup):
    waiting_password = State()


class TicketForm(StatesGroup):
    full_name = State()
    subject = State()
    category = State()
    priority = State()
    description = State()
    photo = State()


# ================= ЗАПИТИ ДО APPS SCRIPT =================
async def call_apps_script(action: str, data: dict) -> dict:
    payload = {"secret": SECRET_KEY, "action": action, "data": data}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(APPS_SCRIPT_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    logging.error(f"Неочікувана відповідь Apps Script: {text}")
                    return {"ok": False, "error": "bad_response"}
    except Exception as e:
        logging.error(f"Помилка запиту до Apps Script ({action}): {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


async def save_ticket_remote(ticket_data: dict) -> dict:
    return await call_apps_script("save_ticket", ticket_data)


async def register_operator_remote(chat_id: str, user_name: str) -> dict:
    return await call_apps_script("register_operator", {"chat_id": chat_id, "user_name": user_name})


async def get_operators_remote() -> list[str]:
    result = await call_apps_script("get_operators", {})
    if result.get("ok"):
        return result.get("operators", [])
    return []


async def get_open_tickets_remote() -> list[dict]:
    result = await call_apps_script("get_open_tickets", {})
    if result.get("ok"):
        return result.get("tickets", [])
    return []


async def update_ticket_status_remote(ticket_id: str, date_sheet_name: str, status: str) -> dict:
    return await call_apps_script(
        "update_ticket_status",
        {"ticket_id": ticket_id, "date_sheet_name": date_sheet_name, "status": status},
    )


async def broadcast_ticket(ticket_data: dict, ticket_id: str, date_sheet_name: str, time_string: str = ""):
    recipients = await get_operators_remote()
    if not recipients:
        logging.warning("Немає зареєстрованих операторів для розсилки.")
        return

    photo_url = ticket_data.get("photo_url")
    photo_text = (
        f'🖼 <b>Фото:</b> <a href="{photo_url}">Переглянути</a>'
        if photo_url else "🖼 <b>Фото:</b> Відсутнє"
    )

    date_time_line = f"{date_sheet_name} {time_string}".strip()

    message = (
        f"🚨 <b>НОВИЙ ЗАПИТ:</b> <code>{ticket_id}</code>\n\n"
        f"📅 <b>Дата/час:</b> {date_time_line}\n"
        f"👤 <b>Контакт:</b> {ticket_data.get('full_name')}\n"
        f"📌 <b>Тема:</b> {ticket_data.get('subject')}\n"
        f"🏷 <b>Категорія:</b> {ticket_data.get('category')}\n"
        f"⚡ <b>Пріоритет:</b> {ticket_data.get('priority')}\n\n"
        f"📝 <b>Опис:</b>\n{ticket_data.get('description')}\n\n{photo_text}"
    )

    for chat_id in recipients:
        try:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Помилка відправки адміну {chat_id}: {e}")


# ================= КЛАВІАТУРИ (Reply) =================
def role_keyboard() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_ROLE_USER)
    b.button(text=BTN_ROLE_ADMIN)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def user_menu_keyboard() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_NEW_TICKET)
    b.button(text=BTN_MAIN_MENU)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_TICKET_LIST)
    b.button(text=BTN_MAIN_MENU)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


# ================= КЛАВІАТУРИ (Inline, для кроків форми та дій) =================
def category_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cat in ["Програми", "ПК", "Доступи / Акаунти", "Інтернет", "Інше"]:
        b.button(text=cat, callback_data=f"cat:{cat}")
    b.adjust(1)
    return b.as_markup()


def priority_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🟢 Низький (не треміново)", callback_data="prio:Низький")
    b.button(text="🟡 Звичайний (треба сьогодні)", callback_data="prio:Звичайний")
    b.button(text="🔴 Високий (терміново)", callback_data="prio:Високий")
    b.adjust(1)
    return b.as_markup()


def skip_photo_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Пропустити (без фото)", callback_data="skip_photo")
    return b.as_markup()


def ticket_done_keyboard(ticket_id: str, date_sheet_name: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Позначити виконаним", callback_data=f"done:{date_sheet_name}:{ticket_id}")
    return b.as_markup()


# ================= /start та /cancel =================
@router.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Вітаємо, <b>{message.from_user.first_name}</b>! 👋\n\n"
        f"Оберіть вашу роль для роботи з ботом:",
        parse_mode="HTML",
        reply_markup=role_keyboard(),
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Дію скасовано. Введіть /start, щоб почати знову.", reply_markup=role_keyboard())


@router.message(F.text == BTN_MAIN_MENU)
async def main_menu_button(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оберіть вашу роль для роботи з ботом:", reply_markup=role_keyboard())


# ================= ВИБІР РОЛІ =================
@router.message(F.text == BTN_ROLE_USER)
async def role_user_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Ви увійшли як Користувач.</b>\nНатисніть кнопку нижче:",
        parse_mode="HTML",
        reply_markup=user_menu_keyboard(),
    )


@router.message(F.text == BTN_ROLE_ADMIN)
async def role_admin_handler(message: Message, state: FSMContext):
    await state.set_state(AdminAuth.waiting_password)
    await message.answer("🔐 Введіть пароль адміністратора:")


@router.message(AdminAuth.waiting_password)
async def admin_password_check(message: Message, state: FSMContext):
    if message.text and message.text.strip() == ADMIN_PASSWORD:
        result = await register_operator_remote(str(message.chat.id), message.from_user.first_name or "Адмін")
        await state.clear()
        if result.get("ok"):
            await message.answer(
                "✅ <b>Успішна авторизація!</b>\nСюди будуть надходити сповіщення про нові запити.",
                parse_mode="HTML",
                reply_markup=admin_menu_keyboard(),
            )
        else:
            await message.answer(
                "⚠️ Авторизація пройшла, але не вдалося зареєструвати вас у таблиці. Повідомте розробника.",
                reply_markup=admin_menu_keyboard(),
            )
    else:
        await message.answer("❌ <b>Невірний пароль.</b> Спробуйте ще раз або введіть /cancel", parse_mode="HTML")


# ================= СПИСОК ЗАПИТІВ (АДМІН) =================
@router.message(F.text == BTN_TICKET_LIST)
async def show_ticket_list(message: Message, state: FSMContext):
    await message.answer("⏳ Завантажуємо список запитів...")
    tickets = await get_open_tickets_remote()

    if not tickets:
        await message.answer("✅ Наразі немає невиконаних запитів.", reply_markup=admin_menu_keyboard())
        return

    for t in tickets:
        ticket_id = t.get("ticket_id", "")
        date_sheet_name = t.get("date_sheet_name", "")
        photo_url = t.get("photo_url")
        photo_text = (
            f'🖼 <b>Фото:</b> <a href="{photo_url}">Переглянути</a>'
            if photo_url else "🖼 <b>Фото:</b> Відсутнє"
        )
        date_time_line = f"{date_sheet_name} {t.get('time', '')}".strip() #    date_time_line = f"{date_sheet_name} {time_string}".strip()


        text = (
            f"🎫 <b>Запит:</b> <code>{ticket_id}</code>\n"
            f"📅 <b>Дата/час:</b> {date_time_line}\n"
            f"👤 <b>Контакт:</b> {t.get('full_name')}\n"
            f"📌 <b>Тема:</b> {t.get('subject')}\n"
            f"🏷 <b>Категорія:</b> {t.get('category')}\n"
            f"⚡ <b>Пріоритет:</b> {t.get('priority')}\n"
            f"📊 <b>Статус:</b> {t.get('status')}\n\n"
            f"📝 <b>Опис:</b>\n{t.get('description')}\n\n{photo_text}"
        )
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=ticket_done_keyboard(ticket_id, date_sheet_name),
        )

    await message.answer("👆 Це всі активні запити.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("done:"))
async def mark_ticket_done(callback: CallbackQuery, state: FSMContext):
    try:
        _, date_sheet_name, ticket_id = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Помилка обробки запиту", show_alert=True)
        return

    result = await update_ticket_status_remote(ticket_id, date_sheet_name, "Виконано")

    if not result.get("ok"):
        await callback.answer("⚠️ Не вдалося оновити статус запиту.", show_alert=True)
        logging.error(f"Помилка оновлення статусу запиту {ticket_id}: {result.get('error')}")
        return

    completed_at = datetime.now(TIMEZONE).strftime("%H:%M:%S")

    # Прибираємо кнопку і позначаємо повідомлення як виконане
    try:
        new_text = (callback.message.html_text or callback.message.text or "") + f"\n\n✅ <b>ВИКОНАНО о {completed_at}</b>"
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception:
        pass

    await callback.answer("✅ Запит позначено виконаним")

    user_chat_id = result.get("user_chat_id")
    if user_chat_id:
        try:
            await bot.send_message(
                chat_id=user_chat_id,
                text=(
                    f"✅ <b>Ваш запит виконано!</b>\n\n"
                    f"🆔 ID: <code>{ticket_id}</code>\n"
                    f"📌 Тема: {result.get('subject', '')}\n"
                    f"🕒 Час виконання: {completed_at}\n\n"
                    f"Якщо проблема повторюється — створіть новий запит."
                ),
                parse_mode="HTML",
                reply_markup=user_menu_keyboard(),
            )
        except Exception as e:
            logging.error(f"Не вдалося надіслати сповіщення користувачу {user_chat_id}: {e}")


# ================= СТВОРЕННЯ ЗАПИТУ =================
@router.message(F.text == BTN_NEW_TICKET)
async def create_ticket_start(message: Message, state: FSMContext):
    await state.set_state(TicketForm.full_name)
    await message.answer(
        "<b>Крок 1/6:</b> Введіть ваше Ім'я та Прізвище:",
        parse_mode="HTML",
    )


@router.message(TicketForm.full_name)
async def ticket_full_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text, user_chat_id=str(message.chat.id))
    await state.set_state(TicketForm.subject)
    await message.answer("<b>Крок 2/6:</b> Введіть Тему вашого звернення (проблеми):", parse_mode="HTML")


@router.message(TicketForm.subject)
async def ticket_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text)
    await state.set_state(TicketForm.category)
    await message.answer("<b>Крок 3/6:</b> Оберіть Категорію:", parse_mode="HTML", reply_markup=category_keyboard())


@router.callback_query(TicketForm.category, F.data.startswith("cat:"))
async def ticket_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("cat:", 1)[1]
    await state.update_data(category=category)
    await state.set_state(TicketForm.priority)
    await callback.message.answer("<b>Крок 4/6:</b> Оберіть Пріоритет:", parse_mode="HTML", reply_markup=priority_keyboard())
    await callback.answer()


@router.callback_query(TicketForm.priority, F.data.startswith("prio:"))
async def ticket_priority(callback: CallbackQuery, state: FSMContext):
    priority = callback.data.split("prio:", 1)[1]
    await state.update_data(priority=priority)
    await state.set_state(TicketForm.description)
    await callback.message.answer("<b>Крок 5/6:</b> Напишіть детальний Опис проблеми:", parse_mode="HTML")
    await callback.answer()


@router.message(TicketForm.description)
async def ticket_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(TicketForm.photo)
    await message.answer(
        "<b>Крок 6/6:</b> Надішліть Фото/Скріншот або натисніть пропустити:",
        parse_mode="HTML",
        reply_markup=skip_photo_keyboard(),
    )


@router.callback_query(TicketForm.photo, F.data == "skip_photo")
async def ticket_skip_photo(callback: CallbackQuery, state: FSMContext):
    await state.update_data(photo_url="")
    await finalize_ticket(callback.message, state)
    await callback.answer()


@router.message(TicketForm.photo, F.photo)
async def ticket_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    # Формуємо повне посилання на файл, а не сирий file_path
    photo_url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
    await state.update_data(photo_url=photo_url)
    await finalize_ticket(message, state)


@router.message(TicketForm.photo)
async def ticket_photo_invalid(message: Message):
    await message.answer("❌ Будь ласка, надішліть фото, або натисніть кнопку 'Пропустити'.")


async def finalize_ticket(message: Message, state: FSMContext):
    await message.answer("⏳ Зберігаємо запит...")
    ticket_data = await state.get_data()
    result = await save_ticket_remote(ticket_data)

    if result.get("ok"):
        ticket_id = result["ticket_id"]
        date_sheet_name = result["date_sheet_name"]
        time_string = result.get("time_string", "")
        await broadcast_ticket(ticket_data, ticket_id, date_sheet_name, time_string)
        await message.answer(
            f"✅ <b>Запит успішно створено!</b>\n"
            f"🆔 Ваш ID: <code>{ticket_id}</code>\n"
            f"🕒 Час створення: {time_string}\n\n"
            f"Ви можете створити ще один запит, якщо потрібно:",
            parse_mode="HTML",
            reply_markup=user_menu_keyboard(),
        )
    else:
        logging.error(f"Помилка збереження Запиту: {result.get('error')}")
        await message.answer(
            "⚠️ Сталася помилка при збереженні Запиту. Спробуйте ще раз пізніше або зверніться до адміна.",
            reply_markup=user_menu_keyboard(),
        )

    await state.clear()


# ================= ЗАПУСК =================
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("🚀 Бот запустився (aiogram 3, Apps Script webhook)!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())