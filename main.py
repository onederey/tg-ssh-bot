"""
Главный модуль запуска Telegram-бота для Termux / SSH на базе Telethon.
"""

import asyncio
import html
import logging
import os
import sys
from telethon import events
from telethon.tl.custom import Button

from client import create_telegram_client
from config import load_config
from executor import CommandExecutor
from favorites import FavoritesManager
from file_manager import FileManager
from security import configure_bot_command_scopes, is_authorized, require_auth

# Настройка логирования
def setup_logging(log_level: str = "INFO", log_file: str = None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
        level=level,
        handlers=handlers,
        force=True,
    )

logger = logging.getLogger("tg_ssh_bot")


async def main():
    config = load_config()
    setup_logging(log_level=config.log_level, log_file=config.log_file)

    logger.info("Конфигурация успешно загружена.")
    logger.info(f"Разрешенные пользователи (Whitelist): {config.allowed_users or 'НЕТ (Бот недоступен)'}")
    logger.info(f"Тип подключения: {config.proxy_type} (Хост: {config.proxy_host}:{config.proxy_port})")

    # Инициализация Telethon клиента
    client = create_telegram_client(config)

    # Инициализация сервисов
    executor = CommandExecutor(client=client, default_workdir=config.workdir)
    file_manager = FileManager(client=client)
    favorites_manager = FavoritesManager(storage_path=os.path.join(config.workdir, "favorites.json"))

    # Декоратор проверки авторизации для текущего списка пользователей
    auth_check = require_auth(config.allowed_users)

    # --- Обработчики команд ---

    @client.on(events.NewMessage(pattern=r"^/(start|help)$"))
    @auth_check
    async def cmd_help(event):
        workdir = executor.get_workdir(event.sender_id)
        text = (
            "🤖 <b>Termux SSH / Shell Bot</b>\n\n"
            "Бот готов к выполнению команд на вашем сервере.\n\n"
            f"📂 <b>Текущая директория:</b> <code>{html.escape(workdir)}</code>\n\n"
            "📌 <b>Основные возможности:</b>\n"
            "• Просто отправьте любую команду (например, <code>uname -a</code> или <code>df -h</code>), чтобы выполнить ее.\n"
            "• <code>/cd &lt;директория&gt;</code> — сменить текущую директорию\n"
            "• <code>/pwd</code> — показать текущую директорию\n"
            "• <code>/ls [директория]</code> — список файлов\n"
            "• <code>/get &lt;файл&gt;</code> — скачать файл с сервера (до 2 ГБ)\n"
            "• Отправьте любой документ боту, чтобы загрузить его на сервер\n"
            "• <code>/kill</code> — принудительно остановить текущий запущенный процесс\n"
            "• <code>/fav</code> — панель избранных команд (с инлайн-кнопками)\n"
            "• <code>/fav add &lt;имя&gt; &lt;команда&gt;</code> — добавить в избранное\n"
            "• <code>/fav del &lt;имя&gt;</code> — удалить из избранного\n"
        )
        await event.reply(text, parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/pwd$"))
    @auth_check
    async def cmd_pwd(event):
        workdir = executor.get_workdir(event.sender_id)
        await event.reply(f"📂 <b>Текущая директория:</b>\n<code>{html.escape(workdir)}</code>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/cd(?:\s+(.+))?$"))
    @auth_check
    async def cmd_cd(event):
        target = event.pattern_match.group(1)
        if not target:
            target = config.workdir
        success, res = executor.set_workdir(event.sender_id, target)
        if success:
            await event.reply(f"📂 <b>Рабочая директория изменена на:</b>\n<code>{html.escape(res)}</code>", parse_mode="html")
        else:
            await event.reply(f"❌ {html.escape(res)}", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/ls(?:\s+(.+))?$"))
    @auth_check
    async def cmd_ls(event):
        path = event.pattern_match.group(1)
        workdir = executor.get_workdir(event.sender_id)
        await file_manager.list_directory(event.chat_id, path, workdir)

    @client.on(events.NewMessage(pattern=r"^/get(?:\s+(.+))?$"))
    @auth_check
    async def cmd_get(event):
        path = event.pattern_match.group(1)
        if not path:
            await event.reply("⚠️ Укажите путь к файлу: <code>/get &lt;путь_к_файлу&gt;</code>", parse_mode="html")
            return
        workdir = executor.get_workdir(event.sender_id)
        await file_manager.send_file_to_user(event.chat_id, path, workdir)

    @client.on(events.NewMessage(pattern=r"^/kill$"))
    @auth_check
    async def cmd_kill(event):
        killed = await executor.kill_process(event.sender_id)
        if killed:
            await event.reply("🛑 Сигнал остановки отправлен процессу.")
        else:
            await event.reply("ℹ️ Нет активных запущенных процессов.")

    @client.on(events.NewMessage(pattern=r"^/fav(?:\s+(.*))?$"))
    @auth_check
    async def cmd_fav(event):
        args_str = event.pattern_match.group(1) or ""
        parts = args_str.strip().split(maxsplit=2)
        subcmd = parts[0].lower() if parts else "list"

        if subcmd == "add":
            if len(parts) < 3:
                await event.reply("⚠️ Формат: <code>/fav add &lt;имя&gt; &lt;команда&gt;</code>", parse_mode="html")
                return
            alias, command = parts[1], parts[2]
            if len(alias.encode("utf-8")) > 40:
                await event.reply("⚠️ Имя команды (алиас) слишком длинное (максимум 40 байт для кнопок).")
                return
            favorites_manager.add_favorite(event.sender_id, alias, command)
            await event.reply(
                f"✅ Команда сохранена в избранное:\n⚡ <code>{html.escape(alias)}</code> ➔ <code>{html.escape(command)}</code>",
                parse_mode="html",
            )

        elif subcmd == "del":
            if len(parts) < 2:
                await event.reply("⚠️ Формат: <code>/fav del &lt;имя&gt;</code>", parse_mode="html")
                return
            alias = parts[1]
            if favorites_manager.delete_favorite(event.sender_id, alias):
                await event.reply(f"🗑 Команда <code>{html.escape(alias)}</code> удалена из избранного.", parse_mode="html")
            else:
                await event.reply(f"❌ Команда <code>{html.escape(alias)}</code> не найдена в избранном.", parse_mode="html")

        else:
            # Вывод списка избранного с кнопками
            favs = favorites_manager.get_favorites(event.sender_id)
            if not favs:
                await event.reply(
                    "⭐ У вас пока нет избранных команд.\n\n"
                    "Добавьте команду с помощью:\n"
                    "<code>/fav add htop top -b -n 1</code>\n"
                    "<code>/fav add space df -h</code>",
                    parse_mode="html",
                )
                return

            lines = ["⭐ <b>Избранные команды:</b>\n"]
            for alias, cmd in favs.items():
                lines.append(f"• <b>{html.escape(alias)}</b>: <code>{html.escape(cmd)}</code>")
            lines.append("\n<i>Нажмите на кнопку ниже для быстрого запуска:</i>")

            keyboard = favorites_manager.get_keyboard(event.sender_id)
            await event.reply("\n".join(lines), parse_mode="html", buttons=keyboard)

    # --- Обработчик загрузки входящих файлов от пользователя ---
    @client.on(events.NewMessage(func=lambda e: bool(e.file)))
    @auth_check
    async def handle_document_upload(event):
        workdir = executor.get_workdir(event.sender_id)
        await file_manager.handle_incoming_file(event.message, workdir)

    # --- Обработчик нажатия на инлайн-кнопки ---
    @client.on(events.CallbackQuery)
    @auth_check
    async def handle_callback(event):
        data = event.data.decode("utf-8")
        if data.startswith("kill:"):
            try:
                target_user = int(data.split(":")[1])
            except (IndexError, ValueError):
                target_user = None

            if target_user is not None and event.sender_id == target_user:
                killed = await executor.kill_process(event.sender_id)
                if killed:
                    await event.answer("Процесс прерван.")
                else:
                    await event.answer("Процесс уже завершен.")
            else:
                await event.answer("Это не ваш процесс!", alert=True)

        elif data.startswith("fav_run:"):
            alias = data.split(":", 1)[1]
            favs = favorites_manager.get_favorites(event.sender_id)
            cmd = favs.get(alias)
            if cmd:
                await event.answer(f"Запуск: {alias}")
                await executor.execute(event.chat_id, event.sender_id, cmd)
            else:
                await event.answer("Команда не найдена!", alert=True)

    # --- Основной обработчик команд shell ---
    @client.on(events.NewMessage)
    @auth_check
    async def handle_shell_message(event):
        text = (event.raw_text or "").strip()
        if not text:
            return

        # Игнорируем сообщения с файлами (они обрабатываются в handle_document_upload)
        if event.file:
            return

        # Игнорируем встроенные команды бота
        if any(text.startswith(prefix) for prefix in ["/start", "/help", "/pwd", "/cd", "/ls", "/get", "/kill", "/fav"]):
            return

        # Если команда начинается с /run, отсекаем его
        if text.startswith("/run"):
            cmd = text[4:].strip()
        else:
            cmd = text

        if not cmd:
            return

        logger.info(f"Выполнение команды от {event.sender_id}: {cmd}")
        await executor.execute(event.chat_id, event.sender_id, cmd)

    # --- Запуск клиента ---
    logger.info("Запуск Telegram-бота...")
    await client.start(bot_token=config.bot_token)
    logger.info("Бот успешно авторизован в Telegram!")

    # Конфигурация видимости подсказок команд
    await configure_bot_command_scopes(client, config.allowed_users)

    logger.info("Бот работает и ожидает входящих команд.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа бота завершена.")
