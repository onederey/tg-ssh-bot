"""
Юнит-тесты для проверки безопасности и разграничения прав доступа бота tg-ssh-bot.

Проверяют:
1. Авторизацию пользователей по ID (is_authorized) — белый список (whitelist).
2. Поведение при пустом списке разрешенных пользователей (принцип fail-closed).
3. Работу декоратора require_auth (блокировка неавторизованных пользователей и сообщений из групп/каналов).
4. Корректность парсинга списка разрешенных пользователей из параметров командной строки и .env (parse_users_list).
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

# Добавляем корневую директорию проекта в sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Безопасный импорт Telethon: если библиотека еще не установлена в тестовом окружении,
# создаем мок-объекты, сохраняя логику исключения StopPropagation
try:
    from telethon.events import StopPropagation
    import security
except ImportError:
    class StopPropagation(Exception):
        """Исключение Telethon для прерывания дальнейшей обработки события."""
        pass

    telethon_mock = MagicMock()
    telethon_mock.events.StopPropagation = StopPropagation
    sys.modules["telethon"] = telethon_mock
    sys.modules["telethon.events"] = telethon_mock.events
    sys.modules["telethon.tl"] = telethon_mock.tl
    sys.modules["telethon.tl.functions"] = telethon_mock.tl.functions
    sys.modules["telethon.tl.functions.bots"] = telethon_mock.tl.functions.bots
    sys.modules["telethon.tl.types"] = telethon_mock.tl.types

    import security
    from telethon.events import StopPropagation

import argparse
from config import parse_users_list


def run_coro(coro):
    """
    Вспомогательная функция для запуска корутины без необходимости создания
    системного цикла событий (asyncio event loop), что гарантирует переносимость
    и мгновенное выполнение тестов в любых средах (Linux, Windows, Termux).
    """
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value


class TestSecurityAuthorization(unittest.TestCase):
    """Тестирование функции проверки прав доступа is_authorized."""

    def setUp(self):
        self.allowed_users = [111111, 222222, 333333]

    def test_authorized_user_access_granted(self):
        """Пользователь, указанный в параметрах (белом списке), успешно авторизуется."""
        for user_id in self.allowed_users:
            with self.subTest(user_id=user_id):
                self.assertTrue(
                    security.is_authorized(user_id, self.allowed_users),
                    f"Пользователь с ID {user_id} из списка разрешенных должен иметь доступ.",
                )

    def test_unauthorized_user_access_denied(self):
        """Любой посторонний пользователь получает отказ в доступе."""
        unauthorized_users = [999999, 123456, 444444, 777777]
        for user_id in unauthorized_users:
            with self.subTest(user_id=user_id):
                self.assertFalse(
                    security.is_authorized(user_id, self.allowed_users),
                    f"Пользователь с ID {user_id} не указан в параметрах и должен быть заблокирован.",
                )

    def test_empty_allowed_users_blocks_everyone(self):
        """
        Если список разрешенных пользователей пуст (например, параметр не передан),
        доступ должен блокироваться абсолютно для всех ради безопасности (fail-closed).
        """
        empty_whitelist = []
        self.assertFalse(
            security.is_authorized(111111, empty_whitelist),
            "При пустом списке разрешенных пользователей доступ должен быть закрыт для всех.",
        )
        self.assertFalse(
            security.is_authorized(999999, empty_whitelist),
            "При пустом списке разрешенных пользователей доступ должен быть закрыт для всех.",
        )

    def test_none_or_zero_user_id_denied(self):
        """Запросы с отсутствующим или некорректным ID (None, 0) отклоняются."""
        self.assertFalse(security.is_authorized(None, self.allowed_users))
        self.assertFalse(security.is_authorized(0, self.allowed_users))


class TestRequireAuthDecorator(unittest.TestCase):
    """Тестирование защитного декоратора require_auth для Telegram-хендлеров."""

    def setUp(self):
        self.allowed_users = [111111, 222222]

    def test_authorized_user_in_private_chat_executes_handler(self):
        """Авторизованный пользователь в личном чате успешно вызывает целевой хендлер."""
        event = MagicMock()
        event.is_private = True
        event.sender_id = 111111

        executed = False

        async def dummy_handler(evt):
            nonlocal executed
            executed = True
            return "ok"

        wrapped = security.require_auth(self.allowed_users)(dummy_handler)
        result = run_coro(wrapped(event))

        self.assertTrue(executed, "Хендлер должен быть выполнен для разрешенного пользователя.")
        self.assertEqual(result, "ok")

    def test_unauthorized_user_is_silently_dropped(self):
        """
        Сообщение от неавторизованного пользователя молча отбрасывается
        (выбрасывается StopPropagation, хендлер не выполняется).
        """
        event = MagicMock()
        event.is_private = True
        event.sender_id = 999999

        executed = False

        async def dummy_handler(evt):
            nonlocal executed
            executed = True
            return "ok"

        wrapped = security.require_auth(self.allowed_users)(dummy_handler)

        with self.assertRaises(StopPropagation):
            run_coro(wrapped(event))

        self.assertFalse(executed, "Хендлер не должен выполняться для посторонних лиц.")

    def test_empty_allowed_users_drops_all_messages(self):
        """При пустом списке --users любые сообщения отбрасываются через StopPropagation."""
        event = MagicMock()
        event.is_private = True
        event.sender_id = 111111

        executed = False

        async def dummy_handler(evt):
            nonlocal executed
            executed = True
            return "ok"

        wrapped = security.require_auth([])(dummy_handler)

        with self.assertRaises(StopPropagation):
            run_coro(wrapped(event))

        self.assertFalse(executed, "Хендлер не должен вызываться, если список разрешенных пользователей пуст.")

    def test_group_chat_is_dropped_even_for_authorized_user(self):
        """
        Сообщения из публичных чатов/групп/каналов отбрасываются даже от доверенного пользователя
        во избежание утечки шелл-доступа в групповые чаты.
        """
        event = MagicMock()
        event.is_private = False
        event.sender_id = 111111

        executed = False

        async def dummy_handler(evt):
            nonlocal executed
            executed = True
            return "ok"

        wrapped = security.require_auth(self.allowed_users)(dummy_handler)

        with self.assertRaises(StopPropagation):
            run_coro(wrapped(event))

        self.assertFalse(executed, "Команды из групп и каналов должны быть заблокированы.")


class TestConfigUsersParsing(unittest.TestCase):
    """Тестирование парсинга списка разрешенных пользователей из CLI параметров и .env."""

    def test_parse_users_list_comma_separated(self):
        """Парсинг ID пользователей, разделенных запятыми."""
        result = parse_users_list("111111, 222222, 333333")
        self.assertEqual(result, [111111, 222222, 333333])

    def test_parse_users_list_space_separated(self):
        """Парсинг ID пользователей, разделенных пробелами."""
        result = parse_users_list("111111 222222 333333")
        self.assertEqual(result, [111111, 222222, 333333])

    def test_parse_users_list_mixed_whitespace_and_commas(self):
        """Парсинг ID пользователей со смешанными пробелами и запятыми."""
        result = parse_users_list(" 111111 ,  222222   333333,444444 ")
        self.assertEqual(result, [111111, 222222, 333333, 444444])

    def test_parse_users_list_empty(self):
        """Пустая строка возвращает пустой список."""
        self.assertEqual(parse_users_list(""), [])
        self.assertEqual(parse_users_list("   "), [])

    def test_parse_users_list_invalid_value_raises_error(self):
        """Нечисловые значения приводят к ошибке конфигурации (не пропускаются молча)."""
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_users_list("111111, invalid_user_id, 222222")


if __name__ == "__main__":
    unittest.main()
