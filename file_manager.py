"""
Модуль для работы с файлами: скачивание файлов с сервера (/get), просмотр каталогов (/ls)
и загрузка файлов на сервер через Telegram.
"""

import html
import logging
import os
import time
from typing import Optional
from telethon import TelegramClient

logger = logging.getLogger("tg_ssh_bot.file_manager")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROTECTED_BOT_FILES = {
    "main.py",
    "config.py",
    "client.py",
    "executor.py",
    "file_manager.py",
    "favorites.py",
    "security.py",
    "run.sh",
    "requirements.txt",
    ".env",
    ".env.example",
    ".gitignore",
    "plan.md",
    "readme.md",
    "favorites.json",
}


def format_size(size_bytes: int) -> str:
    """Форматирует размер в байтах в человекочитаемый вид."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


class FileManager:
    def __init__(self, client: TelegramClient):
        self.client = client

    def is_protected_bot_file(self, target_path: str) -> bool:
        """Проверяет, относится ли файл к защищенным системным файлам бота."""
        resolved = os.path.abspath(target_path)
        base = os.path.basename(resolved)

        # 1. Защита файлов сессий Telethon в любом каталоге
        if base.endswith(".session") or base.endswith(".session-journal"):
            return True

        # 2. Защита исходных файлов бота в каталоге репозитория бота
        parent = os.path.dirname(resolved)
        try:
            is_in_bot_dir = (
                parent == BOT_DIR
                or (hasattr(os.path, "samefile") and os.path.exists(parent) and os.path.samefile(parent, BOT_DIR))
            )
        except (FileNotFoundError, OSError):
            is_in_bot_dir = parent == BOT_DIR

        if is_in_bot_dir and base in PROTECTED_BOT_FILES:
            return True

        return False

    def resolve_path(self, raw_path: str, workdir: str) -> str:
        """Преобразует относительный или домашний путь в абсолютный."""
        expanded = os.path.expanduser(raw_path.strip())
        if not os.path.isabs(expanded):
            expanded = os.path.join(workdir, expanded)
        return os.path.abspath(expanded)

    async def send_file_to_user(self, chat_id: int, file_path: str, workdir: str):
        """Отправляет файл с сервера пользователю в Telegram."""
        resolved = self.resolve_path(file_path, workdir)

        if not os.path.exists(resolved):
            await self.client.send_message(
                chat_id,
                f"❌ Файл не найден:\n<code>{html.escape(resolved)}</code>",
                parse_mode="html",
            )
            return

        if os.path.isdir(resolved):
            await self.client.send_message(
                chat_id,
                f"⚠️ Указанный путь является папкой:\n<code>{html.escape(resolved)}</code>\n"
                f"Используйте <code>/ls {html.escape(file_path)}</code> для просмотра содержимого или архивируйте (tar/zip).",
                parse_mode="html",
            )
            return

        file_size = os.path.getsize(resolved)
        # Лимит Telegram на отправку через MTProto — 2 ГБ (2000 МБ)
        if file_size > 2000 * 1024 * 1024:
            await self.client.send_message(
                chat_id,
                f"❌ Размер файла ({format_size(file_size)}) превышает лимит Telegram (2 ГБ).",
            )
            return

        file_basename = os.path.basename(resolved)
        status_msg = await self.client.send_message(
            chat_id,
            f"📤 <b>Отправка файла:</b> <code>{html.escape(file_basename)}</code> ({format_size(file_size)})...",
            parse_mode="html",
        )

        last_update = 0.0

        def progress_callback(current, total):
            nonlocal last_update
            now = time.time()
            if now - last_update >= 2.0 or current == total:
                last_update = now
                percent = (current / total) * 100
                logger.debug(f"Отправка {file_basename}: {percent:.1f}%")

        try:
            await self.client.send_file(
                chat_id,
                file=resolved,
                caption=f"📄 <b>Файл:</b> <code>{html.escape(file_basename)}</code>\n📏 <b>Размер:</b> {format_size(file_size)}",
                parse_mode="html",
                progress_callback=progress_callback,
            )
            await status_msg.delete()
        except Exception as e:
            logger.exception("Ошибка при отправке файла:")
            await status_msg.edit(f"❌ Ошибка отправки файла:\n<code>{html.escape(str(e))}</code>", parse_mode="html")

    async def list_directory(self, chat_id: int, dir_path: Optional[str], workdir: str):
        """Выводит список файлов и папок в директории."""
        target_dir = self.resolve_path(dir_path or ".", workdir)

        if not os.path.exists(target_dir):
            await self.client.send_message(
                chat_id,
                f"❌ Директория не найдена:\n<code>{html.escape(target_dir)}</code>",
                parse_mode="html",
            )
            return

        if not os.path.isdir(target_dir):
            await self.client.send_message(
                chat_id,
                f"⚠️ Путь не является папкой:\n<code>{html.escape(target_dir)}</code>",
                parse_mode="html",
            )
            return

        try:
            entries = sorted(os.scandir(target_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
        except Exception as e:
            await self.client.send_message(
                chat_id,
                f"❌ Ошибка чтения директории:\n<code>{html.escape(str(e))}</code>",
                parse_mode="html",
            )
            return

        lines = [f"📂 <b>Директория:</b> <code>{html.escape(target_dir)}</code>\n"]
        dirs_count = 0
        files_count = 0

        for entry in entries:
            try:
                safe_name = html.escape(entry.name)
                if entry.is_dir():
                    dirs_count += 1
                    lines.append(f"📁 <code>{safe_name}/</code>")
                else:
                    files_count += 1
                    size_str = format_size(entry.stat().st_size)
                    lines.append(f"📄 <code>{safe_name}</code> ({size_str})")
            except OSError:
                continue

        lines.append(f"\nВсего: папок {dirs_count}, файлов {files_count}")
        lines.append("\n💡 <i>Чтобы скачать файл, отправьте:</i> <code>/get &lt;имя_файла&gt;</code>")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3900] + "\n\n<i>...список обрезан из-за ограничений длины...</i>"

        await self.client.send_message(chat_id, text, parse_mode="html")

    async def handle_incoming_file(self, message, workdir: str):
        """Сохраняет отправленный пользователем файл на сервер с защитой системных файлов бота."""
        chat_id = message.chat_id
        if not message.file:
            return

        # Имя файла из документа с защитой от path traversal (только basename)
        raw_name = message.file.name or f"file_{int(time.time())}"
        file_name = os.path.basename(raw_name)

        # Если в тексте сообщения передан целевой путь:
        caption = (message.text or "").strip()
        if caption.startswith("/save"):
            parts = caption.split(maxsplit=1)
            target_path = parts[1].strip() if len(parts) > 1 else file_name
        elif caption and not caption.startswith("/"):
            target_path = caption
        else:
            target_path = file_name

        dest_path = self.resolve_path(target_path, workdir)

        # Если путь указывает на директорию, сохраняем с оригинальным именем внутри нее
        if os.path.isdir(dest_path):
            dest_path = os.path.join(dest_path, file_name)

        # Защита от перезаписи файлов бота
        if self.is_protected_bot_file(dest_path):
            safe_name = html.escape(os.path.basename(dest_path))
            await self.client.send_message(
                chat_id,
                f"🚫 <b>Отказано в доступе:</b> Перезапись файлов бота (<code>{safe_name}</code>) запрещена в целях безопасности.",
                parse_mode="html",
            )
            return

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        status_msg = await self.client.send_message(
            chat_id,
            f"📥 <b>Загрузка файла на сервер...</b>\nЦелевой путь: <code>{html.escape(dest_path)}</code>",
            parse_mode="html",
        )

        last_update = 0.0

        def progress_callback(current, total):
            nonlocal last_update
            now = time.time()
            if now - last_update >= 2.0 or current == total:
                last_update = now
                percent = (current / total) * 100
                logger.debug(f"Загрузка на сервер: {percent:.1f}%")

        try:
            await self.client.download_media(
                message,
                file=dest_path,
                progress_callback=progress_callback,
            )
            file_size = os.path.getsize(dest_path)
            await status_msg.edit(
                f"✅ <b>Файл успешно сохранен на сервере!</b>\n"
                f"📂 <b>Путь:</b> <code>{html.escape(dest_path)}</code>\n"
                f"📏 <b>Размер:</b> {format_size(file_size)}",
                parse_mode="html",
            )
        except Exception as e:
            logger.exception("Ошибка при сохранении файла на сервере:")
            await status_msg.edit(f"❌ Ошибка сохранения файла:\n<code>{html.escape(str(e))}</code>", parse_mode="html")
