from aiogram import Router
from .start import router as start_router
from .admin import router as admin_router

seller_router = Router()
seller_router.include_router(start_router)
seller_router.include_router(admin_router)
