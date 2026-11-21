"""
Модуль конфигурации бота и парсинга аргументов командной строки.
Поддерживает запуск с аргументами CLI и fallback на .env / переменные окружения.
"""

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    allowed_users: List[int] = field(default_factory=list)
    proxy_type: str = "auto"  # 'auto', 'mtproto', 'socks5', 'none'
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080
    proxy_secret: Optional[str] = None
    workdir: str = field(default_factory=lambda: os.path.expanduser("~"))
    session_name: str = "bot_session"
    log_level: str = "INFO"
    log_file: Optional[str] = None


def parse_users_list(val: str) -> List[int]:
    """Преобразует строку с ID пользователей (через пробел или запятую) в список целых чисел."""
    if not val:
        return []
    users = []
    # Заменяем запятые на пробелы и разбиваем
    cleaned = val.replace(",", " ").split()
    for item in cleaned:
        item = item.strip()
        if item:
            try:
                users.append(int(item))
            except ValueError:
                raise argparse.ArgumentTypeError(f"Некорректный ID пользователя: '{item}' (ожидалось число)")
    return users


def load_config() -> Config:
    """Парсит аргументы командной строки и переменные окружения."""
    parser = argparse.ArgumentParser(
        description="Telegram SSH & Shell Bot для Termux на базе Telethon (с поддержкой tg-ws-proxy)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-t", "--token",
        dest="bot_token",
        default=os.getenv("BOT_TOKEN"),
        help="Токен Telegram-бота (от @BotFather)",
    )
    parser.add_argument(
        "-i", "--api-id",
        dest="api_id",
        type=int,
        default=int(os.getenv("API_ID", "0")) or None,
        help="Telegram API ID (с my.telegram.org)",
    )
    parser.add_argument(
        "-H", "--api-hash",
        dest="api_hash",
        default=os.getenv("API_HASH"),
        help="Telegram API Hash (с my.telegram.org)",
    )
    parser.add_argument(
        "-u", "--users",
        dest="users",
        nargs="*",
        default=[],
        help="Список разрешенных Telegram User ID (через пробел или запятую)",
    )
    parser.add_argument(
        "-s", "--secret",
        dest="proxy_secret",
        default=os.getenv("PROXY_SECRET"),
        help="Секрет MTProto-прокси (для tg-ws-proxy)",
    )
    parser.add_argument(
        "--proxy-host",
        dest="proxy_host",
        default=os.getenv("PROXY_HOST", "127.0.0.1"),
        help="Хост прокси-сервера",
    )
    parser.add_argument(
        "-p", "--proxy-port",
        dest="proxy_port",
        type=int,
        default=int(os.getenv("PROXY_PORT", "1080")),
        help="Порт прокси-сервера",
    )
    parser.add_argument(
        "--proxy-type",
        dest="proxy_type",
        choices=["auto", "mtproto", "socks5", "none"],
        default=os.getenv("PROXY_TYPE", "auto"),
        help="Тип прокси: auto (определяется наличием --secret), mtproto, socks5 или none",
    )
    parser.add_argument(
        "-w", "--workdir",
        dest="workdir",
        default=os.getenv("WORKDIR", str(Path.home())),
        help="Рабочая директория по умолчанию",
    )
    parser.add_argument(
        "--session",
        dest="session_name",
        default=os.getenv("SESSION_NAME", "bot_session"),
        help="Имя файла сессии Telethon",
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Уровень логирования (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=os.getenv("LOG_FILE"),
        help="Путь к файлу для записи логов (по умолчанию лог пишется только в stdout)",
    )

    args = parser.parse_args()

    # Валидация обязательных параметров
    if not args.bot_token:
        parser.error("Не указан токен бота! Передайте --token или установите BOT_TOKEN в .env")
    if not args.api_id:
        parser.error("Не указан API ID! Передайте --api-id или установите API_ID в .env")
    if not args.api_hash:
        parser.error("Не указан API Hash! Передайте --api-hash или установите API_HASH в .env")

    # Обработка списка пользователей
    allowed_users: List[int] = []
    if args.users:
        if isinstance(args.users, list):
            for u in args.users:
                allowed_users.extend(parse_users_list(str(u)))
        else:
            allowed_users.extend(parse_users_list(str(args.users)))
    else:
        env_users = os.getenv("ALLOWED_USERS", "")
        if env_users:
            allowed_users.extend(parse_users_list(env_users))

    # Определение типа прокси при auto
    proxy_type = args.proxy_type
    if proxy_type == "auto":
        if args.proxy_secret:
            proxy_type = "mtproto"
        elif args.proxy_host and args.proxy_port and args.proxy_port != 1080:
            proxy_type = "socks5"
        else:
            proxy_type = "none"

    return Config(
        bot_token=args.bot_token,
        api_id=args.api_id,
        api_hash=args.api_hash,
        allowed_users=allowed_users,
        proxy_type=proxy_type,
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
        proxy_secret=args.proxy_secret,
        workdir=os.path.abspath(os.path.expanduser(args.workdir)),
        session_name=args.session_name,
        log_level=args.log_level,
        log_file=args.log_file,
    )
