import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from aiogram.client.session.aiohttp import AiohttpSession

from bot.config_manager import config

log = logging.getLogger(__name__)
ACTIVE_PROXY: dict | None = None


class ProxyAiohttpSession(AiohttpSession):
    """Create proxy connectors with the same unverified TLS behavior as svc."""

    async def create_session(self):
        previous_ssl = self._connector_init.get("ssl")
        self._connector_init["ssl"] = False
        try:
            return await super().create_session()
        finally:
            self._connector_init["ssl"] = previous_ssl


def proxy_json_candidates() -> list[Path]:
    configured_path = (
        os.environ.get("PROXY_JSON")
        or config.data.get("PROXY_JSON")
        or config.data.get("proxy_json")
        or ""
    )
    candidates = []
    if str(configured_path).strip():
        candidates.append(Path(str(configured_path).strip()).expanduser())

    # The companion service bot may define the shared proxy path in ~/bot/config.json.
    companion_config = Path.home() / "bot" / "config.json"
    if companion_config.is_file():
        try:
            companion_data = json.loads(companion_config.read_text(encoding="utf-8"))
            if isinstance(companion_data, dict):
                companion_proxy_path = next(
                    (value for key, value in companion_data.items()
                     if str(key).upper() == "PROXY_JSON" and value),
                    None,
                )
                if companion_proxy_path:
                    candidates.append(Path(str(companion_proxy_path)).expanduser())
        except (OSError, json.JSONDecodeError):
            pass

    project_root = Path(__file__).resolve().parent.parent
    candidates.extend([
        project_root / "proxy.json",
        project_root / "proxy" / "proxy.json",
        Path.home() / "proxy" / "proxy.json",
    ])

    unique = []
    seen = set()
    for candidate in candidates:
        normalized = candidate.resolve()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def find_proxy_json() -> Path | None:
    return next((path for path in proxy_json_candidates() if path.is_file()), None)


def _proxy_entries(items) -> list[dict]:
    if not isinstance(items, list):
        return []
    proxies = []
    for entry in items:
        if isinstance(entry, str):
            proxies.append({"id": len(proxies) + 1, "url": entry.strip(), "enabled": True})
        elif isinstance(entry, dict) and entry.get("url"):
            proxies.append({
                "id": int(entry.get("id", len(proxies) + 1)),
                "name": str(entry.get("name", f"proxy-{len(proxies) + 1}")),
                "url": str(entry["url"]).strip(),
                "enabled": bool(entry.get("enabled", True)),
            })
    return proxies


def load_proxies() -> tuple[Path | None, list[dict]]:
    """Load configured proxies, then fall back to the project's proxy.txt."""
    proxy_json = find_proxy_json()
    if proxy_json:
        try:
            data = json.loads(proxy_json.read_text(encoding="utf-8"))
            items = data.get("proxies", []) if isinstance(data, dict) else data
            proxies = _proxy_entries(items)
            if proxies:
                return proxy_json, proxies
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning("Could not read proxy configuration at %s: %s", proxy_json, error)

    proxy_txt = Path(__file__).resolve().parent.parent / "proxy.txt"
    try:
        if proxy_txt.is_file():
            for line in proxy_txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return None, [{"id": 1, "name": "proxy.txt:1", "url": line, "enabled": True}]
    except OSError as error:
        log.warning("Could not read proxy fallback %s: %s", proxy_txt, error)
    return proxy_json, []


def parse_proxy_url(proxy_url: str) -> tuple[str, str, str, str, int]:
    parsed = urlparse(proxy_url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks5h", "http", "https"}:
        raise ValueError("Supported proxy schemes: socks5, socks5h, http, https.")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Proxy URL must include a host and port.")
    return scheme, parsed.username or "", parsed.password or "", parsed.hostname, parsed.port


def mask_proxy_url(proxy_url: str) -> str:
    try:
        parsed = urlparse(proxy_url)
        return f"{parsed.scheme}://{parsed.hostname or '?'}:{parsed.port or '?'}"
    except ValueError:
        return "<invalid proxy URL>"


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> tuple[bool, float]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - started) * 1000
    except OSError:
        return False, (time.monotonic() - started) * 1000


async def validate_via_telegram(proxy_url: str, timeout: float = 12.0) -> bool:
    """Confirm the proxy can reach Telegram's getMe endpoint for this bot."""
    import aiohttp

    token = config.data.get("bot_token")
    if not token:
        return False
    api_url = f"https://api.telegram.org/bot{token}/getMe"
    scheme = urlparse(proxy_url).scheme.lower()
    try:
        if scheme in {"socks5", "socks5h"}:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy_url, rdns=True, ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    payload = await response.json(content_type=None)
                    return response.status == 200 and payload.get("ok") is True

        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as response:
                payload = await response.json(content_type=None)
                return response.status == 200 and payload.get("ok") is True
    except Exception as error:
        log.debug("Proxy validation failed (%s).", type(error).__name__)
        return False


async def pick_first_valid(proxies: list[dict]) -> dict | None:
    """Select the first enabled proxy that passes TCP and Telegram checks."""
    for proxy in proxies:
        if not proxy.get("enabled", True):
            continue
        try:
            _, _, _, host, port = parse_proxy_url(proxy["url"])
        except (KeyError, TypeError, ValueError) as error:
            log.warning("Skipping invalid proxy #%s: %s", proxy.get("id", "?"), error)
            continue

        reachable, latency_ms = await asyncio.to_thread(_tcp_ping, host, port)
        if not reachable:
            log.info("Proxy #%s is unreachable; trying the next proxy.", proxy.get("id", "?"))
            continue
        if not await validate_via_telegram(proxy["url"]):
            log.info("Proxy #%s cannot reach Telegram; trying the next proxy.", proxy.get("id", "?"))
            continue

        log.info(
            "Selected Telegram proxy #%s at %s (TCP %.0f ms).",
            proxy.get("id", "?"),
            mask_proxy_url(proxy["url"]),
            latency_ms,
        )
        return proxy
    return None


async def select_active_proxy() -> dict | None:
    global ACTIVE_PROXY
    source, proxies = load_proxies()
    if not proxies:
        ACTIVE_PROXY = None
        log.info("No proxy configured%s; connecting directly to Telegram.", f" in {source}" if source else "")
        return None

    ACTIVE_PROXY = await pick_first_valid(proxies)
    if ACTIVE_PROXY is None:
        log.warning("No working Telegram proxy found; falling back to a direct connection.")
    return ACTIVE_PROXY
