"""Markdown 本地链接检查门禁。

扫描仓库内所有 Markdown 文件,提取行内链接、图片与引用式链接定义,
校验相对路径目标指向的文件或目录存在;外链、纯锚点、绝对路径不检查。
fenced code block 内的文本不参与提取。

用法:uv run python scripts/verify_md_links.py
退出码:0 全部通过;1 存在失效本地链接。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 不参与扫描的目录名:版本库、虚拟环境、IDE 配置与缓存
EXCLUDED_DIR_NAMES = {".git", ".venv", ".idea", "__pycache__", "node_modules"}

# 行内链接与图片的 target 部分,兼容 <target> 形式
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s<>]+)")
# 单行引用式链接定义 [label]: target
REFERENCE_DEFINITION = re.compile(r"\s{0,3}\[[^\]]+\]:\s*<?([^\s<>]+)")

FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def is_checkable_target(target: str) -> bool:
    """仅仓库内相对路径需要校验;外链、锚点与绝对路径跳过。"""
    if target.startswith(("#", "/")):
        return False
    # http:、https:、mailto: 等带协议前缀的外部目标
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) is None


def strip_fragment(target: str) -> str:
    """去掉 #fragment 与 ?query,只保留路径部分。"""
    return re.split(r"[?#]", target, maxsplit=1)[0]


def markdown_files() -> list[Path]:
    """按路径序返回仓库内参与扫描的 Markdown 文件。"""
    return [
        path
        for path in sorted(ROOT.rglob("*.md"))
        if not any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(ROOT).parts)
    ]


def check_document(path: Path) -> list[tuple[int, str, str]]:
    """检查单个 Markdown 文件,返回 (行号, 原始目标, 解析后路径) 失效列表。

    围栏用 ` 或 ~ 开区分,只有同类标记能闭合,围栏内的行不提取链接。
    """
    failures: list[tuple[int, str, str]] = []
    fence_marker: str | None = None
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fence = FENCE_LINE.match(line)
        if fence is not None:
            marker = fence.group(1)[0]
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        targets = [match.group(1) for match in INLINE_LINK.finditer(line)]
        definition = REFERENCE_DEFINITION.match(line)
        if definition is not None:
            targets.append(definition.group(1))
        for target in targets:
            if not is_checkable_target(target):
                continue
            resolved = (path.parent / strip_fragment(target)).resolve()
            if not resolved.exists():
                failures.append((line_no, target, str(resolved.relative_to(ROOT))))
    return failures


def main() -> int:
    """执行全量检查并汇报,存在失效本地链接时返回 1。"""
    files = markdown_files()
    total_failures = 0
    for path in files:
        for line_no, target, resolved in check_document(path):
            print(f"{path.relative_to(ROOT)}:{line_no}: 失效本地链接 {target!r} -> {resolved}")
            total_failures += 1
    if total_failures > 0:
        print(f"verify-md-links: {total_failures} 个失效本地链接")
        return 1
    print(f"verify-md-links: {len(files)} 个 Markdown 文件本地链接全部有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
