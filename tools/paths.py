# -*- coding: utf-8 -*-
"""项目内资源路径。

统一用绝对路径，避免 `open('libs/xxx.js')` 依赖当前工作目录 ——
Windows / Linux / macOS、以及从别的目录启动时行为一致（方案 #4）。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LIBS_DIR = PROJECT_ROOT / "libs"
DATA_DIR = PROJECT_ROOT / "data"


def libs_path(name: str) -> str:
    """libs/ 下资源文件的绝对路径。"""
    return str(LIBS_DIR / name)


def data_path(*parts: str) -> str:
    """data/ 下路径的绝对路径。"""
    return str(DATA_DIR.joinpath(*parts))
