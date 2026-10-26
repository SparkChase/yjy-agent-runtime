"""入口层配置加载。

职责只有两件事：

1. 把本地 `.env` 文本注入进程环境变量，方便 `uv run yjy-agent-runtime ...` 直接
   读取真实服务配置，而不必每次手动 `export`。
2. 提供 CLI 与真实集成测试共用的加载入口，保证"测试时能连通"等于"CLI 也能连通"。

真正的配置校验仍由基建层的 `OpenAICompatibleConfig` 负责；这里只做文件解析与
注入，不校验业务字段，也不打印任何密钥。

安全边界：
- 已存在的环境变量优先，`.env` 不覆盖它们，便于 CI 用显式变量覆盖本地文件。
- 只注入键值，不回显文件内容；错误信息里也不包含值。
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

# 默认从当前工作目录读取 `.env`。
DEFAULT_ENV_FILE = Path(".env")


class EnvFileError(ValueError):
    """显式指定的 `.env` 文件不存在或无法解析。"""


def parse_env_file(text: str) -> dict[str, str]:
    """把 dotenv 文本解析成键值对。

    支持 `KEY=VALUE`、`export KEY=VALUE`、`#` 注释、空行，以及被单双引号包裹的值。
    解析器故意保持最小：不处理多行值、变量插值和命令替换，避免引入隐式行为。
    """

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """把文件中的键注入环境变量，返回实际注入的键名。

    已存在于 `environ` 的键不会被覆盖；这样显式环境变量始终优先于本地文件。
    """

    target = os.environ if environ is None else environ
    if not path.is_file():
        raise EnvFileError(f"env file not found: {path}")
    loaded: list[str] = []
    for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
        if key in target:
            continue
        target[key] = value
        loaded.append(key)
    return tuple(loaded)


@dataclass(frozen=True, slots=True)
class EnvFileLoadResult:
    """一次环境文件解析的可审计结果，不含任何值。"""

    env_file: Path | None
    loaded_keys: tuple[str, ...]


def prepare_environment(
    *,
    env_file: str | None = None,
    use_env_file: bool = True,
    environ: MutableMapping[str, str] | None = None,
    default_env_file: Path = DEFAULT_ENV_FILE,
) -> EnvFileLoadResult:
    """按 CLI 参数把 `.env` 注入环境变量。

    - `use_env_file=False`（`--no-env-file`）：完全不读取文件。
    - 显式传入 `env_file`：文件必须存在，否则报错，避免"以为加载了其实没有"。
    - 未显式传入：存在默认 `.env` 才加载，缺失时静默跳过，交给配置校验报错。
    """

    if not use_env_file:
        return EnvFileLoadResult(env_file=None, loaded_keys=())
    path = Path(env_file) if env_file is not None else default_env_file
    if not path.exists():
        if env_file is not None:
            raise EnvFileError(f"env file not found: {path}")
        return EnvFileLoadResult(env_file=None, loaded_keys=())
    loaded = load_env_file(path, environ=environ)
    return EnvFileLoadResult(env_file=path, loaded_keys=loaded)
