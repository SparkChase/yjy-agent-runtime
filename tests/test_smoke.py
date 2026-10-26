"""包导入冒烟测试:保证项目可安装、可导入。"""

import yjy_agent_runtime


def test_package_imports() -> None:
    """包可以成功导入并暴露顶层命名空间。"""
    assert yjy_agent_runtime.__name__ == "yjy_agent_runtime"
