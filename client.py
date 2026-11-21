"""
Модуль инициализации и подключения клиента Telethon с поддержкой tg-ws-proxy (MTProto / SOCKS5).
"""

import logging
from telethon import TelegramClient, connection
from config import Config

logger = logging.getLogger("tg_ssh_bot.client")


def create_telegram_client(config: Config) -> TelegramClient:
    """
    Создает экземпляр TelegramClient с корректными настройками прокси для работы через tg-ws-proxy.
    """
    proxy_settings = None
    conn_type = None

    if config.proxy_type == "mtproto":
        if not config.proxy_secret:
            raise ValueError("Для MTProto-прокси необходимо указать секрет (--secret)!")
        
        logger.info(
            f"Инициализация MTProto прокси: {config.proxy_host}:{config.proxy_port} (секрет: {config.proxy_secret[:6]}...)"
        )
        # В Telethon для MTProxy кортеж имеет вид (host, port, secret)
        proxy_settings = (config.proxy_host, config.proxy_port, config.proxy_secret)
        # ConnectionTcpMTProxyAbridged или ConnectionTcpMTProxyRandomizedIntermediate
        # Большинство tg-ws-proxy совместимы с ConnectionTcpMTProxyAbridged или RandomizedIntermediate
        conn_type = connection.ConnectionTcpMTProxyAbridged

    elif config.proxy_type == "socks5":
        try:
            import socks
        except ImportError:
            raise ImportError("Для работы через SOCKS5 необходимо установить PySocks: pip install PySocks")

        logger.info(f"Инициализация SOCKS5 прокси: {config.proxy_host}:{config.proxy_port}")
        proxy_settings = (socks.SOCKS5, config.proxy_host, config.proxy_port)

    else:
        logger.info("Подключение напрямую к Telegram (без прокси)")

    kwargs = {
        "session": config.session_name,
        "api_id": config.api_id,
        "api_hash": config.api_hash,
    }

    if conn_type:
        kwargs["connection"] = conn_type
    if proxy_settings:
        kwargs["proxy"] = proxy_settings

    client = TelegramClient(**kwargs)
    return client
