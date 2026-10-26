"""pytest 全局配置。

默认门禁绝不能访问真实网络：既避免把真实费用带进日常回归，也避免开发者本地
`.env` 里的密钥被意外使用。真实 LLM 验收需要显式选择，因此这里在"用户没有
自己指定 `-m`"时自动加上 `-m "not integration"`。

显式执行 `pytest -m integration` 时不会被覆盖，真实测试照常运行。
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """未显式给出 -m 表达式时，默认排除 integration 标记。"""

    mark_expression = config.getoption("markexpr", default="")
    if not mark_expression:
        config.option.markexpr = "not integration"
