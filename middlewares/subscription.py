from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton
from database.engine import async_session
from database.models import SubscriptionChannel
from sqlalchemy import select
from config import ADMIN_IDS
import logging

logger = logging.getLogger(__name__)

class SubscriptionMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        try:
            user_id = None
            target = None
            
            if isinstance(event, Message):
                user_id = event.from_user.id
                target = event
            elif isinstance(event, CallbackQuery):
                user_id = event.from_user.id
                target = event
            elif isinstance(event, Update):
                if event.message:
                    user_id = event.message.from_user.id
                    target = event.message
                elif event.callback_query:
                    user_id = event.callback_query.from_user.id
                    target = event.callback_query
            
            if not user_id or not target:
                return await handler(event, data)

            bot: Bot = data.get("bot")
            
            # Admins bypass subscription check
            import config
            if user_id in config.SOURCING_ADMIN_IDS:
                return await handler(event, data)

            async with async_session() as session:
                result = await session.execute(
                    select(SubscriptionChannel).where(SubscriptionChannel.bot_type == "sourcing")
                )
                channels = result.scalars().all()

            if not channels:
                return await handler(event, data)

            not_subscribed = []
            for channel in channels:
                try:
                    chat_id = channel.username
                    member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                    if member.status in ["left", "kicked"]:
                        not_subscribed.append(channel)
                except Exception as e:
                    logger.error(f"Error checking sub for {chat_id}: {e}")
                    continue

            if not_subscribed:
                buttons = []
                for ch in not_subscribed:
                    link = ch.link if ch.link.startswith("http") else f"https://t.me/{ch.username.replace('@','')}"
                    buttons.append([InlineKeyboardButton(text="Join Channel", url=link)])
                
                kb = InlineKeyboardMarkup(inline_keyboard=buttons)
                
                msg = (
                    "🔒 <b>Subscription Required</b>\n\n"
                    "Sorry, you must join our channel first to use the bot:\n\n"
                    "✅ <b>After joining, send /start</b>"
                )
                
                if isinstance(target, Message):
                    await target.answer(msg, reply_markup=kb, parse_mode="HTML")
                elif isinstance(target, CallbackQuery):
                    await target.message.answer(msg, reply_markup=kb, parse_mode="HTML")
                    await target.answer()
                
                return # Block further processing

            return await handler(event, data)
        except Exception as e:
            logger.error(f"SubscriptionMiddleware Error: {e}")
            return await handler(event, data)
