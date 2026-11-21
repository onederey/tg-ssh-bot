"""
Модуль исполнения shell-команд с поддержкой потокового вывода, PTY, троттлинга и остановки процессов.
"""

import asyncio
import getpass
import html
import logging
import os
import re
import signal
import socket
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
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

try:
    import fcntl
    import struct
    import termios
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

# Регулярное выражение для удаления ANSI-escape кодов терминала
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Регулярное выражение для очистки оборванных HTML-сущностей в начале среза
HTML_ENTITY_CUT_RE = re.compile(r"^([a-zA-Z0-9]+|#\d+|#x[0-9a-fA-F]+);")

MAX_TG_CHARS = 3800  # Безопасный лимит символов для отправки в pre-блоке Telegram
EDIT_INTERVAL = 1.3  # Минимальный интервал обновления сообщения в секундах (защита от FloodWait)
MAX_BUFFER_CHARS = 50_000  # Максимальный размер буфера в памяти (для live-отображения)
MAX_LOG_BYTES = 20 * 1024 * 1024  # 20 МБ максимальный размер файла лога на диске


def set_pty_winsize(fd: int, rows: int = 24, cols: int = 80):
    """Устанавливает стандартный размер виртуального терминала для корректного форматирования утилит."""
    if HAS_TERMIOS:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass


def clean_terminal_output(text: str) -> str:
    """Удаляет ANSI коды и нормализует переводы строк (включая возврат каретки \r)."""
    text = ANSI_ESCAPE.sub("", text)
    # Обработка возвратов каретки (\r) для утилит вроде curl/wget со статус-барами
    lines = []
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\r")
        lines.append(parts[-1])
    return "\n".join(lines)


DIVIDER = "────────────────────"

# Глобальный кэш неизменяемых данных системы (исключает блокирующие вызовы socket/getpass при частых live-обновлениях)
_CACHED_USER_HOST: Optional[str] = None
_CACHED_HOME: Optional[str] = None


def get_system_user_host() -> str:
    """Возвращает кэшированное значение user@host."""
    global _CACHED_USER_HOST
    if _CACHED_USER_HOST is None:
        try:
            user = getpass.getuser()
        except Exception:
            user = os.environ.get("USER") or "root"

        try:
            host = socket.gethostname()
            if not host or host == "localhost":
                if "com.termux" in os.environ.get("PREFIX", "") or "TERMUX_VERSION" in os.environ:
                    host = "termux"
                else:
                    host = host or "server"
        except Exception:
            host = "termux"
        _CACHED_USER_HOST = f"{user}@{host}"
    return _CACHED_USER_HOST


def shorten_path(workdir: str) -> str:
    """Сокращает путь пользователя, заменяя домашнюю директорию на ~."""
    global _CACHED_HOME
    if _CACHED_HOME is None:
        _CACHED_HOME = os.path.normpath(os.path.expanduser("~"))

    norm_workdir = os.path.normpath(workdir)
    if norm_workdir == _CACHED_HOME:
        return "~"
    elif norm_workdir.startswith(_CACHED_HOME + os.sep):
        rel = norm_workdir[len(_CACHED_HOME) + 1 :]
        return "~/" + rel.replace(os.sep, "/")
    else:
        return norm_workdir.replace(os.sep, "/")


def format_prompt_header(workdir: str, command: str) -> str:
    """Форматирует Unix-заголовок без лишних эмодзи:
    ┌ root@termux:~/app
    └ $ ping -c 4 8.8.8.8
    """
    user_host = get_system_user_host()
    short_path = shorten_path(workdir)
    safe_prompt = html.escape(f"{user_host}:{short_path}")
    # Ограничиваем длину команды в шапке (до 160 символов), чтобы она не вытесняла вывод за лимиты Telegram
    display_cmd = command if len(command) <= 160 else command[:157] + "..."
    safe_cmd = html.escape(display_cmd)
    return (
        f"┌ <b>{safe_prompt}</b>\n"
        f"└ $ <code>{safe_cmd}</code>"
    )


