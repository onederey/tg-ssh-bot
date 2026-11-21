# tg-ssh-bot

Telegram-бот на базе Telethon для выполнения shell-команд и управления Termux (Android) или Linux через Telegram. Поддерживает работу через прокси (MTProto, SOCKS5).

## Возможности

- Выполнение shell-команд с потоковым обновлением вывода (PTY) и возможностью прерывания процесса
- Скачивание файлов с сервера (`/get`, до 2 ГБ) и загрузка файлов на сервер
- Навигация по файловой системе (`/cd`, `/pwd`, `/ls`)
- Информация о системе (`/status`, обертка над `fastfetch`)
- Список избранных команд с запуском через inline-кнопки (`/fav`)
- Белый список пользователей по ID (сообщения от остальных пользователей игнорируются)
- Поддержка MTProto-прокси (`tg-ws-proxy`) и SOCKS5

## Требования

- Python 3.10+
- Telegram API ID и Hash (my.telegram.org)
- Токен бота (@BotFather)

## Установка

```bash
git clone https://github.com/onederey/tg-ssh-bot.git
cd tg-ssh-bot
pip install -r requirements.txt
```

В Termux перед установкой зависимостей установите пакеты:
```bash
pkg update && pkg install -y python clang libffi openssl git
```

## Настройка и запуск

Параметры можно передать через `.env` или аргументы командной строки.

### Использование .env

Скопируйте пример конфига:
```bash
cp .env.example .env
```

Заполните переменные в `.env`:
```env
BOT_TOKEN=123456789:ABCdef...
API_ID=1234567
API_HASH=abcdef...
ALLOWED_USERS=123456789

# Необязательно (для прокси):
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_SECRET=ee000000...
```

Запуск:
```bash
python main.py
```

### Аргументы командной строки

Пример прямого запуска:
```bash
python main.py -t "BOT_TOKEN" -i 1234567 -H "API_HASH" -u 123456789
```

Пример с прокси:
```bash
python main.py -t "BOT_TOKEN" -i 1234567 -H "API_HASH" -u 123456789 \
  --proxy-host 127.0.0.1 --proxy-port 1080 --secret "HEX_SECRET"
```

| Аргумент | Флаг | Переменная .env | Описание | По умолчанию |
| --- | --- | --- | --- | --- |
| `--token` | `-t` | `BOT_TOKEN` | Токен бота от BotFather | Обязателен |
| `--api-id` | `-i` | `API_ID` | Telegram API ID | Обязателен |
| `--api-hash` | `-H` | `API_HASH` | Telegram API Hash | Обязателен |
| `--users` | `-u` | `ALLOWED_USERS` | Список Telegram ID пользователей | `[]` |
| `--secret` | `-s` | `PROXY_SECRET` | Секрет MTProto-прокси | `None` |
| `--proxy-host` | | `PROXY_HOST` | Хост прокси | `127.0.0.1` |
| `--proxy-port` | `-p` | `PROXY_PORT` | Порт прокси | `1080` |
| `--proxy-type` | | `PROXY_TYPE` | Тип прокси: auto, mtproto, socks5, none | `auto` |
| `--workdir` | `-w` | `WORKDIR` | Начальная рабочая директория | `$HOME` |
| `--session` | | `SESSION_NAME` | Имя файла сессии | `bot_session` |
| `--log-level` | | `LOG_LEVEL` | Уровень логов: DEBUG, INFO, WARNING, ERROR | `INFO` |
| `--log-file` | | `LOG_FILE` | Файл для записи логов (по умолчанию stdout) | `None` |

## Команды бота

- `<команда>` или `/run <команда>` — выполнить shell-команду в Unix-минималистичном интерфейсе. Кнопка «⏹ Прервать (Ctrl+C)» или `/kill` прерывают выполнение. После завершения доступны кнопки «🔄 Повторить» и «📋 Команда».
- `/pwd` — текущая директория.
- `/cd <путь>` — сменить директорию.
- `/ls [путь]` — список файлов и папок.
- `/status` — информация о системе (быстрый статус через fastfetch).
- `/get <путь>` — скачать файл с сервера в Telegram.
- Отправка файла в чат — загрузить файл на сервер (в подписи можно передать `/save <путь>`).
- `/fav` — список избранных команд.
- `/fav add <имя> <команда>` — сохранить команду в избранное.
- `/fav del <имя>` — удалить команду из избранного.

## Пример настройки для запуска через termux-services

Пакет `termux-services` позволяет запускать бота как фоновый сервис с автозапуском и контролем через менеджер процессов `runit`.

### 1. Установка termux-services

```bash
pkg install termux-services
```

> **Важно:** после установки перезапустите приложение Termux (закройте через команду `exit` или выгрузите из памяти), чтобы запустился сервис-демон `runsvdir`.

### 2. Создание структуры директорий сервиса

Создайте каталог сервиса (например, `tg-ssh-bot`):

```bash
mkdir -p $PREFIX/var/service/tg-ssh-bot/log
```

Склонируйте репозиторий:

```bash
git clone https://github.com/onederey/tg-ssh-bot.git  /data/data/com.termux/files/home/apps/tg-ssh-bot
```

### 3. Создание скрипта запуска (`run`)

Создайте файл `$PREFIX/var/service/tg-ssh-bot/run`:

```bash
cat << 'EOF' > $PREFIX/var/service/tg-ssh-bot/run
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
cd /data/data/com.termux/files/home/apps/tg-ssh-bot
exec python main.py
EOF

chmod +x $PREFIX/var/service/tg-ssh-bot/run
```

*(Убедитесь, что путь в `cd` соответствует реальному пути к папке с ботом на вашем устройстве).*

### 4. Настройка логирования (опционально)

Создайте файл `$PREFIX/var/service/tg-ssh-bot/log/run` для автоматической ротации логов утилитой `svlogd`:

```bash
cat << 'EOF' > $PREFIX/var/service/tg-ssh-bot/log/run
#!/data/data/com.termux/files/usr/bin/sh
exec svlogd -tt /data/data/com.termux/files/home/tg-ssh-bot/logs
EOF

chmod +x $PREFIX/var/service/tg-ssh-bot/log/run
mkdir -p /data/data/com.termux/files/home/tg-ssh-bot/logs
```

### 5. Управление сервисом

- **Включить автозапуск сервиса:**
  ```bash
  sv-enable tg-ssh-bot
  ```
- **Запустить сервис:**
  ```bash
  sv up tg-ssh-bot
  ```
- **Проверить статус работы:**
  ```bash
  sv status tg-ssh-bot
  ```
- **Перезапустить сервис:**
  ```bash
  sv restart tg-ssh-bot
  ```
- **Остановить сервис:**
  ```bash
  sv down tg-ssh-bot
  ```
- **Отключить автозапуск:**
  ```bash
  sv-disable tg-ssh-bot
  ```
