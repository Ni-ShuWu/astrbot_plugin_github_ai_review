# 贡献指南（CONTRIBUTING.md）

感谢关注 `astrbot_plugin_github_ai_review`！

> 有趣的事实：本插件自身的功能就是**审查 PR 是否符合仓库的 CONTRIBUTING.md**——所以本仓库的 PR 会被自己的插件审查。请遵守下面的规范，你的 PR 会更顺利通过。

## 开发环境

- Python **3.11+**
- 依赖安装：

```bash
pip install -r requirements.txt
pip install pytest pytest-asyncio ruff  # 开发依赖
```

- 运行测试：

```bash
pytest            # 全量测试，提交前必须全部通过
ruff check .      # 静态检查，必须无告警
```

## 提交 PR 前

1. **同步最新 main**：基于最新 `main` 分支开发，避免无谓冲突
2. **测试**：为修复/新功能补充回归测试（本仓库测试桩位于 `tests/conftest.py`，模拟 AstrBot v4.27.5 行为）
3. **自查**：

```bash
pytest && ruff check .
```

4. **PR 描述**：写清楚「改了什么 / 为什么改 / 如何验证」，涉及行为变化请贴前后对比

## 代码规范

- **模块化**：单一职责，文件不超过 ~300 行；禁止把逻辑堆进 `main.py`
- **全异步**：所有 I/O 使用 `async/await`；GitHub API 调用统一走 `github/client.py`
- **类型注解**：公共函数与 dataclass 必须带类型注解
- **Prompt 外置**：提示词放在 `prompts/*.md`，不写死在 Python 里；支持 `/pr_review reload` 热加载
- **配置集中**：所有可调项进 `_conf_schema.json`，提供合理默认值
- **异常必须处理**：网络/LLM/解析层异常就地捕获并记录，不让任务静默失败
- **日志**：统一 `from astrbot.api import logger`，禁止 `print`
- **Adapter 模式**：如需依赖其它插件，走 Adapter 隔离，禁止直接 import

## 兼容性红线

改动涉及 AstrBot API 时，请对照 [AstrBot v4.27.5 源码](https://github.com/AstrBotDevs/AstrBot/tree/v4.27.5) 核实签名，特别注意：

- `Star.__init__` **不会**注入 `self.config`，配置需在 `__init__` 自行保存
- `@filter.command_group(...)` 必须是最外层装饰器
- 优先使用 `Context.get_using_provider_async()`（v4.27 起同步版已 deprecated）

测试桩 `tests/conftest.py` 的 `Star` 模拟保持与上述行为一致，改动需同步。

## 提交信息

- 使用祈使句、中文或英文均可，例如 `fix: 处理 404 时跳过游标更新`
- 一个 PR 聚焦一件事；顺手修的无关问题请拆分提交或另开 PR

## 版本与发布

- 行为变更需在 `metadata.yaml` 中递增版本号（`vMAJOR.MINOR.PATCH`）
- 维护者合并后打 tag 并发布 Release，说明修复内容与升级注意事项

## 报告 Bug

请附上：AstrBot 版本、插件版本（`metadata.yaml`）、完整错误堆栈、复现步骤。

## 许可

提交即表示同意代码以 [AGPL-3.0](LICENSE) 许可发布。
