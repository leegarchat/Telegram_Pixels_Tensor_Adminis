"""Telegram bot entry point — creates the Bot/Dispatcher and starts long polling."""
# Standard library: asyncio drives the event loop, logging reports proxy/startup status.
import asyncio
import logging
# aiogram v3: Bot wraps the Telegram Bot API, Dispatcher routes updates,
# MemoryStorage keeps FSM state in RAM (lost on restart — see handlers for implications).
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

# Local configuration singleton (loads config.json on import).
from bot.config_manager import config
# Proxy helpers: pick a working proxy (if any) and mask credentials for logs.
from bot.proxy_connection import ProxyAiohttpSession, mask_proxy_url, select_active_proxy
# Feature routers — each module registers its own aiogram Router.
from bot.handlers import (
    samples_admin,
    topic_links,
    transfer,
    admin_config,
    mod_actions,
    user_submit,
)


async def create_bot() -> Bot:
    """Create a Bot instance, using a Telegram proxy session when one is available.

    Returns:
        Configured aiogram Bot. Falls back to a direct connection if the proxy
        session cannot be constructed for any reason.
    """
    # Ask the proxy subsystem for the first reachable proxy (TCP + getMe checks inside).
    active_proxy = await select_active_proxy()
    if active_proxy:
        try:
            # ProxyAiohttpSession disables SSL verification like the companion service,
            # which is required in environments with MITM / restricted TLS.
            session = ProxyAiohttpSession(proxy=active_proxy["url"])
            # Never log the full URL (it may contain user:password); mask it instead.
            logging.info("Using Telegram proxy %s", mask_proxy_url(active_proxy["url"]))
            return Bot(token=config.data["bot_token"], session=session)
        except Exception as error:
            # Any failure here must not prevent startup — continue without a proxy.
            logging.warning(
                "Could not initialize the Telegram proxy session (%s); using a direct connection.",
                type(error).__name__,
            )
    # No proxy configured, none reachable, or session creation failed.
    return Bot(token=config.data["bot_token"])


async def main():
    """Configure logging, wire routers, and run polling until interrupted."""
    # Human-readable log format with timestamps; INFO shows proxy selection and aiogram events.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    # Build bot (with optional proxy) and dispatcher with in-memory FSM storage.
    bot = await create_bot()
    dp = Dispatcher(storage=MemoryStorage())

    # Router order matters only for overlapping filters; here each router has
    # distinct chat-type / topic filters, so the order is mostly organizational.
    dp.include_routers(
        samples_admin.router,  # /new_sample, /sample_list, etc. inside mod-group topics
        transfer.router,       # import/export of config, samples, full ZIP backups
        topic_links.router,    # /link, /unlink, /get_links, /help
        admin_config.router,   # superadmin-only /cfg_* commands
        mod_actions.router,    # approve/reject buttons and /approve, /cancel, /new, /change, /remove
        user_submit.router     # private-chat submission wizard (/start flow)
    )

    try:
        # Drop queued updates on restart so old button presses do not fire unexpectedly.
        await bot.delete_webhook(drop_pending_updates=True)
        # Blocking long-poll loop; handles SIGINT/SIGTERM gracefully via aiogram.
        await dp.start_polling(bot)
    finally:
        # Always close the underlying aiohttp session to avoid "unclosed session" warnings.
        await bot.session.close()


# Standard asyncio entry point: `python main.py` starts the bot.
if __name__ == "__main__":
    asyncio.run(main())
