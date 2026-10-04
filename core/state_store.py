"""已审 PR 游标存储：LRU 上限、原子写盘、协程安全。"""

import asyncio
import json
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger

_MAX_ENTRIES = 500


class CursorStore:
    """记录 {cursor_key: head_sha}，防止对同一 head 重复审查。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: OrderedDict[str, str] = OrderedDict()
        self._load()

    def _load(self) -> None:
        """启动加载；JSON 损坏时备份后从零开始，不阻断插件启动。"""

        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = OrderedDict(
                    (str(k), str(v)) for k, v in raw.items()
                )
        except (json.JSONDecodeError, OSError) as exc:
            backup = self._path.with_suffix(".json.bak")
            logger.warning(f"[gh-review] 游标文件损坏，备份至 {backup}: {exc}")
            try:
                self._path.replace(backup)
            except OSError:
                pass

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str) -> str | None:
        """读取游标（同时刷新 LRU 热度）。"""

        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    async def mark(self, key: str, head_sha: str) -> None:
        """记录已审 head_sha 并落盘；超出上限淘汰最旧条目。"""

        async with self._lock:
            self._data[key] = head_sha
            self._data.move_to_end(key)
            while len(self._data) > _MAX_ENTRIES:
                self._data.popitem(last=False)
            await asyncio.to_thread(self._save)

    def _save(self) -> None:
        """原子写入：tmp 文件 + replace，防崩溃丢游标。"""

        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self._path)
