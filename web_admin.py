import os
import logging
import math
import traceback
from datetime import datetime, timedelta
import phonenumbers
from phonenumbers import geocoder
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.future import select
from sqlalchemy import select, delete, update, func, text, or_, cast, String
from database.engine import async_session
from database.models import User, Account, Transaction, AccountStatus, TransactionType, CountryPrice, WithdrawalRequest, WithdrawalStatus, UserCountryPrice, AppSetting, ApiServer, SubscriptionChannel
from urllib.parse import parse_qsl
import re
import pycountry
import hmac
import hashlib
import time
import requests
from pydantic import BaseModel
from typing import List
# Delayed imports inside functions to avoid pyrogram event loop issues
from services.external_provider import ExternalProvider
from services.i18n import get_text
import pycountry
import re
import urllib.request
import json
import asyncio
from datetime import datetime
import random
import string

class AdminAuthRequest(BaseModel):
    user_id: int
    init_data: str

class SellerDataRequest(BaseModel):
    user_id: int

class SellerOTPRequest(BaseModel):
    user_id: int
    phone: str
    init_data: str # Added for security

class SellerOTPSubmit(BaseModel):
    user_id: int
    phone: str
    hash: str
    code: str
    country: str
    buy_price: float
    init_data: str # Added for security

class WithdrawSubmit(BaseModel):
    user_id: int
    amount: float
    method: str
    address: str
    init_data: str # Added for security verification

class WithdrawAction(AdminAuthRequest):
    request_id: int
    action: str # 'approve' or 'reject'


def normalize_provider_countries(srv_countries):
    """Normalizes various API provider responses into a standard list of country dicts."""
    countries_list = []
    
    # 1. Super Parser: Find the node that actually contains country data
    def find_country_node(node):
        if isinstance(node, dict):
            # Case A: Dict with country keys (EG, PS, etc.)
            if any(k in node for k in ["EG", "PS", "SA", "US", "20", "966", "970"]):
                return node
            # Case B: Dict that contains common keys like price/count
            if any(k in node for k in ["price", "count", "rate", "cost", "stock"]):
                return node
            # Otherwise, drill down
            for k, v in node.items():
                res = find_country_node(v)
                if res: return res
        elif isinstance(node, list):
            # Case C: List of objects - check first few items
            for item in node[:3]:
                if isinstance(item, dict):
                    if any(k in item for k in ["price", "count", "rate", "cost", "stock"]):
                        return node # Return the whole list
                    res = find_country_node(item)
                    if res: return node # Return the whole list if children are good
        return None

    # Special handling for Spider Service typo-prone and split structure
    # result: { countries: {1: {ISO: price}}, cuantity: {1: {ISO: count}} }
    spider_prices = {}
    spider_counts = {}
    
    if isinstance(srv_countries, dict) and "result" in srv_countries:
        res = srv_countries["result"]
        if isinstance(res, dict):
            # Try to find prices
            p_node = res.get("countries")
            if isinstance(p_node, dict) and "1" in p_node: p_node = p_node["1"]
            if isinstance(p_node, dict): spider_prices = p_node
            
            # Try to find quantities (handling the 'cuantity' typo)
            q_node = res.get("cuantity") or res.get("quantity")
            if isinstance(q_node, dict) and "1" in q_node: q_node = q_node["1"]
            if isinstance(q_node, dict): spider_counts = q_node

    if spider_prices:
        # If we found Spider-specific split data, merge it
        for code, price in spider_prices.items():
            try:
                countries_list.append({
                    "country": code,
                    "price": float(price),
                    "count": int(spider_counts.get(code, 999))
                })
            except: continue
    else:
        # Fallback to Super Parser for TG-Lion and others
        data_node = find_country_node(srv_countries)
        if not data_node:
            data_node = srv_countries
            for key in ["result", "data", "countries_info", "countries"]:
                if isinstance(data_node, dict) and key in data_node:
                    data_node = data_node[key]
                    break
        
        if isinstance(data_node, dict):
            for code, val in data_node.items():
                if code.lower() in ["status", "message", "error", "ok", "msg", "currency", "success", "rate", "price", "count", "stock", "quantity", "qty", "server_time"]: continue
                if isinstance(val, dict):
                    entry = val.copy()
                    entry["country"] = code
                    countries_list.append(entry)
                elif isinstance(val, (int, float, str)):
                    try:
                        # Use a helper to clean price string if it's not a direct float
                        def clean_p(v):
                            if isinstance(v, (int, float)): return float(v)
                            try: return float(str(v).replace('$', '').replace('USD', '').strip().split()[0])
                            except: return 0.0

                        price_val = clean_p(val)
                        countries_list.append({
                            "country": code,
                            "count": 999,
                            "price": price_val
                        })
                    except: continue
        elif isinstance(data_node, list):
            # Normalize list items to have 'country' key
            for item in data_node:
                if not isinstance(item, dict): continue
                normalized = item.copy()
                if "country" not in normalized:
                    # Try to find country code in common keys
                    for k in ["id", "iso", "code", "name"]:
                        if k in normalized:
                            normalized["country"] = normalized[k]
                            break
                countries_list.append(normalized)
    
    return countries_list



class GeneralSettingsSubmit(AdminAuthRequest):
    bot_name: str
    purchase_log_channel_id: str
    deposit_log_channel_id: str = ""

class ApiServerSubmit(AdminAuthRequest):
    id: int | None = None
    name: str
    url: str
    api_key: str
    server_type: str = "standard"
    extra_id: str | None = None
    profit_margin: float
    min_profit: float = 0.0
    is_active: bool

class MaintenanceToggle(AdminAuthRequest):
    enabled: bool

class ReferralSettingsSubmit(AdminAuthRequest):
    join_bonus: float
    commission_percent: float

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SECURITY: OTP Cooldown Tracking
otp_cooldowns = {} # {phone_number: timestamp, user_id: timestamp}
OTP_COOLDOWN_SECONDS = 15

def generate_transaction_id():
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choice(chars) for _ in range(10))
    return f"TC{suffix}"

def get_flag_emoji(country_code: str):
    """Convert ISO country code to flag emoji."""
    try:
        if not country_code or not isinstance(country_code, str) or len(country_code) != 2:
            return "🌐"
        return "".join(chr(ord(c) + 127397) for c in country_code.upper())
    except:
        return "🌐"

_bot_info_cache = {}

def verify_telegram_auth(init_data: str, bot_token: str, expected_user_id: int) -> bool:
    """Verifies that the request actually comes from the claimed user using Telegram Web App Hash."""
    try:
        if not init_data: return False
        parsed_data = dict(parse_qsl(init_data))
        hash_str = parsed_data.pop('hash', None)
        if not hash_str: return False
        
        # Check if the user ID in init_data matches the claimed user_id
        user_obj = json.loads(parsed_data.get('user', '{}'))
        if int(user_obj.get('id', 0)) != expected_user_id:
            logger.warning(f"Auth Mismatch: Claims {expected_user_id} but InitData is for {user_obj.get('id')}")
            return False
            
        data_check_string = '\n'.join([f"{k}={v}" for k, v in sorted(parsed_data.items())])
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_str
    except Exception as e:
        logger.error(f"Auth Verification Exception: {e}")
        return False

def verify_admin_auth_multi(init_data: str, user_id: int, bot_type: str = "any") -> bool:
    """Helper to verify admin auth against SOURCING_ADMIN_IDS."""
    import config
    from config import BOT_TOKEN
    if not init_data or not user_id: return False
    if user_id not in config.SOURCING_ADMIN_IDS: return False
    return verify_telegram_auth(init_data, BOT_TOKEN, user_id)


def verify_user_auth_multi(init_data: str, user_id: int) -> bool:
    """Helper to verify user auth against BOT_TOKEN."""
    import config
    from config import BOT_TOKEN
    if not init_data or not user_id: return False
    return verify_telegram_auth(init_data, BOT_TOKEN, user_id)





async def send_sourcing_price_log(country_name: str, iso_code: str, country_code: str, buy_price: float, approve_delay: int, quantity: int = 1000):
    """Send a price update log to the configured Telegram channel."""
    import asyncio
    import urllib.request
    import json
    import html as _html
    try:
        async with async_session() as session:
            stmt = select(AppSetting).where(AppSetting.key == "sourcing_log_channel_id")
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            if not obj or not obj.value:
                logger.warning("send_sourcing_price_log: sourcing_log_channel_id is not configured.")
                return
            channel_id = obj.value.strip()

            # Standardize channel ID
            if channel_id.isdigit() or (channel_id.startswith('-') and not channel_id.startswith('-100')):
                if not channel_id.startswith('-'):
                    channel_id = f"-100{channel_id}"
                elif channel_id.startswith('-') and not channel_id.startswith('-100'):
                    channel_id = f"-100{channel_id[1:]}"

        from config import BOT_TOKEN

        flag = get_flag_emoji(iso_code)
        c_name = str(country_name or "Unknown")
        for e in ["\U0001f1f8\U0001f1e6", "\U0001f1ea\U0001f1ec", "\U0001f1fa\U0001f1fe", "\U0001f310"]:
            c_name = c_name.replace(e, "")
        clean_name = _html.escape(c_name.strip())

        buy_str = f"{buy_price:.3f}".rstrip('0').rstrip('.')
        if '.' not in buy_str:
            buy_str = f"{buy_price:.2f}"

        message = (
            f"- {clean_name} - {flag} - ${buy_str}\n\n"
            f"- Quantity - {quantity} - +{_html.escape(str(country_code))} - {_html.escape(str(iso_code))}\n\n"
            f"- Confirmation time [ {approve_delay} ] second\n\n"
            "-The bot is always open. I will announce on this channel if the price goes up or down"
        )

        def _send_tg():
            _username = ""
            try:
                r0 = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                with urllib.request.urlopen(r0, timeout=5) as rr:
                    d0 = json.loads(rr.read().decode())
                    if d0.get("ok"):
                        _username = d0["result"].get("username", "")
            except Exception:
                pass

            payload = {"chat_id": channel_id, "text": message, "parse_mode": "HTML"}
            if _username:
                payload["reply_markup"] = {
                    "inline_keyboard": [[{"text": "\U0001f916 BOT \U0001f916", "url": f"https://t.me/{_username}"}]]
                }

            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        result = await asyncio.to_thread(_send_tg)
        if not result.get("ok"):
            logger.error(f"Telegram API rejected sourcing log: {result}")
        else:
            logger.info(f"Sourcing price log sent -> channel={channel_id} country={country_name}")

    except Exception as e:
        logger.error(f"Error sending sourcing price log: {e}")

# ---- end send_sourcing_price_log ----


