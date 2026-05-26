from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TGUser
from database.models import User
from database.engine import async_session
from sqlalchemy import select
import logging

logger = logging.getLogger(__name__)

class UserUpdateMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        tg_user: TGUser = data.get("event_from_user")

        if tg_user and not tg_user.is_bot:
            try:
                async with async_session() as session:
                    user_id = tg_user.id
                    full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or None
                    username = tg_user.username or None

                    stmt = select(User).where(User.id == user_id)
                    result = await session.execute(stmt)
                    user = result.scalar_one_or_none()

                    referral_id = None
                    if not user or (not user.referred_by):
                        from aiogram.types import Update, Message
                        msg = None
                        if isinstance(event, Message):
                            msg = event
                        elif isinstance(event, Update) and event.message:
                            msg = event.message

                        if msg and msg.text and msg.text.startswith('/start') and len(msg.text.split()) > 1:
                            start_param = msg.text.split()[1]
                            if start_param.startswith("REF"):
                                try: referral_id = int(start_param.replace("REF", ""))
                                except: pass
                            else:
                                try: referral_id = int(start_param)
                                except: pass

                    is_new_join = False

                    if not user:
                        user = User(
                            id=user_id,
                            full_name=full_name,
                            username=username,
                            is_active_sourcing=True,
                            referred_by=referral_id if (referral_id and referral_id != user_id) else None,
                            referral_bonus_awarded=False
                        )
                        session.add(user)
                        is_new_join = True
                        logger.info(f"Middleware: Created new sourcing user {user_id} with referral_by={user.referred_by}")
                    else:
                        changed = False

                        if not user.referred_by and referral_id and referral_id != user_id:
                            user.referred_by = referral_id
                            changed = True
                            logger.info(f"Middleware: Updated referral for user {user_id} to {referral_id}")

                        if not user.is_active_sourcing:
                            user.is_active_sourcing = True
                            changed = True
                            is_new_join = True

                        if user.full_name != full_name:
                            user.full_name = full_name
                            changed = True
                        if user.username != username:
                            user.username = username
                            changed = True

                        if changed:
                            logger.info(f"Middleware: Updated info for user {user_id}")

                    await session.commit()

                    if is_new_join:
                        bot = data.get("bot")
                        if bot:
                            await self._send_join_log(bot, tg_user)

            except Exception as e:
                logger.error(f"Error in UserUpdateMiddleware: {e}")

        return await handler(event, data)

    async def _send_join_log(self, bot, tg_user: TGUser):
        """Send a join notification to the configured sourcing join log channel."""
        try:
            from database.models import AppSetting
            async with async_session() as session:
                obj = (await session.execute(
                    select(AppSetting).where(AppSetting.key == "sourcing_join_log_channel_id")
                )).scalar_one_or_none()
                if not obj or not obj.value or not obj.value.strip():
                    return
                channel_id_raw = obj.value.strip()

            if channel_id_raw.lstrip("-").isdigit():
                channel_id = int(channel_id_raw)
            else:
                channel_id = channel_id_raw

            full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "—"
            username_line = f"@{tg_user.username}" if tg_user.username else "—"

            text = (
                f"🔔 <b>New Member Joined!</b>\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
                f"👤  <b>{full_name}</b>\n\n"
                f"🏷️  <b>{username_line}</b>\n\n"
                f"🆔  <b>{tg_user.id}</b>\n\n"
                f"🤖  <b>SKELETON TG SELL</b>\n\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
            )

            await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
            logger.info(f"Middleware: Join log sent for user {tg_user.id} to channel {channel_id}")
        except Exception as e:
            logger.error(f"Middleware: Failed to send join log for user {tg_user.id}: {e}")
