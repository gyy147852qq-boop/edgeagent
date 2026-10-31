"""路径解析测试：源码模式、数据目录覆盖、资源路径返回类型。

打包（PyInstaller）无法在单测里真实模拟 ``sys._MEIPASS``，这里覆盖可测的契约：
``data_dir`` 尊重 ``EDGEAGENT_DATA_DIR``、``resource_path`` 在源码模式指向仓库根、
``get_resource_path`` 返回字符串。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from server import paths


def test_resource_path_points_to_repo_root() -> None:
    root = paths.resource_path()
    assert (root / "server" / "main.py").is_file()
    assert (root / "web" / "index.html").is_file()


def test_get_resource_path_returns_string() -> None:
    value = paths.get_resource_path("web/index.html")
    assert isinstance(value, str)
    assert value.endswith("index.html")
    assert Path(value).is_file()


def test_data_dir_env_override() -> None:
    base = Path(__file__).resolve().parent.parent / ".pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    target = tempfile.mkdtemp(prefix="data_", dir=str(base))
    original = os.environ.get("EDGEAGENT_DATA_DIR")
    os.environ["EDGEAGENT_DATA_DIR"] = target
    paths.reset_cache()
    try:
        resolved = paths.data_dir()
        assert os.path.normcase(str(resolved)) == os.path.normcase(os.path.realpath(target))
        assert resolved.is_dir()
    finally:
        paths.reset_cache()
        if original is None:
            os.environ.pop("EDGEAGENT_DATA_DIR", None)
        else:
            os.environ["EDGEAGENT_DATA_DIR"] = original
        shutil.rmtree(target, ignore_errors=True)


def test_is_frozen_false_in_source_mode() -> None:
    assert paths.is_frozen() is False
