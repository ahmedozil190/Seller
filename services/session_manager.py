import re
import random
import logging
import asyncio
from pyrogram import Client, errors
from pyrogram.raw import functions, types
from typing import Dict

from config import API_ID, API_HASH
from services.i18n import get_text
from database.engine import async_session
from database.models import User

# We store temporary clients here during the sign-in flow
login_clients: Dict[int, Client] = {}

async def create_client(session_string: str = None) -> Client:
    # Use professional identity strings to avoid automated session bans
    identity = {
        "device_model": "Samsung SM-S918B", # Galaxy S23 Ultra
        "system_version": "Android 14", # Modern Android version
        "app_version": "10.14.5", # Recent Telegram version
        "lang_code": "en" # Forces Telegram messages/OTPs to English
    }
    if session_string:
        client = Client(name="temp", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True, **identity)
    else:
        client = Client(name="temp", api_id=API_ID, api_hash=API_HASH, in_memory=True, **identity)
    return client

async def request_app_code(user_id: int, phone_number: str) -> str:
    """Returns phone_code_hash"""
    client = await create_client()
    await client.connect()
    await asyncio.sleep(1.5) # Human-like delay after connection
    
    try:
        sent_code = await client.send_code(phone_number)
        login_clients[user_id] = client # Store client so we can complete sign_in later
        return sent_code.phone_code_hash
    except errors.PhoneNumberBanned:
        async with async_session() as session:
            user = await session.get(User, user_id)
            lang = user.language if user else "ar"
        raise Exception(get_text("banned_phone", lang))
    except errors.UserDeactivated:
        async with async_session() as session:
            user = await session.get(User, user_id)
            lang = user.language if user else "ar"
        raise Exception(get_text("frozen_company", lang))
    except Exception as e:
        if client.is_connected:
            await client.disconnect()
        raise e

async def submit_app_code(user_id: int, phone_number: str, phone_code_hash: str, phone_code: str) -> dict | None:
    """Returns dict with session_string, two_fa_password, has_other_sessions if successful"""
    client = login_clients.get(user_id)
    if not client:
        logging.error(f"Submit OTP Failed: No active client found in memory for user {user_id}. This usually means the server restarted or hit a different Railway instance.")
        return None
        
    try:
        # Human-like delay before sign-in (simulate typing OTP)
        await asyncio.sleep(random.uniform(2.5, 5.5)) 
        await client.sign_in(phone_number, phone_code_hash, phone_code)
        
        temp_session = await client.export_session_string()
        try: await client.disconnect()
        except: pass

        is_alive, reject_reason = await is_session_alive(temp_session)
        
        if not is_alive:
            try:
                temp_client = await create_client(temp_session)
                await temp_client.connect()
                await temp_client.log_out()
            except: pass
            raise Exception(reject_reason)
            
        # Re-create client with the session string to guarantee proper authorization state for 2FA
        client = await create_client(temp_session)
        await client.connect()
        session_string = temp_session

        # 4. Generate & Enable 2FA
        import string
        two_fa_password = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        try:
            await client.enable_cloud_password(two_fa_password)
            logging.info(f"Enabled 2FA for {phone_number}: {two_fa_password}")
        except Exception as e:
            logging.warning(f"Failed to enable 2FA for {phone_number} (might already have one): {e}")
            two_fa_password = None # Mark as none if failed
            
        # 5. Check active sessions
        has_other_sessions = False
        try:
            from pyrogram.raw.functions.account import GetAuthorizations
            result = await client.invoke(GetAuthorizations())
            authorizations = result.authorizations
            # More than 1 means there's a session other than ours
            if len(authorizations) > 1:
                has_other_sessions = True
                logging.info(f"Account {phone_number} has {len(authorizations)-1} other active session(s). Requires 24h wait.")
        except Exception as e:
            logging.warning(f"Failed to fetch authorizations for {phone_number}: {e}")
            
        return {
            "session_string": session_string,
            "two_fa_password": two_fa_password,
            "has_other_sessions": has_other_sessions
        }
    except Exception as e:
        # Final pass-through for anticipated exceptions
        err_msg = str(e).lower()
        if any(msg in err_msg for msg in ["restricted", "spam-blocked", "frozen", "security check", "responding", "deactivated"]):
            raise e
        raise e
    finally:
        try:
            if client and client.is_connected:
                await client.disconnect()
        except:
            pass # Client might already be terminated by log_out()
        login_clients.pop(user_id, None)

