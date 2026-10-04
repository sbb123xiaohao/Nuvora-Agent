"""Python 代码执行工具：子进程运行，超时控制。

边界说明：这是「子进程级」隔离（独立解释器、-I 隔离模式、超时终止、
工作目录限定在 workspace/），并非容器级沙箱。生产环境建议换用
容器/微虚拟机执行。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MAX_CODE_CHARS = 20000
MAX_TIMEOUT = 120
MAX_OUTPUT_CHARS = 4000


def run_python_code(code: str, timeout: int, cwd: Path) -> str:
    """在 workspace/ 内执行 Python 代码，返回 stdout/stderr 汇总。"""
    if not code.strip():
        return "（代码为空，未执行）"
    if len(code) > MAX_CODE_CHARS:
        return f"（代码过长：{len(code)} 字符，上限 {MAX_CODE_CHARS}，拒绝执行）"
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return f"执行超时（超过 {timeout} 秒），进程已终止。"
    parts: list[str] = []
    if proc.stdout and proc.stdout.strip():
        parts.append("[stdout]\n" + proc.stdout.strip()[:MAX_OUTPUT_CHARS])
    if proc.stderr and proc.stderr.strip():
        parts.append("[stderr]\n" + proc.stderr.strip()[:MAX_OUTPUT_CHARS])
    if not parts:
        parts.append(f"（无输出，退出码 {proc.returncode}）")
    elif proc.returncode != 0:
        parts.append(f"[退出码] {proc.returncode}")
    return "\n".join(parts)