def resolve_country_info(country_code_str: str, full_phone: str = None):
    """Resolve ISO code and Country Name. Handles numeric codes, Alpha-2, and Alpha-3."""
    import pycountry
    import phonenumbers
    import re
    try:
        code_str = str(country_code_str).strip().upper().lstrip('+')
        if not code_str: return "Unknown", "🌐", "XX"

        # 1. Handle if it's already an ISO code (Alpha-2 or Alpha-3)
        if not code_str.isdigit() and len(code_str) in [2, 3]:
            try:
                country = None
                if len(code_str) == 2:
                    country = pycountry.countries.get(alpha_2=code_str)
                else:
                    country = pycountry.countries.get(alpha_3=code_str)
                
                if country:
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', country.name).strip()
                    iso = country.alpha_2
                    return name, get_flag_emoji(iso), iso
            except: pass

        # 2. Handle if full_phone is provided
        if full_phone:
            try:
                parsed = phonenumbers.parse(full_phone if full_phone.startswith('+') else f"+{full_phone}")
                iso_code = phonenumbers.region_code_for_number(parsed)
                country = pycountry.countries.get(alpha_2=iso_code)
                name = country.name if country else iso_code
                name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                return name, get_flag_emoji(iso_code), iso_code
            except: pass

        # 3. Handle numeric calling code prefix
        if code_str.isdigit():
            try:
                numeric_code = int(code_str)
                iso_code = phonenumbers.region_code_for_country_code(numeric_code)
                flag = get_flag_emoji(iso_code)
                
                name = f"Country {numeric_code}"
                country = pycountry.countries.get(alpha_2=iso_code)
                if country:
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', country.name).strip()
                return name, flag, iso_code
            except: pass
        
        return f"Code {code_str}", "🌐", "XX"
    except Exception as e:
        logger.error(f"Error resolving country {country_code_str}: {e}")
        return f"Code {country_code_str}", "🌐", "XX"

def clean_display_name(raw_name: str) -> str:
    """Removes trailing ISO codes like EG, (EG), or [EG], and resolves standalone codes."""
    if not raw_name: return raw_name
    
    # Standalone code resolution map
    codes_map = {
        "EG": "Egypt",
        "US": "United States",
        "UK": "United Kingdom",
        "SA": "Saudi Arabia",
        "RU": "Russia",
        "UA": "Ukraine"
    }
    
    # If the name itself is just a code, resolve it
    trimmed = raw_name.strip().upper()
    if trimmed in codes_map:
        return codes_map[trimmed]
    
    # Split by comma and parenthesis to take the first part
    raw_name = raw_name.split(',')[0].split('(')[0]
    
    removals = [
        "Islamic Republic of",
        "Province of China",
        "Republic of",
        "Federation",
        "United Republic of",
        "Plurinational State of",
        "Bolivarian Republic of",
        "People's Democratic Republic",
        "Arab Republic",
        "Democratic "
    ]
    for r in removals:
        raw_name = raw_name.replace(r, "")
        
    # Handle formats like "Egypt EG", "Egypt (EG)", "Egypt [EG]"
    clean = re.sub(r'\s*[\(\[]?[A-Z]{2,3}[\)\]]?\s*$', '', raw_name)
    return clean.strip()

# ─── Babel Locale Map ───
_LANG_TO_BABEL = {
    "en": "en",
    "ar": "ar",
    "zh": "zh_Hans",
    "bn": "bn",
    "fa": "fa",
    "ru": "ru",
    "uz": "uz",
    "es": "es",
    "tr": "tr",
}

def get_localized_country_name(iso_code: str, lang: str) -> str:
    """Return country name localized to the given language using Babel."""
    if not iso_code or iso_code == 'XX':
        return "ERROR_ISO_XX"
    try:
        from babel import Locale
        from babel.core import UnknownLocaleError
        locale_str = _LANG_TO_BABEL.get(lang, "en")
        try:
            locale = Locale.parse(locale_str)
        except UnknownLocaleError:
            locale = Locale.parse("en")
        name = locale.territories.get(iso_code.upper())
        return name if name else f"ERROR_NO_NAME_FOR_{iso_code}"
    except Exception as e:
        return f"ERROR_BABEL_{str(e)}"

app = FastAPI(title="Store Admin Panel")

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # Apply no-cache to both API and HTML pages to prevent aggressive caching in Telegram
    if request.url.path.startswith("/api/") or request.url.path.startswith("/seller") or request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/seller", response_class=HTMLResponse)
async def get_seller_panel(request: Request):
    return templates.TemplateResponse(request=request, name="seller.html")

