import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config_manager import config
from bot.proxy_connection import ProxyAiohttpSession, mask_proxy_url, select_active_proxy
from bot.handlers import (
    samples_admin,
    topic_links,
    transfer,
    admin_config,
    mod_actions,
    user_submit,
)


async def create_bot() -> Bot:
    active_proxy = await select_active_proxy()
    if active_proxy:
        try:
            session = ProxyAiohttpSession(proxy=active_proxy["url"])
            logging.info("Using Telegram proxy %s", mask_proxy_url(active_proxy["url"]))
            return Bot(token=config.data["bot_token"], session=session)
        except Exception as error:
            logging.warning(
                "Could not initialize the Telegram proxy session (%s); using a direct connection.",
                type(error).__name__,
            )
    return Bot(token=config.data["bot_token"])


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    bot = await create_bot()
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_routers(
        samples_admin.router,
        transfer.router,
        topic_links.router,
        admin_config.router,
        mod_actions.router,
        user_submit.router
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