def truncate_safe_html(text: str, max_chars: int) -> str:
    """Экранирует текст и берет хвост, не превышающий max_chars,
    корректно удаляя оборванную в точке среза HTML-сущность.
    """
    escaped = html.escape(text)
    if len(escaped) <= max_chars:
        return escaped
    tail = escaped[-max_chars:]
    # Если срез прошел внутри сущности (&quot; -> uot; или &#123; -> 123;), удаляем оборванный префикс
    tail = HTML_ENTITY_CUT_RE.sub("", tail)
    return tail


def format_live_output(header: str, output_tail: str, elapsed: float) -> str:
    """Форматирует вывод в процессе исполнения (без эмодзи 👤 и ⏳)."""
    timer_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"

    if output_tail.strip():
        safe_tail = truncate_safe_html(output_tail, max_chars=3200)
        return (
            f"{header}\n"
            f"{DIVIDER}\n"
            f"<pre><code>{safe_tail}</code></pre>\n"
            f"{DIVIDER}\n"
            f"Выполняется... [{timer_str}]"
        )
    else:
        return (
            f"{header}\n"
            f"{DIVIDER}\n"
            f"Выполняется... [{timer_str}]"
        )


def format_status_line(is_terminated: bool, ret_code: Optional[int], duration: float) -> str:
    """Форматирует статусную строку завершения:
    ✔ exit: 0  •  ⏱ 2.1s  •  18:30:12
    ✖ exit: 1  •  ⏱ 0.5s  •  18:30:15
    ⏹ stopped  •  ⏱ 3.2s  •  18:30:20
    """
    now_time = time.strftime("%H:%M:%S")
    if duration < 60:
        dur_str = f"{duration:.1f}s"
    else:
        dur_str = f"{int(duration // 60)}m {duration % 60:.1f}s"

    if is_terminated:
        return f"⏹ stopped  •  ⏱ {dur_str}  •  {now_time}"
    elif ret_code == 0:
        return f"✔ exit: 0  •  ⏱ {dur_str}  •  {now_time}"
    else:
        return f"✖ exit: {ret_code if ret_code is not None else 1}  •  ⏱ {dur_str}  •  {now_time}"


def format_final_output(
    header: str,
    output_body: str,
    status_str: str,
    trunc_note: str = "",
    max_total_chars: int = 4000,
) -> str:
    """Форматирует финальный вывод завершенной команды с гарантией укладывания в лимит Telegram."""
    note = f"\n<i>{trunc_note}</i>" if trunc_note else ""
    wrapper_overhead = len(header) + len(DIVIDER) + len(note) + len(status_str) + 40
    max_body_chars = max(100, max_total_chars - wrapper_overhead)
    safe_body = truncate_safe_html(output_body, max_chars=max_body_chars)
    return (
        f"{header}\n"
        f"{DIVIDER}\n"
        f"<pre><code>{safe_body}</code></pre>{note}\n"
        f"{status_str}"
    )


