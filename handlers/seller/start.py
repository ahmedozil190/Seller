from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, MenuButtonWebApp
from config import SELLER_URL
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from database.engine import async_session
from database.models import User, CountryPrice
from sqlalchemy.future import select

router = Router()

@router.message(Command("ping"))
async def seller_ping(message: Message):
    await message.answer("Sourcing Bot is Ready! 🚀")

@router.message(F.text.in_({"عربي", "English"}))
async def seller_change_language(message: Message):
    lang_code = "ar" if message.text == "عربي" else "en"
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == message.from_user.id))).scalar_one_or_none()
        if user:
            user.language = lang_code
            await session.commit()
    
    # Just show the start message without any confirmation text
    await seller_start_cmd(message)

@router.message(Command("start"))
async def seller_start_cmd(message: Message, bot: Bot = None):
    # Force refresh commands if bot is provided (during manual /start)
    if bot:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=message.from_user.id))
            # Set the "Open Panel" menu button
            await bot.set_chat_menu_button(
                chat_id=message.from_user.id,
                menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=SELLER_URL))
            )
        except:
            pass

    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == message.from_user.id))).scalar_one_or_none()
        if not user:
            user = User(id=message.from_user.id, language="en", is_active_sourcing=True)
            session.add(user)
            await session.commit()
        
        if user.is_banned_sourcing:
            from database.models import AppSetting
            support_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            support_username = support_obj.value if support_obj else None
            
            markup = None
            if support_username:
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Contact Support 🎧", url=f"https://t.me/{support_username}")]
                ])
            
            await message.answer("<b>🚫 Your account has been suspended.</b>", parse_mode="HTML", reply_markup=markup)
            return
        
    welcome_text = "Welcome to the Panel! 🚀\nClick the button below to open."
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=SELLER_URL))]
    ])
    
    await message.answer(welcome_text, reply_markup=markup)

