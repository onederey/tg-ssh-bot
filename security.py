"""
Модуль контроля доступа (whitelist) и управления видимостью подсказок команд (BotCommandScope).
"""

import functools
import logging
from typing import Callable, List, Optional
from telethon import TelegramClient
from telethon.events import StopPropagation
from telethon.tl.functions.bots import SetBotCommandsRequest, ResetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopePeer,
)

logger = logging.getLogger("tg_ssh_bot.security")

BOT_COMMANDS = [
    BotCommand(command="run", description="Запустить shell-команду"),
    BotCommand(command="pwd", description="Текущая директория"),
    BotCommand(command="cd", description="Сменить директорию (/cd <путь>)"),
    BotCommand(command="ls", description="Список файлов (/ls [путь])"),
    BotCommand(command="get", description="Скачать файл (/get <путь>)"),
    BotCommand(command="fav", description="Избранные команды"),
    BotCommand(command="kill", description="Прервать выполняющийся процесс"),
    BotCommand(command="help", description="Справка по возможностям бота"),
]


def is_authorized(user_id: Optional[int], allowed_users: List[int]) -> bool:
    """
    Проверяет, авторизован ли пользователь.
    Если список allowed_users пуст, доступ блокируется для всех ради безопасности.
    """
    if not user_id:
        return False
    if not allowed_users:
        logger.warning(f"Попытка доступа от {user_id}, но список --users пуст! Доступ заблокирован.")
        return False
    return user_id in allowed_users


def require_auth(allowed_users: List[int]):
    """
    Декоратор для обработчиков событий Telethon.
    - Проверяет, что сообщение получено в личном чате (private chat). В группах/каналах бот молчит.
    - Проверяет, что отправитель есть в белом списке (allowed_users).
    Если пользователь не авторизован — полностью игнорирует событие (тихий дроп, без ответа).
    """
    def decorator(handler: Callable):
        @functools.wraps(handler)
        async def wrapper(event, *args, **kwargs):
            # Защита от утечки в публичные/групповые чаты
            if not getattr(event, "is_private", False):
                raise StopPropagation

            sender_id = event.sender_id
            if not is_authorized(sender_id, allowed_users):
                logger.info(f"Игнорировано сообщение от неавторизованного пользователя ID: {sender_id}")
                raise StopPropagation  # Прерывает дальнейшую обработку события
            return await handler(event, *args, **kwargs)
        return wrapper
    return decorator


async def configure_bot_command_scopes(client: TelegramClient, allowed_users: List[int]):
    """
    Настраивает видимость команд:
    - Для DefaultScope (всех остальных пользователей) команды очищаются (пустой список).
    - Для доверенных пользователей из allowed_users меню команд регистрируется индивидуально.
    """
    logger.info("Настройка BotCommandScope: скрытие команд для публики...")
    try:
        # Очищаем глобальный список команд
        await client(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[],
        ))
        logger.info("Глобальные подсказки команд успешно скрыты.")
    except Exception as e:
        logger.warning(f"Не удалось очистить глобальный BotCommandScopeDefault: {e}")

    # Регистрируем меню команд персонально для каждого разрешенного пользователя
    for user_id in allowed_users:
        try:
            peer = await client.get_input_entity(user_id)
            await client(SetBotCommandsRequest(
                scope=BotCommandScopePeer(peer=peer),
                lang_code="",
                commands=BOT_COMMANDS,
            ))
            logger.info(f"Команды успешно настроены для пользователя {user_id}")
        except Exception as e:
            logger.warning(
                f"Не удалось настроить персональные команды для {user_id} (возможно, пользователь ещё не начинал диалог с ботом): {e}"
            )
