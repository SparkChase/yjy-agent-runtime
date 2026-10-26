"""文档字数预算门禁。

对照 scripts/doc-budgets.manifest.json 的词数上限检查常驻文档,
防止规则与说明在单文件内无限堆积。超限时的处置顺序:
先搬运到归属文档、再压缩措辞,确需空间才在提交说明中论证提高上限。

用法:uv run python scripts/verify_doc_budgets.py [--list]
退出码:0 全部在预算内;1 存在超限、缺失或非法上限。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "scripts" / "doc-budgets.manifest.json"


def count_words(text: str) -> int:
    """按空白切分计数,等价于 wc -w。"""
    return len(text.split())


def main() -> int:
    """执行预算检查;--list 打印用量明细并返回 0,不作为门禁结论。"""
    manifest: dict[str, int] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    rows: list[str] = []
    for path, ceiling in manifest.items():
        # bool 是 int 的子类,JSON 的 true 会骗过 isinstance(int)
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
            failures.append(f"{path}: 上限必须是正整数,当前为 {ceiling!r}")
            rows.append(f"BAD   {'—':>6} / {ceiling!s:<6} {path}")
            continue
        document = ROOT / path
        if not document.is_file():
            failures.append(f"{path}: 预算清单中的文件不存在(改名或删除需同步更新清单)")
            rows.append(f"MISS  {'—':>6} / {ceiling!s:<6} {path}")
            continue
        words = count_words(document.read_text(encoding="utf-8"))
        status = "ok" if words <= ceiling else "OVER"
        rows.append(f"{status:<4} {words:>6} / {ceiling:<6} {path}")
        if words > ceiling:
            failures.append(f"{path}: {words} 词超出 {ceiling} 上限,先搬运或压缩")
    if "--list" in sys.argv:
        print("\n".join(rows))
        return 0
    if failures:
        print("verify-doc-budgets 未通过:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"verify-doc-budgets: {len(manifest)} 份文档均在词数预算内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
