"""
Модуль исполнения shell-команд с поддержкой потокового вывода, PTY, троттлинга и остановки процессов.
"""

import asyncio
import html
import logging
import os
import re
import signal
import sys
import tempfile
import time
from typing import Dict, Optional, Tuple
from telethon import TelegramClient
from telethon.tl.custom import Button

logger = logging.getLogger("tg_ssh_bot.executor")

# Проверка наличия PTY (доступен в Linux / Android Termux, отсутствует в стандартном Python на Windows)
try:
    import pty
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

# Регулярное выражение для удаления ANSI-escape кодов терминала
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

MAX_TG_CHARS = 3800  # Безопасный лимит символов для отправки в pre-блоке Telegram
EDIT_INTERVAL = 1.3  # Минимальный интервал обновления сообщения в секундах (защита от FloodWait)
MAX_BUFFER_CHARS = 50_000  # Максимальный размер буфера в памяти (для live-отображения)
MAX_LOG_BYTES = 20 * 1024 * 1024  # 20 МБ максимальный размер файла лога на диске


def clean_terminal_output(text: str) -> str:
    """Удаляет ANSI коды и нормализует переводы строк (включая возврат каретки \r)."""
    text = ANSI_ESCAPE.sub("", text)
    # Обработка возвратов каретки (\r) для утилит вроде curl/wget со статус-барами
    lines = []
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\r")
        lines.append(parts[-1])
    return "\n".join(lines)


class ProcessContext:
    def __init__(self, cmd: str, user_id: int, process: asyncio.subprocess.Process, master_fd: Optional[int] = None):
        self.cmd = cmd
        self.user_id = user_id
        self.process = process
        self.master_fd = master_fd
        self.pty_transport = None
        self.buffer = ""
        self.last_sent_text = ""
        self.last_edit_time = 0.0
        self.is_terminated = False
        self.is_done = False
        self.total_bytes_written = 0
        self.is_log_truncated = False
        self.log_file = tempfile.NamedTemporaryFile(mode="w+b", delete=False)

    def append_output(self, data: bytes):
        """Записывает сырые данные во временный файл и поддерживает кольцевой буфер в памяти."""
        # 1. Запись в лог-файл на диске (с лимитом MAX_LOG_BYTES)
        if self.total_bytes_written < MAX_LOG_BYTES:
            to_write = data[: MAX_LOG_BYTES - self.total_bytes_written]
            try:
                self.log_file.write(to_write)
                self.total_bytes_written += len(to_write)
            except Exception:
                pass
        else:
            self.is_log_truncated = True

        # 2. Накопление в памяти ограниченного хвоста для live-отображения
        decoded = data.decode("utf-8", errors="replace")
        self.buffer += decoded
        if len(self.buffer) > MAX_BUFFER_CHARS:
            self.buffer = self.buffer[-MAX_BUFFER_CHARS:]

    def terminate(self):
        """Мягко завершает процесс и всех его потомков (SIGINT для всей группы процессов)."""
        self.is_terminated = True
        try:
            if sys.platform != "win32":
                try:
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()
        except Exception as e:
            logger.warning(f"Ошибка при отправке SIGINT процессу: {e}")

    def kill(self):
        """Принудительно уничтожает процесс и всех потомков (SIGKILL для всей группы процессов)."""
        self.is_terminated = True
        try:
            if sys.platform != "win32":
                try:
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
        except Exception as e:
            logger.warning(f"Ошибка при SIGKILL процесса: {e}")

    def cleanup(self):
        """Закрывает дескрипторы и удаляет временный файл логов."""
        if self.pty_transport and not self.pty_transport.is_closing():
            try:
                self.pty_transport.close()
            except Exception:
                pass
        try:
            self.log_file.close()
        except Exception:
            pass
        if os.path.exists(self.log_file.name):
            try:
                os.unlink(self.log_file.name)
            except Exception:
                pass


