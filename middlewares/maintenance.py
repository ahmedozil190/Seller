from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton
from database.engine import async_session
from database.models import AppSetting
from sqlalchemy import select
from config import ADMIN_IDS
import logging

logger = logging.getLogger(__name__)

class MaintenanceMiddleware(BaseMiddleware):
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
            if isinstance(event, Message):
                user_id = event.from_user.id
            elif isinstance(event, CallbackQuery):
                user_id = event.from_user.id
            elif isinstance(event, Update):
                if event.message:
                    user_id = event.message.from_user.id
                elif event.callback_query:
                    user_id = event.callback_query.from_user.id
                elif event.inline_query:
                    user_id = event.inline_query.from_user.id

            import config
            is_admin = user_id and user_id in config.SOURCING_ADMIN_IDS
            
            async with async_session() as session:
                stmt = select(AppSetting).where(AppSetting.key == "SOURCING_UNDER_MAINTENANCE")
                result = await session.execute(stmt)
                setting = result.scalar_one_or_none()
                is_maintenance = setting and str(setting.value).lower() == "true"
                
                ch_link = None
                if is_maintenance and not is_admin:
                    ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
                    ch_link = ch_obj.value if ch_obj else None

            if is_maintenance and not is_admin:
                target = None
                if isinstance(event, Message): target = event
                elif isinstance(event, CallbackQuery): target = event
                elif isinstance(event, Update):
                    if event.message: target = event.message
                    elif event.callback_query: target = event.callback_query

                if target:
                    markup = None
                    if ch_link:
                        markup = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Updates Channel 📢", url=ch_link)]
                        ])

                    if isinstance(target, Message):
                        await target.answer("<b>⚠️ The bot is currently under maintenance.</b>", parse_mode="HTML", reply_markup=markup)
                    elif isinstance(target, CallbackQuery):
                        await target.answer("Maintenance Mode ⚠️", show_alert=True)
                    return # STOP HERE

            return await handler(event, data)
        except Exception as e:
            logger.error(f"MaintenanceMiddleware Error: {e}")
            return await handler(event, data)
