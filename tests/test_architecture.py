"""分层边界的架构测试。

这些测试只做一件事：用静态方式解析源码里的 import，确认依赖方向始终是

    interfaces -> infrastructure -> agent -> domain

它不检查运行期行为，只防止某一层在后续开发中悄悄反向依赖。这样"应用层
不绑定具体模型/数据库/命令行实现"这条约束由测试守护，而不是只写在文档里。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "yjy_agent_runtime"

# 每层允许依赖的内部层。: 越靠前的层可以依赖越靠后的层。
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    "domain": set(),
    "agent": {"domain"},
    "infrastructure": {"domain", "agent"},
    "interfaces": {"domain", "agent", "infrastructure"},
}

# 迁移后不应再出现的顶层包名；它们的实现已经并入 infrastructure/。
REMOVED_PACKAGES = {"llm", "session", "tools", "trace", "context"}


def _layer_of(path: Path) -> str | None:
    """返回文件所属的顶层分层名；包根文件返回 None。"""

    relative = path.relative_to(PACKAGE)
    if len(relative.parts) < 2:
        return None
    layer = relative.parts[0]
    return layer if layer in ALLOWED_DEPENDENCIES else None


def _source_files() -> list[Path]:
    """按路径序返回包内所有 Python 源文件。"""

    return sorted(PACKAGE.rglob("*.py"))


def _imported_package_modules(path: Path) -> set[str]:
    """提取文件中绝对导入的 yjy_agent_runtime 子模块名。

    只关心 `import yjy_agent_runtime.x` 与 `from yjy_agent_runtime.x import ...`
    两种写法；相对导入停留在同一层内部，不参与跨层判断。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.startswith(
                "yjy_agent_runtime."
            ):
                modules.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("yjy_agent_runtime."):
                    modules.add(alias.name.split(".")[1])
    return modules


def test_dependency_direction_between_layers() -> None:
    """任何一层都只能依赖自己允许的下游层，禁止反向依赖。"""

    violations: list[str] = []
    for path in _source_files():
        layer = _layer_of(path)
        if layer is None:
            continue
        for imported in _imported_package_modules(path):
            if imported not in ALLOWED_DEPENDENCIES:
                continue
            if imported == layer:
                continue
            if imported not in ALLOWED_DEPENDENCIES[layer]:
                violations.append(
                    f"{path.relative_to(ROOT)}: {layer} 不得依赖 {imported}"
                )

    assert violations == [], "发现反向分层依赖：\n" + "\n".join(violations)


def test_domain_layer_stays_isolated_from_io_layers() -> None:
    """领域层只描述事实，不导入应用、基建或入口层。"""

    offenders: list[str] = []
    for path in sorted((PACKAGE / "domain").rglob("*.py")):
        for imported in _imported_package_modules(path):
            if imported in {"agent", "infrastructure", "interfaces"}:
                offenders.append(f"{path.relative_to(ROOT)}: 导入 {imported}")

    assert offenders == [], "领域层出现内部依赖：\n" + "\n".join(offenders)


def test_removed_top_level_packages_are_not_imported() -> None:
    """确认没有任何代码继续引用已迁移的顶层占位包。"""

    offenders: list[str] = []
    for path in _source_files():
        for imported in _imported_package_modules(path):
            if imported in REMOVED_PACKAGES:
                offenders.append(f"{path.relative_to(ROOT)}: 导入 {imported}")

    assert offenders == [], "仍在引用已删除的空包：\n" + "\n".join(offenders)


def test_removed_top_level_packages_do_not_exist() -> None:
    """空占位目录必须清理干净，避免读者误以为核心模块缺失。"""

    leftovers = [
        name for name in sorted(REMOVED_PACKAGES) if (PACKAGE / name).exists()
    ]

    assert leftovers == [], f"仍存在空占位包：{leftovers}"