class CommandExecutor:
    def __init__(self, client: TelegramClient, default_workdir: str):
        self.client = client
        self.workdirs: Dict[int, str] = {}  # Текущая рабочая директория пользователя
        self.default_workdir = default_workdir
        self.running_processes: Dict[int, ProcessContext] = {}

    def get_workdir(self, user_id: int) -> str:
        """Возвращает текущую рабочую директорию пользователя."""
        return self.workdirs.get(user_id, self.default_workdir)

    def set_workdir(self, user_id: int, new_dir: str) -> Tuple[bool, str]:
        """Сменяет рабочую директорию для пользователя."""
        expanded = os.path.expanduser(new_dir)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.get_workdir(user_id), expanded)
        resolved = os.path.abspath(expanded)
        if os.path.isdir(resolved):
            self.workdirs[user_id] = resolved
            return True, resolved
        return False, f"Директория '{new_dir}' не найдена."

    def get_active_process(self, user_id: int) -> Optional[ProcessContext]:
        """Возвращает запущенный процесс пользователя, если есть."""
        return self.running_processes.get(user_id)

    async def kill_process(self, user_id: int) -> bool:
        """Прерывает активный процесс пользователя и все его дочерние процессы."""
        ctx = self.get_active_process(user_id)
        if ctx and not ctx.is_done:
            ctx.terminate()
            # Даем секунду на мягкое завершение, если нет - принудительный SIGKILL
            await asyncio.sleep(0.8)
            try:
                if ctx.process.returncode is None:
                    ctx.kill()
            except Exception:
                pass
            return True
        return False

    async def execute(self, chat_id: int, user_id: int, command: str):
        """Запускает команду, ведет потоковый вывод и обновляет сообщение в Telegram."""
        if self.get_active_process(user_id):
            await self.client.send_message(
                chat_id,
                "⚠️ У вас уже запущен процесс! Используйте /kill или нажмите кнопку «Остановить», чтобы завершить его.",
            )
            return

        workdir = self.get_workdir(user_id)
        safe_cmd = html.escape(command)
        safe_workdir = html.escape(workdir)

        status_msg = await self.client.send_message(
            chat_id,
            f"🚀 <b>Запуск команды:</b> <code>{safe_cmd}</code>\n📂 <b>Каталог:</b> <code>{safe_workdir}</code>\n\n<i>Подготовка вывода...</i>",
            parse_mode="html",
            buttons=[Button.inline("⏹ Остановить", data=f"kill:{user_id}")],
        )

        master_fd = None
        slave_fd = None
        ctx = None

        try:
            # Использование PTY в Linux/Termux обеспечивает честный TTY (htop, top, ping не буферизируют вывод)
            if HAS_PTY:
                master_fd, slave_fd = pty.openpty()
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=workdir,
                    close_fds=True,
                    preexec_fn=os.setsid if sys.platform != "win32" else None,
                )
                os.close(slave_fd)
                slave_fd = None
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=workdir,
                    preexec_fn=os.setsid if sys.platform != "win32" else None,
                )

            ctx = ProcessContext(cmd=command, user_id=user_id, process=proc, master_fd=master_fd)
            self.running_processes[user_id] = ctx

            # Запуск чтения вывода
            if HAS_PTY and master_fd is not None:
                read_task = asyncio.create_task(self._read_pty_output(ctx, status_msg, chat_id, user_id))
            else:
                read_task = asyncio.create_task(self._read_pipe_output(ctx, status_msg, chat_id, user_id))

            await proc.wait()
            ctx.is_done = True
            await read_task

            # Финальное сообщение
            await self._finalize_output(ctx, status_msg, chat_id)

        except Exception as e:
            logger.exception("Ошибка при выполнении команды:")
            await self.client.send_message(
                chat_id,
                f"❌ <b>Ошибка выполнения:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode="html",
            )
        finally:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except Exception:
                    pass
            # Если pty_transport не был подключен к master_fd, закрываем FD вручную
            if ctx and not ctx.pty_transport and master_fd is not None:
                try:
                    os.close(master_fd)
                except Exception:
                    pass
            if ctx:
                ctx.cleanup()
            self.running_processes.pop(user_id, None)

    async def _read_pty_output(self, ctx: ProcessContext, status_msg, chat_id: int, user_id: int):
        """Асинхронное чтение из master FD псевдотерминала с корректным управлением транспортом."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            transport, _ = await loop.connect_read_pipe(
                lambda: protocol,
                os.fdopen(ctx.master_fd, "rb", buffering=0),
            )
            ctx.pty_transport = transport
        except Exception as e:
            logger.warning(f"Не удалось подключить read pipe к master_fd: {e}")
            return

        try:
            while not ctx.is_done:
                try:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=0.5)
                    if not chunk:
                        break
                    ctx.append_output(chunk)
                    await self._maybe_update_message(ctx, status_msg)
                except asyncio.TimeoutError:
                    await self._maybe_update_message(ctx, status_msg)
                except (OSError, Exception):
                    # В Linux при завершении дочернего процесса в PTY генерируется EIO (Errno 5)
                    break

            # Вычитываем оставшиеся данные после завершения процесса
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=0.1)
                    if not chunk:
                        break
                    ctx.append_output(chunk)
                except Exception:
                    break
        finally:
            if ctx.pty_transport and not ctx.pty_transport.is_closing():
                try:
                    ctx.pty_transport.close()
                except Exception:
                    pass

    async def _read_pipe_output(self, ctx: ProcessContext, status_msg, chat_id: int, user_id: int):
        """Чтение из стандартного async pipe (fallback для не-PTY окружений)."""
        while not ctx.is_done:
            try:
                line = await asyncio.wait_for(ctx.process.stdout.readline(), timeout=0.5)
                if not line:
                    if ctx.process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                    continue
                ctx.append_output(line)
                await self._maybe_update_message(ctx, status_msg)
            except asyncio.TimeoutError:
                await self._maybe_update_message(ctx, status_msg)
            except Exception:
                break

        # Вычитываем оставшиеся строки
        while True:
            try:
                line = await asyncio.wait_for(ctx.process.stdout.readline(), timeout=0.1)
                if not line:
                    break
                ctx.append_output(line)
            except Exception:
                break

    async def _maybe_update_message(self, ctx: ProcessContext, status_msg):
        """Периодическое обновление сообщения с проверкой троттлинга и экранированием HTML."""
        now = time.time()
        if now - ctx.last_edit_time < EDIT_INTERVAL:
            return

        cleaned = clean_terminal_output(ctx.buffer)
        if not cleaned.strip():
            return

        # Берем хвост вывода, чтобы не превысить лимит
        tail = cleaned[-MAX_TG_CHARS:]
        if tail == ctx.last_sent_text:
            return

        ctx.last_sent_text = tail
        ctx.last_edit_time = now

        safe_cmd = html.escape(ctx.cmd)
        safe_tail = html.escape(tail)
        if len(safe_tail) > MAX_TG_CHARS:
            safe_tail = safe_tail[-MAX_TG_CHARS:]

        text = (
            f"⚡ <b>Выполняется:</b> <code>{safe_cmd}</code>\n\n"
            f"<pre><code>{safe_tail}</code></pre>"
        )
        try:
            await status_msg.edit(
                text,
                parse_mode="html",
                buttons=[Button.inline("⏹ Остановить", data=f"kill:{ctx.user_id}")],
            )
        except Exception as e:
            # Игнорируем частые ошибки MessageNotModified
            if "not modified" not in str(e).lower():
                logger.debug(f"Ошибка редактирования сообщения: {e}")

    async def _finalize_output(self, ctx: ProcessContext, status_msg, chat_id: int):
        """Финальное оформление результатов выполнения команды."""
        cleaned = clean_terminal_output(ctx.buffer)
        ret_code = ctx.process.returncode

        if ctx.is_terminated:
            status_header = "🛑 <b>Процесс остановлен пользователем</b>"
        elif ret_code == 0:
            status_header = "✅ <b>Команда успешно выполнена</b>"
        else:
            status_header = f"❌ <b>Завершено с кодом ошибки {ret_code}</b>"

        body = cleaned.strip()
        if not body:
            body = "(Вывод пуст)"

        safe_cmd = html.escape(ctx.cmd)

        # Если вывод целиком уместился в одно сообщение и буфер не был урезан
        if len(body) <= MAX_TG_CHARS and not ctx.is_log_truncated and ctx.total_bytes_written <= MAX_BUFFER_CHARS:
            safe_body = html.escape(body)
            if len(safe_body) > MAX_TG_CHARS:
                safe_body = safe_body[-MAX_TG_CHARS:]

            final_text = (
                f"{status_header}\n"
                f"Команда: <code>{safe_cmd}</code>\n\n"
                f"<pre><code>{safe_body}</code></pre>"
            )
            try:
                await status_msg.edit(final_text, parse_mode="html", buttons=None)
            except Exception:
                try:
                    await self.client.send_message(chat_id, final_text, parse_mode="html")
                except Exception:
                    # Резервный вывод в виде чистого текста при сбое разметки
                    await self.client.send_message(chat_id, f"{ctx.cmd}\n\n{body}")
        else:
            # Вывод слишком большой: показываем хвост и отправляем файл лога
            tail = body[-MAX_TG_CHARS:]
            safe_tail = html.escape(tail)
            if len(safe_tail) > MAX_TG_CHARS:
                safe_tail = safe_tail[-MAX_TG_CHARS:]

            trunc_note = " (обрезан до 20 МБ)" if ctx.is_log_truncated else ""
            final_text = (
                f"{status_header}\n"
                f"Команда: <code>{safe_cmd}</code>\n"
                f"<i>(Вывод превышает лимит Telegram, полный лог прикреплен файлом{trunc_note})</i>\n\n"
                f"<pre><code>{safe_tail}</code></pre>"
            )
            try:
                await status_msg.edit(final_text, parse_mode="html", buttons=None)
            except Exception:
                try:
                    await self.client.send_message(chat_id, final_text, parse_mode="html")
                except Exception:
                    pass

            # Отправка файла полного вывода
            try:
                ctx.log_file.flush()
                file_size = os.path.getsize(ctx.log_file.name)
                if file_size > 0:
                    await self.client.send_file(
                        chat_id,
                        file=ctx.log_file.name,
                        caption=f"📄 Полный вывод команды: {ctx.cmd[:100]}",
                    )
            except Exception as e:
                logger.error(f"Не удалось отправить файл лога: {e}")