@app.on_event("startup")
async def run_migrations():
    """Auto-migrate SQLite DB to add any missing columns and create new tables."""
    from database.engine import engine
    from database.models import Base
    import sqlalchemy
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.begin() as conn:
            # Add full_name to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN full_name TEXT"))
            except: pass
            # Add username to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN username TEXT"))
            except: pass
            # Add balance_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN balance_store FLOAT DEFAULT 0.0"))
                # Copy existing balance to balance_store if possible
                try:
                    await conn.execute(sqlalchemy.text("UPDATE users SET balance_store = balance"))
                except: pass
            except: pass

            # Add iso_code to user_country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE user_country_prices ADD COLUMN iso_code TEXT DEFAULT 'XX'"))
            except: pass
            # Add approve_delay to user_country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE user_country_prices ADD COLUMN approve_delay INTEGER DEFAULT 0"))
            except: pass
            
            # Add otp_code to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN otp_code VARCHAR"))
            except: pass
            # Add balance_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN balance_sourcing FLOAT DEFAULT 0.0"))
            except: pass
            # Add is_active_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_active_store BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_active_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_active_sourcing BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_banned_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_banned_store BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_banned_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_banned_sourcing BOOLEAN DEFAULT 0"))
            except: pass
            # Add referral columns to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN referred_by INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN referral_earnings FLOAT DEFAULT 0.0"))
            except: pass
            
            # One-time migration: set existing users active in both if they weren't before
            # This ensures they appear in dashboards immediately after migration
            try:
                await conn.execute(sqlalchemy.text("UPDATE users SET is_active_store = 1, is_active_sourcing = 1 WHERE is_active_store = 0 AND is_active_sourcing = 0"))
            except: pass

            # Add missing columns to accounts table
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN seller_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN buyer_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN created_at DATETIME"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN purchased_at DATETIME"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN server_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN hash_code TEXT"))
            except: pass

            # Add missing columns to country_prices table
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN country_name TEXT"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN buy_price FLOAT DEFAULT 0.0"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN approve_delay INTEGER DEFAULT 0"))
            except: pass
            
            # Add iso_code to country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN iso_code TEXT DEFAULT 'XX'"))
            except: pass
            
            # Drop unique constraint on country_code if it exists (SQLite workaround requires rebuilding table)
            try:
                table_sql_res = await conn.execute(sqlalchemy.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='country_prices'"))
                table_sql = table_sql_res.scalar()
                if table_sql and 'UNIQUE' in table_sql.upper():
                    logger.info("Rebuilding country_prices to remove UNIQUE constraint")
                    await conn.execute(sqlalchemy.text("""
                        CREATE TABLE country_prices_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            country_code VARCHAR NOT NULL,
                            iso_code VARCHAR DEFAULT 'XX',
                            country_name VARCHAR NOT NULL,
                            price FLOAT NOT NULL DEFAULT 1.0,
                            buy_price FLOAT NOT NULL DEFAULT 0.5,
                            approve_delay INTEGER NOT NULL DEFAULT 0,
                            updated_at DATETIME
                        )
                    """))
                    await conn.execute(sqlalchemy.text("INSERT INTO country_prices_new (id, country_code, iso_code, country_name, price, buy_price, approve_delay, updated_at) SELECT coalesce(id, 0), coalesce(country_code, ''), coalesce(iso_code, 'XX'), coalesce(country_name, 'Unknown'), coalesce(price, 0), coalesce(buy_price, 0), coalesce(approve_delay, 0), updated_at FROM country_prices"))
                    await conn.execute(sqlalchemy.text("DROP TABLE country_prices"))
                    await conn.execute(sqlalchemy.text("ALTER TABLE country_prices_new RENAME TO country_prices"))
            except Exception as e:
                logger.error(f"Failed to rebuild country_prices table: {e}")

            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN updated_at DATETIME"))
            except: pass

            # One-time resolution: Fix 'XX' iso_codes for legacy data
            try:
                cursor = await conn.execute(sqlalchemy.text("SELECT id, country_code FROM country_prices WHERE iso_code = 'XX' OR iso_code IS NULL"))
                rows = cursor.fetchall()
                for row_id, c_code in rows:
                    try:
                        clean_code = c_code.strip().lstrip('+').lstrip('0')
                        numeric_code = int(clean_code)
                        detected_iso = phonenumbers.region_code_for_country_code(numeric_code)
                        if detected_iso:
                            await conn.execute(sqlalchemy.text("UPDATE country_prices SET iso_code = :iso WHERE id = :id"), {"iso": detected_iso, "id": row_id})
                    except: pass
            except: pass

            try:
                await conn.execute(sqlalchemy.text("UPDATE country_prices SET updated_at = '" + datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + "' WHERE updated_at IS NULL"))
            except: pass

            # Fix: Sync existing available accounts with CountryPrice selling prices
            try:
                await conn.execute(sqlalchemy.text("""
                    UPDATE accounts 
                    SET price = (
                        SELECT price FROM country_prices 
                        WHERE country_prices.country_name = accounts.country 
                        LIMIT 1
                    )
                    WHERE status = 'available' 
                    AND EXISTS (
                        SELECT 1 FROM country_prices WHERE country_prices.country_name = accounts.country
                    )
                """))
                logger.info("Successfully synced available account prices with CountryPrice table.")
            except Exception as e:
                logger.warning(f"Failed to sync account prices: {e}")

            # Add two_fa_password column to accounts table if it doesn't exist
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN two_fa_password VARCHAR;"))
                logger.info("Added two_fa_password column to accounts table.")
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    pass

            # Add locked_buy_price to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN locked_buy_price FLOAT;"))
                logger.info("Added locked_buy_price column to accounts table.")
            except: pass
            # Add locked_approve_delay to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN locked_approve_delay INTEGER;"))
                logger.info("Added locked_approve_delay column to accounts table.")
            except: pass

            # Backfill locked values for existing pending accounts that have NULL values
            try:
                await conn.execute(sqlalchemy.text("""
                    UPDATE accounts
                    SET
                        locked_buy_price = (
                            SELECT cp.buy_price FROM country_prices cp
                            WHERE cp.country_name = accounts.country
                            LIMIT 1
                        ),
                        locked_approve_delay = (
                            SELECT cp.approve_delay FROM country_prices cp
                            WHERE cp.country_name = accounts.country
                            LIMIT 1
                        )
                    WHERE status = 'pending'
                    AND (locked_buy_price IS NULL OR locked_approve_delay IS NULL)
                """))
                logger.info("Backfilled locked values for legacy pending accounts.")
            except Exception as e:
                logger.warning(f"Backfill locked values warning: {e}")
            # Add reject_reason to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN reject_reason VARCHAR;"))
                logger.info("Added reject_reason column to accounts table.")
            except: pass

            # Add min_profit to api_servers if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE api_servers ADD COLUMN min_profit FLOAT DEFAULT 0.0;"))
                logger.info("Added min_profit column to api_servers table.")
            except: pass

            # Add fee and net_amount to withdrawal_requests if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE withdrawal_requests ADD COLUMN fee FLOAT NOT NULL DEFAULT 0.0;"))
                await conn.execute(sqlalchemy.text("ALTER TABLE withdrawal_requests ADD COLUMN net_amount FLOAT NOT NULL DEFAULT 0.0;"))
                logger.info("Added fee and net_amount columns to withdrawal_requests table.")
            except: pass

            # One-time migration: Update server_type based on URL if it's 'standard' or 'other'
            try:
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'max' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%max-tg.com%'"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'fast' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%fast-tg.com%'"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'lion' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND (url LIKE '%TG-Lion.net%' OR url LIKE '%tg-lion%')"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'spider' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%spider-service.com%'"))
                # If still standard/other, leave as 'other' to avoid 'Standard' label
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'other' WHERE server_type = 'standard'"))
                logger.info("Successfully migrated legacy server types based on URLs.")
            except Exception as e:
                logger.warning(f"Failed to migrate server types: {e}")

            # Load extra admin IDs from database into memory
            try:
                from database.models import AppSetting
                from sqlalchemy import select
                import config
                import os
                
                # 1. Load Store Extra Admins
                extra_admins_obj = (await conn.execute(select(AppSetting).where(AppSetting.key == "extra_admin_ids"))).scalar_one_or_none()
                if extra_admins_obj and extra_admins_obj.value:
                    extra_str = extra_admins_obj.value
                    for eid in extra_str.split(","):
                        if eid.strip().isdigit():
                            parsed_id = int(eid.strip())
                            if False:  # store admin IDs removed
                                pass  # store admin IDs removed
                                
                # 2. Load Sourcing Extra Admins
                sourcing_admins_obj = (await conn.execute(select(AppSetting).where(AppSetting.key == "sourcing_extra_admin_ids"))).scalar_one_or_none()
                if sourcing_admins_obj and sourcing_admins_obj.value:
                    sourcing_str = sourcing_admins_obj.value
                    for eid in sourcing_str.split(","):
                        if eid.strip().isdigit():
                            parsed_id = int(eid.strip())
                            if parsed_id not in config.SOURCING_ADMIN_IDS:
                                config.SOURCING_ADMIN_IDS.append(parsed_id)
                                
                logger.info(f"Loaded extra admins. Active SOURCING_ADMIN_IDS: {config.SOURCING_ADMIN_IDS}")
            except Exception as e:
                logger.warning(f"Failed to load extra admins: {e}")
                    
        logger.info("DB migration check complete.")
    except Exception as e:
        logger.warning(f"Migration warning: {e}")

# Use absolute path for templates to avoid issues in deployment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Models for API requests
class StockLoginStart(AdminAuthRequest):
    phone: str

class StockLoginComplete(AdminAuthRequest):
    phone: str
    code: str
    hash: str
    password: str = None
    country: str
    price: float

class BalanceUpdate(AdminAuthRequest):
    user_id_target: int
    amount: float
    type: str = "store" # "store" or "sourcing"

class BanToggle(AdminAuthRequest):
    user_id_target: int
    bot_type: str # "store" or "sourcing"
    banned: bool

class PriceUpdate(AdminAuthRequest):
    country_code: str
    country_name: str
    iso_code: str = "XX"
    price: float
    buy_price: float
    approve_delay: int

class UserPriceCreate(AdminAuthRequest):
    id: int | None = None
    user_id_target: int # Changed from user_id to avoid conflict with admin user_id
    country_code: str
    iso_code: str = "XX"
    buy_price: float
    approve_delay: int = 0



class UserSync(AdminAuthRequest):
    user_id_target: int
    bot_type: str # "store" or "sourcing"

@app.get("/admin/sourcing", response_class=HTMLResponse)
async def admin_sourcing(request: Request):
    try:
        import config
        return templates.TemplateResponse(request=request, name="admin_sourcing.html", context={"ADMIN_IDS": config.SOURCING_ADMIN_IDS})
    except Exception as e:
        logger.error(f"Error rendering sourcing dashboard: {e}")
        return HTMLResponse(content=f"<h1>Error</h1><pre>{e}</pre>", status_code=500)

@app.get("/", response_class=HTMLResponse)
async def root_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/sourcing")

@app.get("/api/admin/sourcing/data")
async def get_sourcing_data(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            total_sourced = (await session.execute(select(func.count(Account.id)))).scalar() or 0
            pending_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.PENDING))).scalar() or 0
            accepted_sourced = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.AVAILABLE))).scalar() or 0
            available_count = accepted_sourced
            sold_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD))).scalar() or 0
            rejected_sourced = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED))).scalar() or 0
            frozen_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%frozen%"), Account.reject_reason.ilike("%banned%"), Account.reject_reason.ilike("%تجميد%"), Account.reject_reason.ilike("%محظور%"), Account.reject_reason.ilike("%باند%")), Account.reject_reason.ilike("%REVOKED%") == False))).scalar() or 0
            spam_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%spam%"), Account.reject_reason.ilike("%restricted%"), Account.reject_reason.ilike("%سبام%"), Account.reject_reason.ilike("%مقيد%"), Account.reject_reason.ilike("%محدود%"))))).scalar() or 0
            
            # Withdrawal stats
            withdraw_pending = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))).scalar() or 0
            withdraw_approved = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == WithdrawalStatus.APPROVED))).scalar() or 0
            withdraw_rejected = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalStatus.REJECTED == WithdrawalRequest.status))).scalar() or 0
            total_paid_amount = (await session.execute(select(func.sum(WithdrawalRequest.amount)).where(WithdrawalRequest.status == WithdrawalStatus.APPROVED))).scalar() or 0
            withdraw_pending_amount = (await session.execute(select(func.sum(WithdrawalRequest.amount)).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))).scalar() or 0
            
            # User stats
            total_users = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True))).scalar() or 0
            banned_users = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True, User.is_banned_sourcing == True))).scalar() or 0
            active_users = total_users - banned_users
            
            # Custom User Prices stats (Unique Users & Countries)
            from sqlalchemy import distinct
            total_custom_prices = (await session.execute(select(func.count(distinct(UserCountryPrice.user_id))))).scalar() or 0
            total_custom_countries = (await session.execute(select(func.count(distinct(UserCountryPrice.iso_code))))).scalar() or 0
            
            recent_result = await session.execute(
                select(Account).order_by(Account.id.desc()).limit(50)
            )
            recent = []
            for a in recent_result.scalars().all():
                flag = "🌐"
                try:
                    p = phonenumbers.parse(a.phone_number)
                    flag = get_flag_emoji(phonenumbers.region_code_for_number(p))
                except: pass
                
                # Fetch actual buy_price from CountryPrice
                actual_buy_price = 0
                try:
                    parsed = phonenumbers.parse(a.phone_number)
                    cc = str(parsed.country_code)
                    target_iso = phonenumbers.region_code_for_number(parsed)
                    
                    stmt = select(CountryPrice).where(
                        CountryPrice.country_code == cc,
                        CountryPrice.iso_code == target_iso
                    )
                    cp_row = (await session.execute(stmt)).scalar()
                    if not cp_row:
                         cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                         
                    if cp_row:
                        actual_buy_price = cp_row.buy_price
                except: pass
                recent.append({
                    "phone": a.phone_number,
                    "country": f"{flag} {a.country}",
                    "buy_price": actual_buy_price,
                    "status": a.status.name,
                    "seller_id": a.seller_id,
                    "date": a.created_at.isoformat() if a.created_at else None
                })

            prices_result = await session.execute(
                select(CountryPrice).where(CountryPrice.buy_price > 0).order_by(CountryPrice.updated_at.desc())
            )
            prices = []
            for p in prices_result.scalars().all():
                iso = getattr(p, 'iso_code', None) or 'XX'
                flag = get_flag_emoji(iso)
                prices.append({
                    "code": p.country_code, 
                    "iso": iso,
                    "name": f"{flag} {clean_display_name(p.country_name)}", 
                    "buy_price": p.buy_price,
                    "price": p.price,
                    "approve_delay": p.approve_delay,
                    "log_quantity": getattr(p, 'log_quantity', 1000)
                })

            # Bot-specific user count and balance
            # Priority: AppSetting > Telegram
            bot_name = "Bot"
            try:
                bn_stmt = select(AppSetting).where(AppSetting.key == "bot_name")
                bn_res = await session.execute(bn_stmt)
                bn_obj = bn_res.scalar_one_or_none()
                if bn_obj:
                    bot_name = bn_obj.value
                else:
                    from config import BOT_TOKEN
                    def fetch_bot_name():
                        try:
                            req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                            with urllib.request.urlopen(req, timeout=2) as r:
                                res_data = json.loads(r.read().decode())
                                if res_data.get("ok"):
                                    return res_data["result"].get("first_name", "Bot")
                        except: return "Bot"
                    bot_name = await asyncio.to_thread(fetch_bot_name)
            except Exception as b_err:
                logger.error(f"Error fetching sourcing bot name: {b_err}")

            user_count = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True))).scalar() or 0
            total_sourcing_balance = (await session.execute(select(func.sum(User.balance_sourcing)).where(User.is_active_sourcing == True))).scalar() or 0.0

            users_result = await session.execute(select(User).where(User.is_active_sourcing == True).order_by(User.join_date.desc()).limit(200))
            db_users = users_result.scalars().all()
            
            # Get seller stats for these users
            u_ids = [u.id for u in db_users]
            seller_stats = {uid: {"sold": 0, "accepted": 0, "rejected": 0} for uid in u_ids}
            if u_ids:
                sold_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.SOLD).group_by(Account.seller_id)
                acc_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.AVAILABLE).group_by(Account.seller_id)
                rej_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.REJECTED).group_by(Account.seller_id)
                
                for rid, cnt in (await session.execute(sold_stmt)).all(): seller_stats[rid]["sold"] = cnt
                for rid, cnt in (await session.execute(acc_stmt)).all(): seller_stats[rid]["accepted"] = cnt
                for rid, cnt in (await session.execute(rej_stmt)).all(): seller_stats[rid]["rejected"] = cnt

            users_list = []
            for u in db_users:
                users_list.append({
                    "id": u.id,
                    "full_name": u.full_name or "N/A",
                    "username": f"@{u.username}" if u.username else "N/A",
                    "balance_sourcing": round(u.balance_sourcing or 0.0, 3),
                    "join_date": u.join_date.strftime("%Y-%m-%d") if u.join_date else "N/A",
                    "banned": u.is_banned_sourcing,
                    "sold_count": seller_stats[u.id]["sold"],
                    "accepted_count": seller_stats[u.id]["accepted"],
                    "rejected_count": seller_stats[u.id]["rejected"]
                })

            # Sourcing settings
            settings_stmt = select(AppSetting).where(AppSetting.key.in_(["sourcing_log_channel_id", "sourcing_join_log_channel_id", "min_withdraw_trx", "min_withdraw_usdt", "fee_withdraw_trx", "fee_withdraw_usdt", "sourcing_extra_admin_ids"]))
            settings_res = await session.execute(settings_stmt)
            settings_dict = {s.key: s.value for s in settings_res.scalars().all()}
            
            sourcing_log_channel_id = settings_dict.get("sourcing_log_channel_id", "")
            sourcing_join_log_channel_id = settings_dict.get("sourcing_join_log_channel_id", "")
            min_withdraw_trx = settings_dict.get("min_withdraw_trx", "4.0")
            min_withdraw_usdt = settings_dict.get("min_withdraw_usdt", "10.0")
            fee_withdraw_trx = settings_dict.get("fee_withdraw_trx", "0.2")
            fee_withdraw_usdt = settings_dict.get("fee_withdraw_usdt", "0.2")
            sourcing_extra_admin_ids = settings_dict.get("sourcing_extra_admin_ids", "")

            # Support & Channel settings
            support_username_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            updates_channel_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
            support_username = support_username_obj.value if support_username_obj else ""
            updates_channel = updates_channel_obj.value if updates_channel_obj else ""

            return {
                "bot_name": bot_name,
                "sourcing_log_channel_id": sourcing_log_channel_id,
                "sourcing_join_log_channel_id": sourcing_join_log_channel_id,
                "support_username": support_username,
                "updates_channel": updates_channel,
                "extra_admin_ids": sourcing_extra_admin_ids,
                "min_withdraw_trx": min_withdraw_trx,
                "min_withdraw_usdt": min_withdraw_usdt,
                "fee_withdraw_trx": fee_withdraw_trx,
                "fee_withdraw_usdt": fee_withdraw_usdt,
                "stats": {
                    "total_sourced": total_sourced, 
                    "pending_count": pending_count,
                    "available_count": available_count,
                    "sold_count": sold_count,
                    "accepted_sourced": accepted_sourced,
                    "rejected_sourced": rejected_sourced,
                    "frozen_count": frozen_count, # force
                    "spam_count": spam_count, # force
                    "total_balance": round(total_sourcing_balance, 3),
                    "user_count": user_count,
                    "withdraw_pending": withdraw_pending,
                    "withdraw_approved": withdraw_approved,
                    "withdraw_rejected": withdraw_rejected,
                    "withdraw_pending_amount": float(withdraw_pending_amount),
                    "total_paid_amount": float(total_paid_amount),
                    "total_users": total_users,
                    "active_users": active_users,
                    "banned_users": banned_users,
                    "total_custom_prices": total_custom_prices,
                    "total_custom_countries": total_custom_countries
                },
                "recent": recent,
                "prices": prices,
                "users": users_list,
                "support_username": (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none().value if (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none() else "",
                "updates_channel": (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none().value if (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none() else ""
            }
    except Exception as e:
        logger.error(f"Sourcing Data Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/support/settings")
async def save_support_settings(data: dict):
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            allowed_keys = [
                "SUPPORT_USERNAME", "UPDATES_CHANNEL", "PURCHASE_LOG_CHANNEL_ID",
                "SOURCING_LOG_CHANNEL_ID", "purchase_log_channel_id", "sourcing_log_channel_id",
                "deposit_log_channel_id", "store_join_log_channel_id", "sourcing_join_log_channel_id",
                "extra_admin_ids", "sourcing_extra_admin_ids"
            ]
            for k, v in data.items():
                if k in ["user_id", "init_data"]: continue
                if k not in allowed_keys: continue
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                if obj:
                    obj.value = v.strip() if isinstance(v, str) else str(v)
                else:
                    session.add(AppSetting(key=k, value=v.strip() if isinstance(v, str) else str(v)))
            await session.commit()

            import config, os
            base_admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

            # Update STORE_ADMIN_IDS removed (store bot deleted)
            if "extra_admin_ids" in data:
                # config.STORE_ADMIN_IDS removed (store bot deleted)
                # store admin IDs removed
                val = data.get("extra_admin_ids", "")
                if val and isinstance(val, str):
                    for eid in val.split(","):
                        if eid.strip().isdigit():
                            pid = int(eid.strip())
                            pass  # store admin IDs removed
                # SOURCING_ADMIN_IDS updated above

            # Update SOURCING_ADMIN_IDS if sourcing_extra_admin_ids changed
            if "sourcing_extra_admin_ids" in data:
                config.SOURCING_ADMIN_IDS.clear()
                config.SOURCING_ADMIN_IDS.extend(base_admins)
                val = data.get("sourcing_extra_admin_ids", "")
                if val and isinstance(val, str):
                    for eid in val.split(","):
                        if eid.strip().isdigit():
                            pid = int(eid.strip())
                            if pid not in config.SOURCING_ADMIN_IDS:
                                config.SOURCING_ADMIN_IDS.append(pid)
                logger.info(f"Updated SOURCING_ADMIN_IDS: {config.SOURCING_ADMIN_IDS}")

            return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/admin/system/maintenance")
async def get_maintenance(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    async with async_session() as session:
        mnt_store = (await session.execute(select(AppSetting).where(AppSetting.key == "STORE_UNDER_MAINTENANCE"))).scalar_one_or_none()
        mnt_src = (await session.execute(select(AppSetting).where(AppSetting.key == "SOURCING_UNDER_MAINTENANCE"))).scalar_one_or_none()
        return {
            "store_enabled": (mnt_store.value.lower() == "true") if mnt_store else False,
            "sourcing_enabled": (mnt_src.value.lower() == "true") if mnt_src else False
        }

async def _update_maintenance(key: str, enabled: bool):
    async with async_session() as session:
        logger.info(f"[Maintenance] Updating {key} to {enabled}")
        setting = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if not setting:
            session.add(AppSetting(key=key, value="true" if enabled else "false"))
        else:
            setting.value = "true" if enabled else "false"
        await session.commit()
    return {"status": "success"}

@app.post("/api/admin/sourcing/maintenance")
async def set_sourcing_maintenance(data: MaintenanceToggle):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await _update_maintenance("SOURCING_UNDER_MAINTENANCE", data.enabled)

@app.post("/api/admin/stock/start-login")
async def start_login(data: StockLoginStart):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import request_app_code
    phone = data.phone
    if not phone.startswith("+"):
        phone = "+" + phone
        
    try:
        parsed = phonenumbers.parse(phone)
        country_code = str(parsed.country_code)
        iso_code = phonenumbers.region_code_for_number(parsed)
        flag = get_flag_emoji(iso_code)
        country_name = f"{flag} " + (geocoder.description_for_number(parsed, "en") or f"Code {country_code}")
    except Exception as e:
        logger.error(f"Phone Parse Error: {e}")
        raise HTTPException(status_code=400, detail="رقم هاتف غير صالح")
        
    async with async_session() as session:
        # Match by both code and ISO for accurate price lookup
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == country_code,
            CountryPrice.iso_code == iso_code
        )
        cp = (await session.execute(stmt)).scalar()
        if not cp:
             # Fallback to code only
             cp = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == country_code))).scalar()
        
        price = cp.price if cp else 1.0
        
    try:
        # Use -1 as a special ID for Admin Login
        code_hash = await request_app_code(-1, phone)
        return {
            "status": "success",
            "country": country_name,
            "price": price,
            "hash": code_hash
        }
    except Exception as e:
        logger.error(f"Login Start Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/stock/complete-login")
async def complete_login(data: StockLoginComplete):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import submit_app_code
    try:
        # If 2FA is needed, the current session_manager doesn't handle it well in submit_app_code.
        # But for now, we'll try the simple path.
        submit_result = await submit_app_code(-1, data.phone, data.hash, data.code)
        
        if not submit_result:
            raise HTTPException(status_code=400, detail="فشل في جلب الجلسة. قد يكون الكود خطأ.")
            
        session_string = submit_result["session_string"]
        two_fa_password = submit_result["two_fa_password"]
            
        async with async_session() as session:
            new_acc = Account(
                phone_number=data.phone,
                country=data.country,
                price=data.price,
                session_string=session_string,
                two_fa_password=two_fa_password,
                status=AccountStatus.AVAILABLE,
                created_at=datetime.now()
            )
            session.add(new_acc)
            await session.commit()
            
  # store price alert removed
            
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Login Complete Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/sourcing/price/update")
async def update_sourcing_price(data: dict):
    # data: {user_id, init_data, country_code, buy_price, approve_delay, iso_code, country_name}
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    code = data.get("country_code")
    iso = data.get("iso_code", "XX")
    buy_p = float(data.get("buy_price", 0))
    delay = int(data.get("approve_delay", 0))
    qty = int(data.get("quantity", 1000))
    c_name = data.get("country_name")

    # If iso/name not provided, auto-detect (legacy or basic add)
    if iso == "XX" or not c_name:
        name_only, _, detected_iso = resolve_country_info(code)
        if not c_name: c_name = name_only
        if iso == "XX": iso = detected_iso

    async with async_session() as session:
        # Match by both code and ISO to support shared prefixes like +1
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == code,
            CountryPrice.iso_code == iso
        )
        cp = (await session.execute(stmt)).scalar()
        
        if cp:
            cp.buy_price = buy_p
            cp.approve_delay = delay
            cp.log_quantity = qty
            cp.updated_at = datetime.utcnow()
            if c_name: cp.country_name = c_name
        else:
            cp = CountryPrice(
                country_code=code,
                iso_code=iso,
                country_name=c_name, 
                price=0,
                buy_price=buy_p,
                approve_delay=delay,
                log_quantity=qty
            )
            session.add(cp)
        await session.commit()
        
        # Trigger price log in background if enabled
        if data.get("send_log", True):
            try:
                await send_sourcing_price_log(cp.country_name, cp.iso_code, cp.country_code, cp.buy_price, cp.approve_delay, cp.log_quantity)
            except Exception as log_err:
                logger.error(f"Failed to send sourcing price log: {log_err}")
            
    return {"status": "success"}