async def get_telegram_login_code(session_string: str, after_ts: float = None) -> str | None:
    import time
    client = await create_client(session_string)
    code = None
    now = time.time()
    
    try:
        await client.connect()
        async for message in client.get_chat_history(777000, limit=5):
            msg_ts = message.date.timestamp() if message.date else 0
            
            # 1. Use after_ts if provided (Purchase time)
            if after_ts and msg_ts < after_ts:
                continue
                
            # 2. Fallback to 120s window if no after_ts
            if not after_ts and (now - msg_ts) > 120:
                continue

            text = message.text
            if not text:
                continue
            
            # The code is usually a 5-digit number, often with "Login code:" context
            match = re.search(r'\b(\d{5})\b', text)
            if match:
                code = match.group(1)
                break
    except (errors.AuthKeyInvalid, errors.AuthKeyUnregistered, errors.UserDeactivated, errors.SessionRevoked, errors.SessionExpired):
        raise Exception("SESSION_REVOKED")
    except Exception as e:
        logging.error(f"Error fetching code for session: {e}")
        raise e
    finally:
        if client.is_connected:
            await client.disconnect()
            pass
    return code

async def perform_full_wipe(client: Client):
    """Deletes all chats and clears history for Saved Messages."""
    try:
        # 1. Clear Saved Messages (me) explicitly
        try:
            from pyrogram.raw import functions
            peer = await client.resolve_peer("me")
            await client.invoke(functions.messages.DeleteHistory(
                peer=peer, max_id=0, just_clear=True, revoke=False
            ))
        except Exception: pass



        # 3. Iterate through all dialogs and delete them aggressively
        from pyrogram.raw import functions
        async for dialog in client.get_dialogs():
            if dialog.chat.id == 777000: # Skip Telegram Official
                continue
            try:
                peer = await client.resolve_peer(dialog.chat.id)
                # Delete history for both sides and remove from list
                await client.invoke(functions.messages.DeleteHistory(
                    peer=peer, max_id=0, just_clear=False, revoke=True
                ))
            except Exception:
                try:
                    # Fallback for groups/channels if DeleteHistory fails
                    await client.delete_chat(dialog.chat.id)
                except Exception: pass
        
        logging.info("Full Wipe completed successfully (including SpamBot and all dialogs).")
    except Exception as e:
        logging.error(f"Full Wipe error: {e}")

async def clean_account_for_buyer(session_string: str, two_fa: str = None):
    try:
        client = await create_client(session_string)
        await client.connect()
        try:
            # Terminate all other active sessions so the buyer is alone
            await client.invoke(functions.auth.ResetAuthorizations())
        except errors.FreshResetAuthorisationForbidden:
            logging.info("ResetAuthorizations failed (Fresh Session). Attempting individual termination...")
            try:
                # Fallback: Try individual termination (sometimes works if ResetAuth is blocked)
                auths = await client.invoke(functions.account.GetAuthorizations())
                for auth in auths.authorizations:
                    if auth.hash != 0: # 0 is the current session
                        try:
                            await client.invoke(functions.account.TerminateAuthorization(hash=auth.hash))
                        except Exception: pass
            except Exception: pass
        except Exception as e:
            logging.error(f"Failed to reset authorizations during cleaning: {e}")
            
        try:
            # Remove the 2FA password so the buyer doesn't need it
            if two_fa and two_fa.strip():
                await client.remove_cloud_password(two_fa)
            else:
                try:
                    await client.remove_cloud_password()
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Failed to remove 2FA during cleaning: {e}")
            
        try:
            # Perform the aggressive full wipe
            await perform_full_wipe(client)
        except Exception as e:
            logging.warning(f"Full Wipe during handover failed: {e}")
            
        if client.is_connected:
            await client.disconnect()
    except Exception as e:
        logging.error(f"clean_account_for_buyer total error: {e}")

async def logout_bot_session(session_string: str, delay: int = 600):
    """Monitors for a new session (buyer) and logs out as soon as they enter, or after a timeout."""
    if not session_string: return
    try:
        client = await create_client(session_string)
        await client.connect()
        
        # Get initial count of sessions
        try:
            initial_auths = await client.invoke(functions.account.GetAuthorizations())
            initial_count = len(initial_auths.authorizations)
        except:
            initial_count = 1
            
        start_time = asyncio.get_event_loop().time()
        
        # Check every 5 seconds if a new session (the buyer) has joined
        while (asyncio.get_event_loop().time() - start_time) < delay:
            await asyncio.sleep(5)
            try:
                current_auths = await client.invoke(functions.account.GetAuthorizations())
                if len(current_auths.authorizations) > initial_count:
                    logging.info("Buyer detected! Logging out bot session to leave them alone.")
                    break
            except Exception:
                # If session is already revoked or error, just stop
                return

        # This permanently kills the bot's session on the Telegram server
        # It does NOT affect the buyer's session.
        await client.log_out() 
        logging.info(f"Bot session successfully terminated.")
    except Exception as e:
        logging.error(f"Error during bot logout monitoring: {e}")

