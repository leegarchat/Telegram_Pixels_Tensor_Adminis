import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config_manager import config
from bot.handlers import user_submit, mod_actions, admin_config


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    bot = Bot(token=config.data["bot_token"])
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_routers(
        admin_config.router,
        mod_actions.router,
        user_submit.router
    )

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())