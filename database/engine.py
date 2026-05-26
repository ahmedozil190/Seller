import os
import shutil
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from database.models import Base
from config import DATABASE_URL

# Auto-migrate local DB to persistent volume if needed
if os.path.exists("/data") and not os.path.exists("/data/app.db"):
    if os.path.exists("app.db"):
        try:
            shutil.copy2("app.db", "/data/app.db")
            print("Successfully migrated app.db to /data/app.db")
        except Exception as e:
            print(f"Migration failed: {e}")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

from sqlalchemy import text, select
from database.models import ApiServer

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
        # Auto-migration: Check columns for various tables
        try:
            # 1. withdrawal_requests.transaction_id
            def check_withdraw_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(withdrawal_requests)"))
                return [row[1] for row in cursor]
            
            w_cols = await conn.run_sync(check_withdraw_cols)
            if 'transaction_id' not in w_cols:
                await conn.execute(text("ALTER TABLE withdrawal_requests ADD COLUMN transaction_id VARCHAR(12)"))
                print("Successfully added transaction_id column to withdrawal_requests")
            
            # 2. deposits.method
            def check_deposit_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(deposits)"))
                return [row[1] for row in cursor]
            
            d_cols = await conn.run_sync(check_deposit_cols)
            if 'method' not in d_cols:
                await conn.execute(text("ALTER TABLE deposits ADD COLUMN method VARCHAR(50)"))
                print("Successfully added method column to deposits")

            # 3. accounts.server_id & hash_code
            def check_account_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(accounts)"))
                return [row[1] for row in cursor]
            
            a_cols = await conn.run_sync(check_account_cols)
            if 'server_id' not in a_cols:
                await conn.execute(text("ALTER TABLE accounts ADD COLUMN server_id INTEGER"))
                print("Successfully added server_id column to accounts")
            if 'hash_code' not in a_cols:
                await conn.execute(text("ALTER TABLE accounts ADD COLUMN hash_code TEXT"))
                print("Successfully added hash_code column to accounts")

            # 4. api_servers.server_type & extra_id
            def check_srv_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(api_servers)"))
                return [row[1] for row in cursor]
            
            s_cols = await conn.run_sync(check_srv_cols)
            if 'server_type' not in s_cols:
                await conn.execute(text("ALTER TABLE api_servers ADD COLUMN server_type VARCHAR(20) DEFAULT 'standard'"))
                print("Successfully added server_type column to api_servers")
            if 'extra_id' not in s_cols:
                await conn.execute(text("ALTER TABLE api_servers ADD COLUMN extra_id VARCHAR(100)"))
                print("Successfully added extra_id column to api_servers")

            # 5. country_prices.log_quantity
            def check_cp_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(country_prices)"))
                return [row[1] for row in cursor]
            
            cp_cols = await conn.run_sync(check_cp_cols)
            if 'log_quantity' not in cp_cols:
                await conn.execute(text("ALTER TABLE country_prices ADD COLUMN log_quantity INTEGER DEFAULT 1000"))
                print("Successfully added log_quantity column to country_prices")

            # 6. Ensure subscription_channels table exists (Base.metadata.create_all handles this usually, but init_db runs it)
            # 7. subscription_channels.bot_type
            def check_sub_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(subscription_channels)"))
                return [row[1] for row in cursor]
            
            # Catch errors in case table doesn't exist yet during the first migration check
            try:
                sub_cols = await conn.run_sync(check_sub_cols)
                if 'bot_type' not in sub_cols:
                    await conn.execute(text("ALTER TABLE subscription_channels ADD COLUMN bot_type VARCHAR DEFAULT 'store'"))
                    print("Successfully added bot_type column to subscription_channels")
            except Exception:
                pass

            # 8. users.refer_count & referral_bonus_awarded
            def check_user_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(users)"))
                return [row[1] for row in cursor]
            
            try:
                u_cols = await conn.run_sync(check_user_cols)
                if 'refer_count' not in u_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN refer_count INTEGER DEFAULT 0"))
                    print("Successfully added refer_count column to users")
                if 'referral_bonus_awarded' not in u_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN referral_bonus_awarded BOOLEAN DEFAULT 0"))
                    print("Successfully added referral_bonus_awarded column to users")
            except Exception:
                pass
                
        except Exception as e:
            print(f"Migration check failed: {e}")