@app.get("/api/admin/sourcing/user-prices")
async def get_user_prices(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice, User
    async with async_session() as session:
        result = await session.execute(
            select(UserCountryPrice, User)
            .join(User, (UserCountryPrice.user_id == User.id) & (User.is_active_sourcing == True))
            .order_by(UserCountryPrice.created_at.desc())
        )
        data = []
        for ucp, user in result.all():
            flag = "🌐"
            name = f"Code {ucp.country_code}"
            
            # Use iso_code if available (not 'XX')
            iso = ucp.iso_code if ucp.iso_code and ucp.iso_code != 'XX' else None
            
            try:
                import pycountry
                if iso:
                    from web_admin import get_flag_emoji
                    flag = get_flag_emoji(iso)
                    country = pycountry.countries.get(alpha_2=iso)
                    if country:
                        name = country.name
                        import re
                        name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                else:
                    n, f, _ = resolve_country_info(ucp.country_code)
                    if n != "Unknown":
                        name = n
                        flag = f
            except: pass
            
            data.append({
                "id": ucp.id,
                "user_id": user.id,
                "user_name": user.full_name or "N/A",
                "user_handle": f"@{user.username}" if user.username else "N/A",
                "country_code": ucp.country_code,
                "iso_code": ucp.iso_code,
                "country_name": f"{flag} {name}",
                "buy_price": ucp.buy_price,
                "approve_delay": ucp.approve_delay,
                "date": ucp.created_at.isoformat() if ucp.created_at else None
            })
        return {"prices": data}

@app.post("/api/admin/sourcing/user-prices")
async def add_user_price(data: UserPriceCreate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice, User
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
            
        if data.id:
            # Explicit Update
            ucp = await session.get(UserCountryPrice, data.id)
            if not ucp:
                raise HTTPException(status_code=404, detail="Price record not found")
            ucp.buy_price = data.buy_price
            ucp.approve_delay = data.approve_delay
            # If they changed the country/iso in the modal (though UI might prevent it)
            ucp.country_code = data.country_code
            ucp.iso_code = data.iso_code
        else:
            # Check for Duplicate before adding new
            stmt = select(UserCountryPrice).where(
                UserCountryPrice.user_id == data.user_id_target,
                UserCountryPrice.country_code == data.country_code,
                UserCountryPrice.iso_code == data.iso_code
            )
            existing = (await session.execute(stmt)).scalar()
            if existing:
                raise HTTPException(status_code=400, detail="This country is already added for this user. Please edit the existing entry instead.")
                
            new_ucp = UserCountryPrice(
                user_id=data.user_id_target,
                country_code=data.country_code,
                iso_code=data.iso_code,
                buy_price=data.buy_price,
                approve_delay=data.approve_delay
            )
            session.add(new_ucp)
            
        await session.commit()
        return {"status": "success"}

@app.delete("/api/admin/sourcing/user-prices/{id}")
async def delete_user_price(id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice
    async with async_session() as session:
        ucp = await session.get(UserCountryPrice, id)
        if ucp:
            await session.delete(ucp)
            await session.commit()

        return {"status": "success"}

@app.delete("/api/admin/prices/delete")
async def delete_price_entry(code: str, iso: str, user_id: int, init_data: str, bot: str = "store"):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == code,
            CountryPrice.iso_code == iso
        )
        cp = (await session.execute(stmt)).scalar()
        if cp:
            if bot == "sourcing":
                cp.buy_price = 0
            else:
                cp.price = 0
            
            # If both prices are 0, we can fully delete the entry
            if cp.price == 0 and cp.buy_price == 0:
                await session.delete(cp)
        
        await session.commit()
    return {"status": "success"}

@app.post("/api/admin/prices/update")
async def update_price(data: PriceUpdate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    """General update (mostly used by Store admin now)"""
    async with async_session() as session:
        # Identify by code and ISO
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == data.country_code,
            CountryPrice.iso_code == data.iso_code
        )
        cp = (await session.execute(stmt)).scalar()
        
        if cp:
            # PARTIAL UPDATE: Only touch store price and name
            cp.price = data.price
            if data.country_name and data.country_name != "Unknown":
                cp.country_name = data.country_name
            elif not cp.country_name or cp.country_name == "Unknown":
                name, _, _ = resolve_country_info(data.country_code)
                cp.country_name = name
            
            # CRITICAL: Do NOT overwrite buy_price or approve_delay if update is from store dashboard
            # We keep whatever is currently there.
            cp.updated_at = datetime.utcnow()
        else:
            name = data.country_name
            iso = data.iso_code
            if not name or name == "Unknown" or iso == "XX":
                name_det, _, iso_det = resolve_country_info(data.country_code)
                if not name or name == "Unknown": name = name_det
                if iso == "XX": iso = iso_det
                
            cp = CountryPrice(
                country_code=data.country_code,
                iso_code=iso,
                country_name=name, 
                price=data.price,
                buy_price=0, # Initial sourcing buy price is 0
                approve_delay=0
            )
            session.add(cp)
        await session.commit()
    return {"status": "success"}

@app.delete("/api/admin/stock/delete/{acc_id}")
async def delete_stock(acc_id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        acc = await session.get(Account, acc_id)
        if acc:
            await session.delete(acc)
            await session.commit()
    return {"status": "success"}

@app.post("/api/admin/user/balance")
async def update_balance(data: BalanceUpdate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if user:
            user.balance_sourcing = data.amount
            await session.commit()
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/user/toggle-ban")
async def toggle_ban(data: BanToggle):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if user:
            user.is_banned_sourcing = data.banned
            await session.commit()
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="User not found")
# --- Seller Panel APIs ---

@app.get("/api/seller/data")
async def get_seller_data(user_id: int, init_data: str, lang: str = "en"):
    try:
        from config import BOT_TOKEN
        if not verify_user_auth_multi(init_data, user_id):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        async with async_session() as session:
            # CHECK MAINTENANCE MODE FIRST (Admins bypass)
            mnt_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SOURCING_UNDER_MAINTENANCE"))).scalar_one_or_none()
            maintenance_mode = (mnt_obj.value.lower() == "true") if mnt_obj else False
            
            from config import SOURCING_ADMIN_IDS
            # Support & Channel settings
            support_username = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            updates_channel = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
            extra_admin_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "sourcing_extra_admin_ids"))).scalar_one_or_none()

            if maintenance_mode and user_id not in SOURCING_ADMIN_IDS:
                return {
                    "maintenance_sourcing": True,
                    "support_username": support_username.value if support_username else "",
                    "updates_channel": updates_channel.value if updates_channel else ""
                }

            if user_id:
                user = await session.get(User, user_id)
                if user and user.is_banned_sourcing and user_id not in SOURCING_ADMIN_IDS:
                    return {
                        "is_banned": True,
                        "support_username": support_username.value if support_username else "",
                        "updates_channel": updates_channel.value if updates_channel else ""
                    }

            user = await session.get(User, user_id)
            if not user:
                # Add new user without active flags
                user = User(id=user_id, balance_sourcing=0.0)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            
            # Fetch Bot Name dynamically
            bot_name = "Bot"
            try:
                from config import BOT_TOKEN
                with urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=5) as r:
                    res_data = json.loads(r.read().decode())
                    if res_data.get("ok"):
                        bot_name = res_data["result"].get("first_name", "Bot")
            except Exception as b_err:
                logger.error(f"Error fetching bot name: {b_err}")

            # Get stats
            sold_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.SOLD))).scalar() or 0
            pending_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.PENDING))).scalar() or 0
            accepted_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.AVAILABLE))).scalar() or 0
            rejected_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.REJECTED))).scalar() or 0
            
            # Calculate Pending Withdrawn — sum PENDING withdrawal requests
            pending_balance = (await session.execute(
                select(func.sum(WithdrawalRequest.amount)).where(
                    WithdrawalRequest.user_id == user_id,
                    WithdrawalRequest.status == WithdrawalStatus.PENDING
                )
            )).scalar() or 0.0
            
            # Calculate Total Withdrawn — sum only APPROVED withdrawal requests
            total_withdrawn = (await session.execute(
                select(func.sum(WithdrawalRequest.amount)).where(
                    WithdrawalRequest.user_id == user_id,
                    WithdrawalRequest.status == WithdrawalStatus.APPROVED
                )
            )).scalar() or 0.0
            
            # Get prices
            prices_result = await session.execute(select(CountryPrice).where(CountryPrice.buy_price > 0).order_by(CountryPrice.updated_at.desc()))
            prices = prices_result.scalars().all()
            
            # Get custom user prices (organized by code and ISO)
            from database.models import UserCountryPrice
            custom_prices_result = await session.execute(select(UserCountryPrice).where(UserCountryPrice.user_id == user_id))
            custom_rows = custom_prices_result.scalars().all()
            
            # Key: (country_code, iso_code)
            custom_prices = {(cp.country_code, cp.iso_code): cp.buy_price for cp in custom_rows}
            
            formatted_prices = []
            seen_codes = set()
            
            # First, add global prices, applying custom overrides if they exist
            for p in prices:
                try:
                    iso = getattr(p, 'iso_code', None) or 'XX'
                    default_name, default_flag, resolved_iso = resolve_country_info(p.country_code)
                    
                    if iso == 'XX' and resolved_iso != 'XX':
                        iso = resolved_iso
                        
                    flag = get_flag_emoji(iso)
                    name = p.country_name if p.country_name and p.country_name != "Unknown" else default_name
                    
                    # Resolve price: Specific ISO override > Generic XX override > Global price
                    price_val = custom_prices.get((p.country_code, iso), 
                                                custom_prices.get((p.country_code, 'XX'), p.buy_price))
                    
                    if price_val > 0:
                        localized_names = {lc: ln for lc in _LANG_TO_BABEL if (ln := get_localized_country_name(iso, lc))}
                        
                        # Add a debug key to check what iso we passed
                        localized_names["_debug_iso"] = iso
                        
                        # Fallback for cached frontends
                        single_localized = localized_names.get(lang) or name
                        
                        formatted_prices.append({
                            "name": name,
                            "iso": iso,
                            "localized_name": single_localized,
                            "localized_names": localized_names,
                            "flag": flag,
                            "code": p.country_code,
                            "price": price_val
                        })
                        seen_codes.add(p.country_code)
                except Exception as inner_e:
                    logger.error(f"Error processing price for code {p.country_code}: {inner_e}")
                    
            # Next, add any custom prices that are NOT in the global active list
            for (cc, c_iso), cp_buy_price in custom_prices.items():
                # Avoid duplicates if already added via the global prices loop
                if cc not in seen_codes and cp_buy_price > 0:
                    try:
                        # Use the specific ISO if available, else XX
                        n, f, resolved_iso = resolve_country_info(cc)
                        name = n if n != "Unknown" else f"Code {cc}"
                        
                        # If a custom ISO was specified, try to get a better name/flag
                        if c_iso != 'XX':
                            flag = get_flag_emoji(c_iso)
                            target_iso = c_iso
                        else:
                            flag = f
                            target_iso = c_iso if c_iso != 'XX' else None
                        localized_names = {lc: ln for lc in _LANG_TO_BABEL if (ln := get_localized_country_name(target_iso, lc))}
                        localized_names["_debug_iso"] = target_iso
                        
                        # Fallback for cached frontends
                        single_localized = localized_names.get(lang) or name
                        
                        formatted_prices.append({
                            "name": name,
                            "iso": target_iso or cc,
                            "localized_name": single_localized,
                            "localized_names": localized_names,
                            "flag": flag,
                            "code": cc,
                            "price": cp_buy_price
                        })
                    except: pass
                
            
            return {
                "maintenance_mode": False,
                "user": {
                    "id": user.id,
                    "balance": user.balance_sourcing,
                    "pending_balance": pending_balance,
                    "total_withdrawn": total_withdrawn,
                    "is_banned": user.is_banned_sourcing
                },
                "bot_name": bot_name,
                "stats": {
                    "sold": sold_count,
                    "pending": pending_count,
                    "accepted": accepted_count,
                    "rejected": rejected_count
                },
                "prices": formatted_prices,
                "settings": {
                    "min_withdraw_trx": float((await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar_one_or_none().value or 4.0) if (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar_one_or_none() else 4.0,
                    "min_withdraw_usdt": float((await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar_one_or_none().value or 10.0) if (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar_one_or_none() else 10.0,
                    "fee_withdraw_trx": float((await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar_one_or_none().value or 0.2) if (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar_one_or_none() else 0.2,
                    "fee_withdraw_usdt": float((await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar_one_or_none().value or 0.2) if (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar_one_or_none() else 0.2
                },
                "support_username": support_username.value if support_username else "",
                "updates_channel": updates_channel.value if updates_channel else "",
                "extra_admin_ids": extra_admin_obj.value if extra_admin_obj else ""
            }
    except Exception as e:
        logger.error(f"Seller Data API Error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"خطأ برمي: {str(e)}")

@app.post("/api/seller/request-otp")
async def seller_request_otp(data: SellerOTPRequest):
    from services.session_manager import request_app_code
    from config import BOT_TOKEN
    if not verify_user_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=401, detail="Unauthorized identity")

    async with async_session() as session:
        user = await session.get(User, data.user_id)
        if user and user.is_banned_sourcing:
            raise HTTPException(status_code=403, detail="عذراً، أنت محظور من التوريد.")
        
        # 0. OTP Flood Protection (Cooldown)
        now = time.time()
        phone_key = f"p_{data.phone.strip()}"
        user_key = f"u_{data.user_id}"
        
        last_phone_req = otp_cooldowns.get(phone_key, 0)
        last_user_req = otp_cooldowns.get(user_key, 0)
        
        if now - last_phone_req < OTP_COOLDOWN_SECONDS:
            wait_time = int(OTP_COOLDOWN_SECONDS - (now - last_phone_req))
            lang = user.language if user else "ar"
            raise HTTPException(status_code=429, detail=get_text("wait_code", lang, wait_time=wait_time))
            
        if now - last_user_req < OTP_COOLDOWN_SECONDS:
            wait_time = int(OTP_COOLDOWN_SECONDS - (now - last_user_req))
            lang = user.language if user else "ar"
            raise HTTPException(status_code=429, detail=get_text("wait_another", lang, wait_time=wait_time))

        # Update cooldowns
        otp_cooldowns[phone_key] = now
        otp_cooldowns[user_key] = now
            
    try:
        phone = data.phone.strip()
        if not phone.startswith("+"): phone = "+" + phone
        
        # Pre-check 1: Duplicity
        async with async_session() as session:
            dup_stmt = select(Account).where(Account.phone_number == phone)
            existing = (await session.execute(dup_stmt)).scalar()
            if existing:
                lang = user.language if user else "ar"
                raise HTTPException(status_code=400, detail=get_text("acc_exists", lang))

        # 2. Country & Pricing Check
        try:
            # Clean phone and parse
            phone_p = phone if phone.startswith("+") else "+" + phone
            parsed = phonenumbers.parse(phone_p)
            cc = str(parsed.country_code)
            target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
            
            async with async_session() as session:
                from sqlalchemy import or_
                # Fetch all possible price candidates for this country code
                # This covers both specific ISO and global 'XX' entries
                
                # Resilient code matching
                cc_clean = cc.lstrip("+")
                cc_plus = "+" + cc_clean
                
                logger.info(f"OTP Request: User={data.user_id}, CC={cc}, ISO={target_iso}")
                
                # Check User Specific Prices first
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == data.user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_list = (await session.execute(ucp_stmt)).scalars().all()
                logger.info(f"OTP User Candidates: {[f'{u.country_code}/{u.iso_code}' for u in ucp_list]}")
                
                # Filter for best match
                # Priority: Exact ISO > Global XX > First available for this code
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
                
                # Check Global Prices
                cp_stmt = select(CountryPrice).where(
                    or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
                )
                cp_list = (await session.execute(cp_stmt)).scalars().all()
                logger.info(f"OTP Global Candidates: {[f'{c.country_code}/{c.iso_code}' for c in cp_list]}")
                
                cp = next((c for c in cp_list if c.iso_code == target_iso), 
                          next((c for c in cp_list if c.iso_code == 'XX'), 
                               (cp_list[0] if cp_list else None)))
                
            # 3. Final Resolution
            final_buy_price = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
            
            if final_buy_price <= 0:
                raise HTTPException(status_code=400, detail="Sorry, this country is not requested at the moment.")
                
            # Clean number for Telegram (E164)
            phone_clean = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                    
        except HTTPException as he: raise he
        except Exception as e:
            logger.error(f"Sourcing Price Error: {e}")
            raise HTTPException(status_code=400, detail="Error detecting country price. Please check number format.")

        phone_code_hash = await request_app_code(data.user_id, phone_clean)
        return {"hash": phone_code_hash, "phone": phone_clean}
    except Exception as e:
        logger.error(f"Seller OTP Request Error: {e}")
        if isinstance(e, HTTPException): raise e
        err_msg = str(e)
        err_lower = err_msg.lower()
        # Handle Telegram FLOOD_WAIT
        if "flood_wait" in err_lower or "flood" in err_lower:
            import re as _re
            wait_match = _re.search(r'wait of (\d+) seconds', err_msg, _re.IGNORECASE)
            wait_secs = int(wait_match.group(1)) if wait_match else 3600
            if wait_secs >= 3600:
                wait_str = f"{wait_secs // 3600}h {(wait_secs % 3600) // 60}m"
            else:
                wait_str = f"{wait_secs // 60}m {wait_secs % 60}s"
            raise HTTPException(status_code=429, detail=f"FLOOD|{wait_str}")
        if any(x in err_lower for x in ["banned", "frozen", "security"]):
            raise HTTPException(status_code=400, detail=err_msg)
        # Handle common Telegram number errors cleanly
        if "phone_number_invalid" in err_lower:
            raise HTTPException(status_code=400, detail="INVALID_PHONE|This phone number is not valid or not registered on Telegram")
        if "phone_number_banned" in err_lower:
            raise HTTPException(status_code=400, detail="BANNED_PHONE|This number is permanently banned by Telegram")
        if "phone_number_unoccupied" in err_lower:
            raise HTTPException(status_code=400, detail="INVALID_PHONE|This phone number has no Telegram account")
        raise HTTPException(status_code=500, detail=f"Request error: {str(e)}")

@app.post("/api/seller/submit-otp")
async def seller_submit_otp(data: SellerOTPSubmit):
    from services.session_manager import submit_app_code
    from config import BOT_TOKEN
    if not verify_user_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=401, detail="Unauthorized identity")
    try:
        submit_result = await submit_app_code(data.user_id, data.phone, data.hash, data.code)
        
        if not submit_result:
            raise HTTPException(status_code=400, detail="The verification code you entered is incorrect")
            
        session_string = submit_result["session_string"]
        two_fa_password = submit_result["two_fa_password"]
        has_other_sessions = submit_result["has_other_sessions"]
            
        async with async_session() as session:
            # Automatic price detection
            price = 0
            try:
                phone_p = data.phone if data.phone.startswith("+") else "+" + data.phone
                parsed = phonenumbers.parse(phone_p)
                cc = str(parsed.country_code)
                target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
                
                # Resilient code matching
                cc_clean = cc.lstrip("+")
                cc_plus = "+" + cc_clean
                
                # 1. User Price
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == data.user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_list = (await session.execute(ucp_stmt)).scalars().all()
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
                
                # 2. Global Price
                cp_stmt = select(CountryPrice).where(
                    or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
                )
                cp_list = (await session.execute(cp_stmt)).scalars().all()
                cp = next((c for c in cp_list if c.iso_code == target_iso), 
                          next((c for c in cp_list if c.iso_code == 'XX'), 
                               (cp_list[0] if cp_list else None)))
                
                price = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
                locked_approve_delay = ucp.approve_delay if ucp else (cp.approve_delay if cp else 0)
            except Exception as e:
                logger.error(f"Submit Price Detection Error: {e}")

            new_acc = Account(
                phone_number=data.phone,
                country=data.country,
                price=price,
                session_string=session_string,
                two_fa_password=two_fa_password,
                status=AccountStatus.PENDING,
                seller_id=data.user_id,
                created_at=datetime.now(),
                locked_buy_price=price,
                locked_approve_delay=locked_approve_delay
            )
            session.add(new_acc)
            await session.commit()
            
  # store price alert removed
            
        return {"status": "success", "price": price}
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        logger.error(f"Seller OTP Submit Error: {e}")
        err_msg = str(e)
        err_msg_lower = err_msg.lower()
        
        # Custom 2FA Handling
        if "password" in err_msg_lower or "two-step" in err_msg_lower:
            raise HTTPException(status_code=400, detail="AUTH_ERROR|Please disable Two-Step Verification (2FA) and try again.")
            
        if "phone_code_invalid" in err_msg_lower:
            raise HTTPException(status_code=400, detail="WRONG_CODE|The verification code you entered is incorrect.")
            
        if "phone_code_expired" in err_msg_lower:
            raise HTTPException(status_code=400, detail="EXPIRED_CODE|This code has expired. Please request a new one.")

        if any(msg in err_msg_lower for msg in ["restricted", "frozen", "security check"]):
            raise HTTPException(status_code=400, detail=f"ACCOUNT_ERROR|{err_msg}")
            
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/seller/withdraw")
async def seller_withdraw(req: WithdrawSubmit):
    async with async_session() as session:
        # 1. AUTH VERIFICATION
        from config import BOT_TOKEN
        if not verify_user_auth_multi(req.init_data, req.user_id):
            raise HTTPException(status_code=401, detail="Unauthorized: Telegram identity verification failed.")

        # Secure User Fetch with Row Locking
        user = await session.get(User, req.user_id, with_for_update=True)
        if not user:
            raise HTTPException(status_code=403, detail="User not verified for sourcing bot.")
        
        # Validation: Use FULL balance for withdrawal
        withdraw_amount = user.balance_sourcing
        
        # Get dynamic withdrawal minimums from AppSetting
        trx_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar()
        usdt_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar()
        
        try:
            min_trx = float(trx_setting.value) if trx_setting and trx_setting.value else 4.0
        except ValueError:
            min_trx = 4.0
            
        try:
            min_usdt = float(usdt_setting.value) if usdt_setting and usdt_setting.value else 10.0
        except ValueError:
            min_usdt = 10.0
        
        # Get dynamic withdrawal fees from AppSetting
        trx_fee_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar()
        usdt_fee_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar()

        try:
            fee_trx = float(trx_fee_setting.value) if trx_fee_setting and trx_fee_setting.value else 0.2
        except ValueError:
            fee_trx = 0.2
            
        try:
            fee_usdt = float(usdt_fee_setting.value) if usdt_fee_setting and usdt_fee_setting.value else 0.2
        except ValueError:
            fee_usdt = 0.2
            
        min_amount = min_trx if "TRX" in req.method else min_usdt
        fee = fee_trx if "TRX" in req.method else fee_usdt
        
        if withdraw_amount < min_amount:
            raise HTTPException(status_code=400, detail=f"Minimum withdrawal is ${min_amount}")
        
        if withdraw_amount <= fee:
            raise HTTPException(status_code=400, detail="Amount too low to cover network fees")

        net_amount = withdraw_amount - fee

        # Create Request
        tid = generate_transaction_id()
        withdraw = WithdrawalRequest(
            user_id=req.user_id,
            amount=withdraw_amount,
            method=req.method,
            address=req.address,
            fee=fee,
            net_amount=net_amount,
            transaction_id=tid
        )
        
        # Deduct balance immediately
        user.balance_sourcing = 0
        
        session.add(withdraw)
        await session.flush() # Secure the ID
        
        # Link accounts to this withdrawal
        await session.execute(
            update(Account)
            .where(
                Account.seller_id == req.user_id, 
                or_(Account.status == AccountStatus.AVAILABLE, Account.status == AccountStatus.SOLD),
                Account.withdrawal_id == None
            )
            .values(withdrawal_id=withdraw.id)
        )
        
        await session.commit()
        await session.refresh(withdraw)
        return {"ok": True, "id": tid}

@app.get("/api/seller/withdrawals")
async def get_withdrawals(user_id: int, init_data: str, page: int = 1, status: str = "all"):
    from config import BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"history": [], "total_pages": 0, "current_page": 1, "total_count": 0}

    page_size = 10
    offset = (page - 1) * page_size
    async with async_session() as session:
        # Build base filter
        base_filters = [WithdrawalRequest.user_id == user_id]
        if status != "all":
            try:
                # Convert string status to enum
                enum_status = WithdrawalStatus(status.lower())
                base_filters.append(WithdrawalRequest.status == enum_status)
            except: pass

        # Get total count for pagination
        count_stmt = select(func.count(WithdrawalRequest.id)).where(*base_filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = (total_count + page_size - 1) // page_size

        # Get page data
        stmt = select(WithdrawalRequest).where(*base_filters).order_by(WithdrawalRequest.created_at.desc()).offset(offset).limit(page_size)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        for r in results:
            history.append({
                "id": r.id,
                "transaction_id": r.transaction_id,
                "amount": r.amount,
                "fee": r.fee,
                "net_amount": r.net_amount,
                "method": r.method,
                "address": r.address,
                "status": r.status.value,
                "date": r.created_at.isoformat() if r.created_at else None
            })
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "total_count": total_count
        }

@app.get("/api/admin/withdrawals/all")
async def admin_get_all_withdrawals(user_id: int, init_data: str, page: int = 1, status: str = "all"):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    page_size = 10
    offset = (page - 1) * page_size
    async with async_session() as session:
        # Build base filter
        filters = []
        if status != "all":
            # Map string to Enum member safely
            s_map = {
                "pending": WithdrawalStatus.PENDING, 
                "approved": WithdrawalStatus.APPROVED, 
                "rejected": WithdrawalStatus.REJECTED
            }
            if status.lower() in s_map:
                filters.append(WithdrawalRequest.status == s_map[status.lower()])
            
        # Count total
        count_stmt = select(func.count(WithdrawalRequest.id)).where(*filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        
        # Build results query
        stmt = select(WithdrawalRequest).where(*filters).order_by(WithdrawalRequest.created_at.desc()).offset(offset).limit(page_size)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        for r in results:
            # Fetch user info for display
            u = await session.get(User, r.user_id)
            history.append({
                "id": r.id,
                "user_id": r.user_id,
                "user_name": u.full_name if u else "N/A",
                "user_handle": f"@{u.username}" if u and u.username else "N/A",
                "transaction_id": r.transaction_id,
                "amount": r.amount,
                "fee": r.fee,
                "net_amount": r.net_amount,
                "method": r.method,
                "address": r.address,
                "status": r.status.value,
                "date": r.created_at.isoformat() if r.created_at else None
            })
            
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "total_count": total_count
        }

@app.post("/api/admin/withdrawals/action")
async def admin_withdrawal_action(data: WithdrawAction):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        req = await session.get(WithdrawalRequest, data.request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        if req.status != WithdrawalStatus.PENDING:
            raise HTTPException(status_code=400, detail=f"Request is already {req.status.value}")
            
        # 1. Update Status
        if data.action == "approve":
            req.status = WithdrawalStatus.APPROVED
            btn_text = "✅ Approved"
            msg_theme = "🟢"
        elif data.action == "reject":
            # NO REFUND as per user request
            req.status = WithdrawalStatus.REJECTED
            btn_text = "❌ Rejected (No Refund)"
            msg_theme = "🔴"
        else:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        await session.commit()
        
        # 2. Notify User via Bot
        bot = getattr(app.state, 'bot', None)
        if bot:
            try:
                # Localized message based on user preference
                user = await session.get(User, req.user_id)
                lang = user.language if user else "ar"
                
                if data.action == 'approve':
                    msg = get_text("withdraw_approved", lang, tx_id=req.transaction_id, amount=req.amount)
                else:
                    msg = get_text("withdraw_rejected", lang, tx_id=req.transaction_id, amount=req.amount)
                
                await bot.send_message(req.user_id, msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send withdrawal notification: {e}")
                
        return {"ok": True, "status": "success", "message": f"Withdrawal {data.action}ed successfully"}

@app.get("/api/admin/withdrawal/{request_id}/audit")
async def get_withdrawal_audit(request_id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        # 1. Get current withdrawal request
        req = await session.get(WithdrawalRequest, request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        # 2. Fetch accounts linked to this withdrawal
        acc_stmt = select(Account).where(
            Account.withdrawal_id == request_id
        ).order_by(Account.created_at.desc())
        
        accounts = (await session.execute(acc_stmt)).scalars().all()
        
        return {
            "accounts": [
                {
                    "id": a.id,
                    "phone": a.phone_number,
                    "country": a.country,
                    "price": a.price,
                    "status": a.status.value,
                    "date": a.created_at.isoformat()
                } for a in accounts
            ],
            "start_date": req.created_at.isoformat(), # Use request date as ref
            "total_count": len(accounts),
            "total_audit_value": sum(a.price for a in accounts)
        }

@app.post("/api/admin/accounts/check-alive")
async def admin_check_account_alive(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import is_session_alive
    acc_id = data.get("account_id")
    async with async_session() as session:
        acc = await session.get(Account, acc_id)
        if not acc: return {"status": "error", "message": "Not found"}
        
        if acc.status == AccountStatus.SOLD:
            return {"status": "sold"}
            
        try:
            is_alive, reason = await is_session_alive(acc.session_string)
            if is_alive:
                return {"status": "alive"}
            else:
                # If is_session_alive returns False, return the specific reason
                return {"status": "dead", "error": reason}
        except Exception as e:
            return {"status": "dead", "error": str(e)}







@app.get("/api/admin/countries-for-code/{code}")
async def get_countries_for_code(code: str, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    """Returns a list of matching countries for a given numeric code."""
    try:
        clean_code = code.strip().lstrip('+').lstrip('0')
        numeric_code = int(clean_code)
        regions = phonenumbers.COUNTRY_CODE_TO_REGION_CODE.get(numeric_code, [])
        
        results = []
        for r in regions:
            try:
                country = pycountry.countries.get(alpha_2=r)
                if country:
                    name = country.name
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                    results.append({"iso": r, "name": name, "flag": get_flag_emoji(r)})
            except: pass
        return results
    except:
        return []

@app.get("/api/seller/detect-country")
async def detect_country(phone: str, user_id: int, init_data: str):
    from config import BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"found": False}
    try:
        # Clean input
        raw = phone.strip().lstrip('+')
        if not raw: return {"found": False}
        
        # Immediate CC detection (best for short input like +20)
        detected_cc = None
        for i in range(4, 0, -1):
            prefix = raw[:i]
            if prefix.isdigit() and int(prefix) in phonenumbers.COUNTRY_CODE_TO_REGION_CODE:
                detected_cc = prefix
                break
        
        # Fallback to full parsing if it's a long number
        target_iso = 'XX'
        try:
            phone_p = phone if phone.startswith('+') else f"+{phone}"
            parsed = phonenumbers.parse(phone_p)
            detected_cc = str(parsed.country_code)
            target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
        except: pass

        if not detected_cc:
            return {"found": False}
            
        async with async_session() as session:
            from sqlalchemy import or_
            cc_clean = detected_cc.lstrip("+")
            cc_plus = "+" + cc_clean
            
            # 1. Custom User Price
            ucp = None
            if user_id > 0:
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_res = await session.execute(ucp_stmt)
                ucp_list = ucp_res.scalars().all()
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
            
            # 2. Global Price
            cp_stmt = select(CountryPrice).where(
                or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
            )
            cp_res = await session.execute(cp_stmt)
            cp_list = cp_res.scalars().all()
            cp = next((c for c in cp_list if c.iso_code == target_iso), 
                      next((c for c in cp_list if c.iso_code == 'XX'), 
                           (cp_list[0] if cp_list else None)))
            
            # Resolution
            price_val = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
            
            if price_val > 0:
                # Resolve Name & Flag
                display_iso = target_iso
                if display_iso == 'XX':
                    display_iso = phonenumbers.region_code_for_country_code(int(detected_cc))
                
                name = cp.country_name if cp else (ucp.country_name if hasattr(ucp, 'country_name') else "Requested Country")
                if not cp and ucp:
                    n, _, _ = resolve_country_info(detected_cc)
                    name = n if n != "Unknown" else f"Code {detected_cc}"

                return {
                    "found": True,
                    "name": name,
                    "iso": display_iso,
                    "flag": get_flag_emoji(display_iso) if display_iso != 'XX' else "🌐",
                    "price": price_val
                }
    except Exception as e:
        logger.error(f"Detection Error: {e}")
    return {"found": False}

@app.get("/api/seller/accounts")
async def get_seller_accounts(user_id: int, init_data: str, page: int = 1, limit: int = 10, status: str = "all"):
    from config import BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"accounts": [], "total_pages": 0, "current_page": 1, "total_count": 0}

    async with async_session() as session:
        offset = (page - 1) * limit
        
        # Build base filter
        base_filters = [Account.seller_id == user_id]
        if status != "all":
            if status == "pending":
                base_filters.append(Account.status == AccountStatus.PENDING)
            elif status == "accepted":
                # Sellers see SOLD as AVAILABLE/ACCEPTED
                base_filters.append(or_(Account.status == AccountStatus.AVAILABLE, Account.status == AccountStatus.SOLD))
            elif status == "rejected":
                base_filters.append(Account.status == AccountStatus.REJECTED)

        # Get total count for pagination
        count_stmt = select(func.count(Account.id)).where(*base_filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        
        stmt = select(Account).where(*base_filters).order_by(Account.id.desc()).offset(offset).limit(limit)
        results = (await session.execute(stmt)).scalars().all()
        accounts_data = []
        for a in results:
            # Detect reward price for seller
            actual_buy_price = 0
            approve_delay = 0
            flag = "🌐"
            try:
                parsed = phonenumbers.parse(a.phone_number)
                cc = str(parsed.country_code)
                region = phonenumbers.region_code_for_number(parsed)
                flag = get_flag_emoji(region)
                cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                if cp_row:
                    actual_buy_price = cp_row.buy_price
                    approve_delay = cp_row.approve_delay
            except: pass

            # Prefer locked values snapshotted at submission (immune to admin changes)
            actual_buy_price = a.locked_buy_price if a.locked_buy_price is not None else actual_buy_price
            approve_delay = a.locked_approve_delay if a.locked_approve_delay is not None else approve_delay

            ready_at = (a.created_at + timedelta(seconds=approve_delay)) if a.created_at else None

            # Mask SOLD status for seller to show as AVAILABLE (ACCEPTED)
            status_name = a.status.name
            if a.status == AccountStatus.SOLD:
                status_name = "AVAILABLE"

            accounts_data.append({
                "phone": a.phone_number,
                "status": status_name,
                "country": a.country,
                "flag": flag,
                "iso": region,
                "buy_price": actual_buy_price,
                "ready_at": int(ready_at.timestamp() * 1000) if ready_at else None,
                "date": a.created_at.isoformat() if a.created_at else None,
                "reject_reason": a.reject_reason
            })

        return {
            "accounts": accounts_data,
            "total_pages": total_pages,
            "current_page": page,
            "server_now": int(datetime.utcnow().timestamp() * 1000)
        }

@app.get("/api/admin/sourcing/history")
async def get_admin_sourcing_history(
    user_id: int,
    init_data: str,
    page: int = 1, 
    limit: int = 10, 
    filter: str = "PENDING",
    search: str = None
):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        offset = (page - 1) * limit
        base_stmt = select(Account)
        
        # 1. Status Filter (Bypassed if searching)
        is_searching = bool(search and search.strip())
        
        # 1. Status Filter
        if filter == "PENDING":
            base_stmt = base_stmt.where(Account.status == AccountStatus.PENDING)
        elif filter == "ACCEPTED":
            base_stmt = base_stmt.where(Account.status == AccountStatus.AVAILABLE)
        elif filter == "SOLD":
            base_stmt = base_stmt.where(Account.status == AccountStatus.SOLD)
        elif filter == "REJECTED":
            # REJECTED = all rejected except REVOKED (which has its own filter)
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason != "REVOKED")
        elif filter == "FROZEN":
            # FROZEN: banned/frozen accounts — explicitly exclude REVOKED
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%frozen%"), Account.reject_reason.ilike("%banned%"), Account.reject_reason.ilike("%company%")), Account.reject_reason != "REVOKED")
        elif filter == "SPAM":
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason.ilike("%spam%"))
        elif filter == "REVOKED":
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason == "REVOKED")
        
        # 2. Search Filter (Phone or ID)
        if is_searching:
            s = f"%{search.strip()}%"
            base_stmt = base_stmt.where(
                or_(
                    Account.phone_number.ilike(s),
                    cast(Account.seller_id, String).ilike(s),
                    Account.country.ilike(s)
                )
            )

        total_count = (await session.execute(
            select(func.count()).select_from(base_stmt.subquery())
        )).scalar() or 0
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        
        stmt = base_stmt.order_by(Account.id.desc()).offset(offset).limit(limit)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        import phonenumbers
        for a in results:
            flag = "🌐"
            approve_delay = 0
            price = 0
            try:
                parsed = phonenumbers.parse(a.phone_number)
                cc = str(parsed.country_code)
                region = phonenumbers.region_code_for_number(parsed)
                # Helper to get flag emoji (if not available, we use Globe)
                try:
                    flag = "".join(chr(127397 + ord(c)) for c in region)
                except: pass

                cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                if cp_row:
                    price = cp_row.buy_price
                    approve_delay = cp_row.approve_delay
            except: pass

            # Prefer locked values snapshotted at submission (immune to admin changes)
            price = a.locked_buy_price if a.locked_buy_price is not None else price
            approve_delay = a.locked_approve_delay if a.locked_approve_delay is not None else approve_delay

            ready_at = (a.created_at + timedelta(seconds=approve_delay)) if a.created_at else None

            history.append({
                "id": a.id,
                "phone": a.phone_number,
                "country": f"{flag} {a.country}",
                "buy_price": price,
                "status": a.status.name,
                "seller_id": a.seller_id,
                "two_fa_password": a.two_fa_password,
                "ready_at": int(ready_at.timestamp() * 1000) if ready_at else None,
                "date": a.created_at.isoformat() if a.created_at else None,
                "reject_reason": a.reject_reason,
                "is_available": a.status == AccountStatus.AVAILABLE
            })
            
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "server_now": int(datetime.utcnow().timestamp() * 1000)
        }

@app.get("/api/admin/sourcing/account/{phone}/code")
async def get_account_otp(phone: str, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        account = (await session.execute(select(Account).where(Account.phone_number == phone))).scalar()
        if not account:
            return {"success": False, "error": "ACCOUNT_NOT_FOUND"}
        if not account.session_string:
            return {"success": False, "error": "SESSION_NOT_FOUND"}
            
        try:
            from services.session_manager import get_telegram_login_code
            code = await get_telegram_login_code(account.session_string)
            if code:
                return {"success": True, "code": code}
            else:
                return {"success": False, "error": "NO_CODE_RECEIVED"}
        except Exception as e:
            err_str = str(e)
            if "SESSION_REVOKED" in err_str:
                return {"success": False, "error": "SESSION_NOT_FOUND"}
            return {"success": False, "error": err_str}

@app.delete("/api/admin/sourcing/account/{phone}")
async def revoke_sourcing_account(phone: str, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        account = (await session.execute(select(Account).where(Account.phone_number == phone))).scalar()
        if not account:
            return {"success": False, "error": "Account not found"}
            
        # 1. Terminate Bot Session from the Telegram Account
        if account.session_string:
            try:
                from services.session_manager import create_client
                client = await create_client(account.session_string)
                await client.connect()
                await client.log_out() # Permanently kills the bot's session
                await client.disconnect()
            except Exception as e:
                logger.warning(f"Failed to log out session for {phone} during revocation: {e}")

        # 2. Update Status to REJECTED (REVOKED) instead of deleting
        account.status = AccountStatus.REJECTED
        account.reject_reason = "REVOKED"
        await session.commit()
        
        return {"success": True, "message": "Account revoked and session terminated."}

@app.post("/api/admin/user/sync")
async def sync_user_identity(data: UserSync):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:    
        # 1. Select the correct bot based on bot_type
        bot = app.state.bot if data.bot_type == "store" else app.state.bot
        
        if not bot:
            raise HTTPException(status_code=500, detail="Bot instance not found for sync")
            
        # 2. Fetch latest data from Telegram
        chat = await bot.get_chat(data.user_id_target)
        
        # 3. Format name and username
        new_full_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "N/A"
        new_username = chat.username or None
        
        # 4. Update Database
        async with async_session() as session:
            user = await session.get(User, data.user_id_target)
            if user:
                user.full_name = new_full_name
                user.username = new_username
                await session.commit()
                
                return {
                    "status": "success",
                    "full_name": new_full_name,
                    "username": f"@{new_username}" if new_username else "N/A"
                }
        
        raise HTTPException(status_code=404, detail="User not found in database")
        
    except Exception as e:
        logger.error(f"Identity Sync Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Settings Management ---
@app.post("/api/admin/system/settings")
async def save_system_settings(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        for key, value in data.items():
            if key in ["user_id", "init_data"]:
                continue
            stmt = select(AppSetting).where(AppSetting.key == key)
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            
            if obj:
                obj.value = str(value)
            else:
                session.add(AppSetting(key=key, value=str(value)))
        
        await session.commit()
        
        # Immediately apply extra_admin_ids to memory
        if "extra_admin_ids" in data:
            import config, os
            base_admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
            # config.STORE_ADMIN_IDS removed (store bot deleted)
            # store admin IDs removed
            value = data.get("extra_admin_ids")
            if value and isinstance(value, str):
                for eid in value.split(","):
                    if eid.strip().isdigit():
                        parsed_id = int(eid.strip())
                        if False:  # store admin IDs removed
                            pass  # store admin IDs removed
            # STORE_ADMIN_IDS removed

        return {"status": "success"}

@app.get("/api/admin/subscription-channels")
async def get_subscription_channels(user_id: int, init_data: str, bot_type: str = "store"):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        result = await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.bot_type == bot_type))
        channels = result.scalars().all()
        return [{"id": c.id, "bot_type": c.bot_type, "username": c.username, "link": c.link} for c in channels]

@app.post("/api/user/settings")
async def update_user_settings(data: dict):
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    lang = data.get("language")
    
    if not verify_user_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    async with async_session() as session:
        user = await session.get(User, u_id)
        if user:
            user.language = lang
            await session.commit()
            return {"ok": True}
        raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/subscription-channels")
async def add_subscription_channel(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    bot_type = data.get("bot_type", "store")
    username = data.get("username")
    link = data.get("link")
    if not username or not link:
        return {"ok": False, "error": "Username and Link are required"}
    
    async with async_session() as session:
        new_channel = SubscriptionChannel(bot_type=bot_type, username=username, link=link)
        session.add(new_channel)
        await session.commit()
        await session.refresh(new_channel)
        return {"ok": True, "id": new_channel.id}

@app.delete("/api/admin/subscription-channels/{channel_id}")
async def delete_subscription_channel(channel_id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        channel = await session.get(SubscriptionChannel, channel_id)
        if channel:
            await session.delete(channel)
            await session.commit()
        return {"ok": True}

# ─── TESTING / RESET ENDPOINTS ───────────────────────────────────────────────

@app.get("/api/admin/test/clear-deposits")
async def test_clear_deposits():
    """[TESTING] Clear all DEPOSIT transactions."""
    async with async_session() as session:
        txn_count = (await session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.type == TransactionType.DEPOSIT
            )
        )).scalar() or 0

        await session.execute(
            delete(Transaction).where(Transaction.type == TransactionType.DEPOSIT)
        )
        await session.commit()

    return {
        "status": "success",
        "transactions_cleared": txn_count,
        "message": f"Cleared {deposit_count} deposits."
    }


@app.get("/api/admin/test/clear-sold-accounts")
async def test_clear_sold_accounts():
    """[TESTING] Permanently DELETE all SOLD accounts from DB."""
    async with async_session() as session:
        sold_count = (await session.execute(
            select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD)
        )).scalar() or 0

        await session.execute(
            delete(Account).where(Account.status == AccountStatus.SOLD)
        )
        await session.commit()

    return {
        "status": "success",
        "accounts_deleted": sold_count,
        "message": f"Permanently deleted {sold_count} SOLD accounts from the database."
    }


@app.get("/api/admin/test/delete-account")
async def test_delete_account(phone: str):
    """[TESTING] Permanently delete a single account by phone number."""
    async with async_session() as session:
        result = await session.execute(
            select(Account).where(Account.phone_number == phone)
        )
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail=f"No account found with phone: {phone}")

        account_id   = account.id
        phone_stored = account.phone_number
        status_val   = account.status.value

        await session.delete(account)
        await session.commit()

    return {
        "status": "success",
        "deleted_account_id": account_id,
        "phone_number": phone_stored,
        "was_status": status_val,
        "message": f"Account {phone_stored} permanently deleted."
    }

# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/check-subscription")
async def check_subscription(user_id: int, bot_type: str = "store"):
    from config import BOT_TOKEN, ADMIN_IDS
    
    # Admins bypass check
    if user_id in ADMIN_IDS:
        return {"ok": True}
        
    token = BOT_TOKEN
    
    async with async_session() as session:
        result = await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.bot_type == bot_type))
        channels = result.scalars().all()
        
    if not channels:
        return {"ok": True}
        
    not_subscribed = []
    for ch in channels:
        try:
            # Telegram API check
            chat_id = ch.username
            api_url = f"https://api.telegram.org/bot{token}/getChatMember?chat_id={chat_id}&user_id={user_id}"
            
            def do_check():
                try:
                    r = requests.get(api_url, timeout=5)
                    return r.json()
                except: return None
                
            data = await asyncio.to_thread(do_check)
            
            if not data or not data.get("ok"):
                # If bot is not admin or channel not found, we might want to skip or block. 
                # To be safe and avoid locking out everyone on misconfig, we skip errors for now.
                # But if data.ok is false, it usually means the bot can't see the member.
                continue
                
            status = data["result"]["status"]
            if status in ["left", "kicked"]:
                not_subscribed.append({"username": ch.username, "link": ch.link})
        except Exception as e:
            logger.error(f"Error checking sub for {ch.username}: {e}")
            continue
            
    if not_subscribed:
        return {"ok": False, "channels": not_subscribed}
        
    return {"ok": True}