class ProcessContext:
    def __init__(
        self,
        cmd: str,
        user_id: int,
        process: asyncio.subprocess.Process,
        workdir: str,
        cmd_id: str = "",
        master_fd: Optional[int] = None,
    ):
        self.cmd = cmd
        self.user_id = user_id
        self.process = process
        self.workdir = workdir
        self.cmd_id = cmd_id
        self.header = format_prompt_header(workdir, cmd)
        self.master_fd = master_fd
        self.pty_transport = None
        self.buffer = ""
        self.last_sent_text = ""
        self.last_edit_time = 0.0
        self.start_time = time.time()
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
            if not self.log_file.closed:
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
        self.command_cache: OrderedDict[str, str] = OrderedDict()  # Кэш команд для повтора и копирования
        self.user_locks: Dict[int, asyncio.Lock] = {}  # Защита от параллельного запуска одним пользователем
        self.MAX_CACHE_SIZE = 500

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Возвращает персональный асинхронный замок пользователя."""
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        return self.user_locks[user_id]

    def save_command(self, command: str) -> str:
        """Сохраняет команду в кэш и возвращает короткий идентификатор для callback_data."""
        cmd_id = uuid.uuid4().hex[:10]
        self.command_cache[cmd_id] = command
        if len(self.command_cache) > self.MAX_CACHE_SIZE:
            self.command_cache.popitem(last=False)
        return cmd_id

    def get_saved_command(self, cmd_id: str) -> Optional[str]:
        """Возвращает сохраненную команду по ее идентификатору."""
        return self.command_cache.get(cmd_id)

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
        lock = self._get_user_lock(user_id)
        if lock.locked() or self.get_active_process(user_id):
            await self.client.send_message(
                chat_id,
                "⚠️ У вас уже запущен процесс! Используйте /kill или нажмите кнопку «⏹ Прервать (Ctrl+C)», чтобы завершить его.",
            )
            return

        async with lock:
            if self.get_active_process(user_id):
                await self.client.send_message(
                    chat_id,
                    "⚠️ У вас уже запущен процесс! Используйте /kill или нажмите кнопку «⏹ Прервать (Ctrl+C)», чтобы завершить его.",
                )
                return

            workdir = self.get_workdir(user_id)
            cmd_id = self.save_command(command)
            header = format_prompt_header(workdir, command)

            initial_text = format_live_output(header, "", elapsed=0.0)

            status_msg = await self.client.send_message(
                chat_id,
                initial_text,
                parse_mode="html",
                buttons=[Button.inline("⏹ Прервать (Ctrl+C)", data=f"kill:{user_id}")],
            )

            master_fd = None
            slave_fd = None
            ctx = None

            try:
                # Использование PTY в Linux/Termux обеспечивает честный TTY (htop, top, ping не буферизируют вывод)
                if HAS_PTY:
                    master_fd, slave_fd = pty.openpty()
                    set_pty_winsize(slave_fd, rows=24, cols=80)
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

                ctx = ProcessContext(
                    cmd=command,
                    user_id=user_id,
                    process=proc,
                    workdir=workdir,
                    cmd_id=cmd_id,
                    master_fd=master_fd,
                )
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
                err_msg = str(e)
                duration = time.time() - (ctx.start_time if ctx else time.time())
                status_line = format_status_line(
                    is_terminated=False,
                    ret_code=1,
                    duration=duration,
                )
                err_buttons = [
                    [
                        Button.inline("🔄 Повторить", data=f"rerun:{cmd_id}"),
                        Button.inline("📋 Команда", data=f"copy_cmd:{cmd_id}"),
                    ]
                ]
                final_err_text = format_final_output(
                    header=ctx.header if ctx else header,
                    output_body=f"Ошибка: {err_msg}",
                    status_str=status_line,
                )
                try:
                    await status_msg.edit(final_err_text, parse_mode="html", buttons=err_buttons)
                except Exception:
                    await self.client.send_message(chat_id, final_err_text, parse_mode="html", buttons=err_buttons)
            finally:
                if slave_fd is not None:
                    try:
                        os.close(slave_fd)
                    except Exception:
                        pass
                # Если master_fd был открыт, но pty_transport не взял владение (например, при падении subprocess) — закрываем FD
                if master_fd is not None and (not ctx or not ctx.pty_transport):
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
                chunk = await asyncio.wait_for(ctx.process.stdout.read(2048), timeout=0.5)
                if not chunk:
                    if ctx.process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                    continue
                ctx.append_output(chunk)
                await self._maybe_update_message(ctx, status_msg)
            except asyncio.TimeoutError:
                await self._maybe_update_message(ctx, status_msg)
            except Exception:
                break

        # Вычитываем оставшиеся байты
        while True:
            try:
                chunk = await asyncio.wait_for(ctx.process.stdout.read(2048), timeout=0.1)
                if not chunk:
                    break
                ctx.append_output(chunk)
            except Exception:
                break

    async def _maybe_update_message(self, ctx: ProcessContext, status_msg):
        """Периодическое обновление сообщения с проверкой троттлинга и экранированием HTML."""
        now = time.time()
        if now - ctx.last_edit_time < EDIT_INTERVAL:
            return

        cleaned = clean_terminal_output(ctx.buffer)
        tail = cleaned[-MAX_TG_CHARS:] if cleaned else ""

        # Если вывод не изменился, обновляем не чаще чем раз в 6 сек для защиты от FloodWait
        if tail == ctx.last_sent_text and (now - ctx.last_edit_time < 6.0):
            return

        ctx.last_sent_text = tail
        ctx.last_edit_time = now

        elapsed = now - ctx.start_time
        text = format_live_output(ctx.header, tail, elapsed)

        try:
            await status_msg.edit(
                text,
                parse_mode="html",
                buttons=[Button.inline("⏹ Прервать (Ctrl+C)", data=f"kill:{ctx.user_id}")],
            )
        except Exception as e:
            # Игнорируем частые ошибки MessageNotModified
            if "not modified" not in str(e).lower():
                logger.debug(f"Ошибка редактирования сообщения: {e}")

    async def _finalize_output(self, ctx: ProcessContext, status_msg, chat_id: int):
        """Финальное оформление результатов выполнения команды в стиле Unix-минимализма."""
        cleaned = clean_terminal_output(ctx.buffer)
        ret_code = ctx.process.returncode
        duration = time.time() - ctx.start_time

        status_line = format_status_line(
            is_terminated=ctx.is_terminated,
            ret_code=ret_code,
            duration=duration,
        )

        body = cleaned.strip() or "(нет вывода)"

        buttons = [
            [
                Button.inline("🔄 Повторить", data=f"rerun:{ctx.cmd_id}"),
                Button.inline("📋 Команда", data=f"copy_cmd:{ctx.cmd_id}"),
            ]
        ]

        # Проверяем, укладывается ли полный вывод в безопасный лимит
        escaped_full = html.escape(body)
        full_candidate = (
            f"{ctx.header}\n"
            f"{DIVIDER}\n"
            f"<pre><code>{escaped_full}</code></pre>\n"
            f"{status_line}"
        )

        # Если сообщение укладывается в безопасный лимит Telegram (4000 символов) и буфер не был урезан
        if len(full_candidate) <= 4000 and not ctx.is_log_truncated and ctx.total_bytes_written <= MAX_BUFFER_CHARS:
            final_text = full_candidate
            need_log_file = False
        else:
            need_log_file = True
            trunc_note = f"(Вывод превышает лимит Telegram, полный лог прикреплен файлом{' [обрезан до 20 МБ]' if ctx.is_log_truncated else ''})"
            final_text = format_final_output(
                header=ctx.header,
                output_body=body,
                status_str=status_line,
                trunc_note=trunc_note,
                max_total_chars=4000,
            )

        try:
            await status_msg.edit(final_text, parse_mode="html", buttons=buttons)
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение: {e}")
            try:
                await self.client.send_message(chat_id, final_text, parse_mode="html", buttons=buttons)
            except Exception:
                # Резервный вывод чистым текстом при критическом сбое разметки (берем хвост вывода)
                tail_plain = body[-2500:]
                plain_fallback = f"┌ {ctx.workdir}\n└ $ {ctx.cmd[:120]}\n{DIVIDER}\n{tail_plain}\n\n{status_line}"
                await self.client.send_message(chat_id, plain_fallback, buttons=buttons)

        if need_log_file:
            try:
                # Безопасно закрываем файл перед отправкой (критично для Windows)
                if not ctx.log_file.closed:
                    ctx.log_file.flush()
                    ctx.log_file.close()

                file_size = os.path.getsize(ctx.log_file.name)
                if file_size > 0:
                    display_cmd = ctx.cmd if len(ctx.cmd) <= 100 else ctx.cmd[:97] + "..."
                    await self.client.send_file(
                        chat_id,
                        file=ctx.log_file.name,
                        caption=f"📄 Полный вывод команды: {display_cmd}",
                    )
            except Exception as e:
                logger.error(f"Не удалось отправить файл лога: {e}")