async def is_session_alive(session_string: str) -> tuple[bool, str]:
    try:
        client = await create_client(session_string)
        await client.connect()
        me = await client.get_me()
        if not me or me.is_scam or me.is_fake or me.is_restricted:
            logging.info(f"[AliveCheck] FAIL — API flags: scam={getattr(me,'is_scam',None)} fake={getattr(me,'is_fake',None)} restricted={getattr(me,'is_restricted',None)}")
            return False, "frozen" # Use key for better i18n mapping
        
        logging.info(f"[AliveCheck] API check passed for {getattr(me, 'phone_number', '?')}")

        # STRICT PHYSICAL CHECK: Try sending to Saved Messages (fails for frozen accounts)
        try:
            test_msg = await client.send_message("me", "✅")
            await test_msg.delete()
            logging.info("[AliveCheck] Saved Messages check PASSED.")
        except Exception as e:
            err_type = type(e).__name__
            logging.warning(f"[AliveCheck] Saved Messages check FAILED: {err_type} — {e}")
            return False, "frozen"

        # SPAM CHECK: Read SpamBot reply to detect spam-restricted accounts
        try:
            import time
            start_time = time.time()
            logging.info(f"[AliveCheck] Starting SpamBot check for {getattr(me, 'phone_number', '?')}...")
            await client.send_message("SpamBot", "/start")
            
            spambot_replied = False
            for i in range(15):
                await asyncio.sleep(0.5)
                async for msg in client.get_chat_history("SpamBot", limit=3):
                    if msg.from_user and msg.from_user.id == 178220800 and msg.date.timestamp() > (start_time - 2):
                        spambot_replied = True
                        
                        # --- UNIVERSAL BUTTON-COUNT LOGIC (Inline + Reply Keyboard) ---
                        btn_count = 0
                        btn_texts = []
                        markup = getattr(msg, "reply_markup", None)
                        if markup:
                            # Check for Inline Buttons (attached to message)
                            if hasattr(markup, "inline_keyboard"):
                                for row in markup.inline_keyboard:
                                    for btn in row:
                                        btn_count += 1
                                        btn_texts.append(getattr(btn, "text", str(btn)))
                            
                            # Check for Keyboard Buttons (bottom keyboard)
                            if hasattr(markup, "keyboard"):
                                for row in markup.keyboard:
                                    for btn in row:
                                        btn_count += 1
                                        btn_texts.append(getattr(btn, "text", str(btn)))
                        
                        logging.info(f"[AliveCheck] SpamBot Result for {getattr(me, 'phone_number', '?')}: Buttons={btn_count} | Labels={btn_texts}")

                        if btn_count >= 3:
                            logging.info(f"[AliveCheck] Result: REJECTED (Reason: {btn_count} buttons detected)")
                            return False, "spam"
                        
                        logging.info(f"[AliveCheck] Result: PASSED (Reason: {btn_count} buttons detected)")
                        return True, ""
                
                if spambot_replied:
                    break
                    
            if not spambot_replied:
                logging.warning(f"[AliveCheck] SpamBot did not reply for {getattr(me, 'phone_number', '?')}. Rejecting for safety.")
                return False, "spam_no_reply"

        except Exception as e:
            err_type = type(e).__name__
            logging.error(f"[AliveCheck] SpamBot check CRASHED for {getattr(me, 'phone_number', '?')}: {err_type} - {e}")
            
            if "YouBlockedUser" in err_type:
                return False, "unblock_spambot"
                
            if any(x in err_type for x in ["PeerFlood", "UserRestricted", "Forbidden", "ChatWriteForbidden"]):
                return False, "spam"
            # Any other error (PEER_ID_INVALID, Timeout, etc) = can't verify = reject
            return False, f"Account check failed: {err_type}"
            
    except Exception as e:
        err_type = type(e).__name__
        err_str = str(e).lower()
        logging.error(f"[AliveCheck] Global check error: {err_type} - {e}")
        if "unauthorized" in err_str or "auth" in err_str or "session" in err_str:
            return False, "session_removed"
        logging.warning(f"Session failed alive check: {e}")
        return False, "frozen"
    finally:
        try:
            if 'client' in locals() and client.is_connected:
                await client.disconnect()
        except:
            pass
