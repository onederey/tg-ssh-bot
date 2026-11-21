"""
Модуль управления избранными командами (сохранение, удаление и быстрый запуск через инлайн-кнопки).
"""

import json
import logging
import os
from typing import Dict, List
from telethon.tl.custom import Button

logger = logging.getLogger("tg_ssh_bot.favorites")


class FavoritesManager:
    def __init__(self, storage_path: str = "favorites.json"):
        self.storage_path = os.path.abspath(storage_path)
        self.favorites: Dict[str, Dict[str, str]] = {}  # {user_id_str: {alias: command}}
        self._load()

    def _load(self):
        """Загружает сохраненные команды из JSON-файла."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.favorites = json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки favorites.json: {e}")
                self.favorites = {}
        else:
            self.favorites = {}

    def _save(self):
        """Сохраняет команды в JSON-файл атомарно."""
        tmp_path = f"{self.storage_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.favorites, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.storage_path)
        except Exception as e:
            logger.error(f"Ошибка сохранения favorites.json: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def add_favorite(self, user_id: int, alias: str, command: str) -> bool:
        """Добавляет команду в избранное пользователя."""
        uid = str(user_id)
        if uid not in self.favorites:
            self.favorites[uid] = {}
        self.favorites[uid][alias] = command
        self._save()
        return True

    def delete_favorite(self, user_id: int, alias: str) -> bool:
        """Удаляет команду из избранного."""
        uid = str(user_id)
        if uid in self.favorites and alias in self.favorites[uid]:
            del self.favorites[uid][alias]
            self._save()
            return True
        return False

    def get_favorites(self, user_id: int) -> Dict[str, str]:
        """Возвращает словарь избранных команд пользователя {alias: command}."""
        return self.favorites.get(str(user_id), {})

    def get_keyboard(self, user_id: int) -> List[List[Button]]:
        """Генерирует инлайн-клавиатуру для быстрого запуска избранных команд."""
        favs = self.get_favorites(user_id)
        if not favs:
            return []

        rows: List[List[Button]] = []
        current_row: List[Button] = []

        for alias in sorted(favs.keys()):
            # Лимит Telegram на callback_data равен 64 байтам ("fav_run:" занимает 8 байт)
            alias_bytes = alias.encode("utf-8")
            if len(alias_bytes) > 50:
                cb_alias = alias_bytes[:50].decode("utf-8", errors="ignore")
            else:
                cb_alias = alias
            btn = Button.inline(f"⚡ {alias}", data=f"fav_run:{cb_alias}")
            current_row.append(btn)
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []

        if current_row:
            rows.append(current_row)

        return rows
