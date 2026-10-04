"""配置解析、游标存储、Prompt 存储测试。"""

import json
import time

import pytest

from core.prompt_store import _FALLBACK, PromptStore
from core.state_store import _MAX_ENTRIES, CursorStore
from models import PluginConfig


class TestPluginConfig:
    def test_defaults(self, config):
        assert config.github.poll_interval_seconds == 300
        assert config.github.guideline_files == ["CONTRIBUTING.md"]
        assert config.review.level == "normal"
        assert config.whitelist.whitelist_action == "skip"
        assert config.security.auto_close_on_injection is True
        assert config.security.close_requires == "any"

    def test_missing_token_raises(self, config_dict):
        config_dict["github"]["token"] = "  "
        with pytest.raises(ValueError, match="token"):
            PluginConfig.from_dict(config_dict)

    def test_empty_repos_raises(self, config_dict):
        config_dict["github"]["repositories"] = []
        with pytest.raises(ValueError, match="repositories"):
            PluginConfig.from_dict(config_dict)

    def test_bad_repo_format_raises(self, config_dict):
        config_dict["github"]["repositories"] = ["no-slash-here"]
        with pytest.raises(ValueError, match="owner/repo"):
            PluginConfig.from_dict(config_dict)

    def test_bad_level_raises(self, config_dict):
        config_dict["review"]["level"] = "extreme"
        with pytest.raises(ValueError, match="level"):
            PluginConfig.from_dict(config_dict)

    def test_bad_whitelist_action_raises(self, config_dict):
        config_dict["whitelist"]["whitelist_action"] = "ignore"
        with pytest.raises(ValueError, match="whitelist_action"):
            PluginConfig.from_dict(config_dict)

    def test_bad_close_requires_raises(self, config_dict):
        config_dict["security"]["close_requires"] = "all"
        with pytest.raises(ValueError, match="close_requires"):
            PluginConfig.from_dict(config_dict)

    def test_poll_interval_floor(self, config_dict):
        config_dict["github"]["poll_interval_seconds"] = 5
        assert PluginConfig.from_dict(config_dict).github.poll_interval_seconds == 60

    def test_string_list_fields_accept_csv(self, config_dict):
        config_dict["github"]["repositories"] = "a/b, c/d"
        config_dict["whitelist"]["users"] = "alice, bob"
        cfg = PluginConfig.from_dict(config_dict)
        assert cfg.github.repositories == ["a/b", "c/d"]
        assert cfg.whitelist.users == ["alice", "bob"]

    def test_retries_floor(self, config_dict):
        config_dict["review"]["llm_max_retries"] = 0
        assert PluginConfig.from_dict(config_dict).review.llm_max_retries == 1


class TestCursorStore:
    async def test_mark_get_and_persist(self, tmp_path):
        path = tmp_path / "cursor.json"
        store = CursorStore(path)
        await store.mark("octo/demo#1", "sha1")
        assert store.get("octo/demo#1") == "sha1"

        reloaded = CursorStore(path)
        assert reloaded.get("octo/demo#1") == "sha1"

    async def test_corrupted_file_backed_up(self, tmp_path):
        path = tmp_path / "cursor.json"
        path.write_text("{not json", encoding="utf-8")
        store = CursorStore(path)
        assert len(store) == 0
        assert (tmp_path / "cursor.json.bak").exists()
        # 损坏后仍可正常写入
        await store.mark("k", "v")
        assert store.get("k") == "v"

    async def test_lru_eviction(self, tmp_path):
        store = CursorStore(tmp_path / "cursor.json")
        for i in range(_MAX_ENTRIES + 10):
            await store.mark(f"r#{i}", f"s{i}")
        assert len(store) == _MAX_ENTRIES
        assert store.get("r#0") is None  # 最旧被淘汰
        assert store.get(f"r#{_MAX_ENTRIES + 9}") is not None

    def test_json_shape(self, tmp_path):
        path = tmp_path / "cursor.json"
        path.write_text(json.dumps({"a#1": "x"}), encoding="utf-8")
        assert CursorStore(path).get("a#1") == "x"


class TestPromptStore:
    def test_loads_repo_templates(self):
        store = PromptStore(_repo_prompt_dir())
        for name in ("system_loose", "system_normal", "system_strict", "user_template"):
            text = store.get(name)
            assert len(text) > 50, f"{name} 模板内容异常"
            assert text != _FALLBACK[name], f"{name} 未加载到外置模板"

    def test_missing_file_uses_fallback(self, tmp_path):
        store = PromptStore(tmp_path)
        assert store.get("system_normal") == _FALLBACK["system_normal"]

    def test_hot_reload_on_mtime_change(self, tmp_path):
        path = tmp_path / "system_normal.md"
        path.write_text("v1", encoding="utf-8")
        store = PromptStore(tmp_path)
        assert store.get("system_normal") == "v1"

        time.sleep(0.02)  # 确保 mtime 变化
        path.write_text("v2", encoding="utf-8")
        mtime = path.stat().st_mtime
        # 某些文件系统 mtime 粒度较粗，强制设置
        import os

        os.utime(path, (mtime + 2, mtime + 2))
        assert store.get("system_normal") == "v2"

    def test_reload_clears_cache(self, tmp_path):
        path = tmp_path / "system_normal.md"
        path.write_text("v1", encoding="utf-8")
        store = PromptStore(tmp_path)
        store.get("system_normal")
        store.reload()
        path.unlink()
        assert store.get("system_normal") == _FALLBACK["system_normal"]


def _repo_prompt_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "prompts"
