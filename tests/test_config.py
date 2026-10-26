"""入口层配置加载测试。

这些测试只覆盖 `.env` 解析、注入优先级和错误语义，不访问网络、不读取真实密钥。
"""

from pathlib import Path

import pytest

from yjy_agent_runtime.interfaces.config import (
    DEFAULT_ENV_FILE,
    EnvFileError,
    parse_env_file,
    prepare_environment,
)


def test_parse_env_file_supports_export_quotes_and_comments() -> None:
    text = (
        "# 注释行\n"
        "\n"
        "LLM_BASE_URL=https://env.example/v1\n"
        "export LLM_MODEL='quoted-model'\n"
        'LLM_API_KEY="secret-value"\n'
        "MALFORMED_LINE_WITHOUT_EQUALS"
    )

    values = parse_env_file(text)

    assert values == {
        "LLM_BASE_URL": "https://env.example/v1",
        "LLM_MODEL": "quoted-model",
        "LLM_API_KEY": "secret-value",
    }


def test_prepare_environment_loads_file_without_overriding_existing(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL=file-model\nLLM_API_KEY=file-key\n", encoding="utf-8")
    environ = {"LLM_MODEL": "process-model"}

    result = prepare_environment(
        env_file=str(env_file),
        environ=environ,
    )

    # 已存在的进程变量优先，文件只补齐缺失的键。
    assert environ["LLM_MODEL"] == "process-model"
    assert environ["LLM_API_KEY"] == "file-key"
    assert result.loaded_keys == ("LLM_API_KEY",)
    assert result.env_file == env_file


def test_prepare_environment_skips_missing_default_file(tmp_path: Path) -> None:
    environ: dict[str, str] = {}

    result = prepare_environment(
        environ=environ,
        default_env_file=tmp_path / ".env",
    )

    assert result.env_file is None
    assert result.loaded_keys == ()
    assert environ == {}


def test_prepare_environment_raises_for_missing_explicit_file(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError, match="not found"):
        prepare_environment(
            env_file=str(tmp_path / "missing.env"),
            environ={},
        )


def test_prepare_environment_can_be_disabled() -> None:
    environ: dict[str, str] = {}

    result = prepare_environment(
        use_env_file=False,
        environ=environ,
        default_env_file=Path("definitely-not-read.env"),
    )

    assert result.env_file is None
    assert environ == {}


def test_default_env_file_points_at_current_directory() -> None:
    assert DEFAULT_ENV_FILE == Path(".env")
