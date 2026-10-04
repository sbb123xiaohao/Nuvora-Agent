"""Python 工具：Linux/WSL 的 bubblewrap + seccomp 隔离；不可用时拒绝执行。"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from ._python_sandbox import MAX_OUTPUT_CHARS, SandboxUnavailable, run_process, sandbox_command, seccomp_policy

MAX_CODE_CHARS = 20000
MAX_TIMEOUT = 120


def run_python_code(code: str, timeout: int, cwd: Path) -> str:
    if not code.strip():
        return "（代码为空，未执行）"
    if len(code) > MAX_CODE_CHARS:
        return f"（代码过长：{len(code)} 字符，上限 {MAX_CODE_CHARS}，拒绝执行）"
    if sys.platform != "linux":
        return "Python 执行不可用：需要 Linux/WSL 的操作系统隔离；未执行代码。"
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        with seccomp_policy() as descriptor:
            command = sandbox_command(cwd, code, timeout, descriptor)
            returncode, stdout, stderr, timed_out = run_process(command, timeout, cwd, (descriptor,))
    except (SandboxUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        return f"Python 执行不可用：{e}；未以无隔离方式执行代码。"
    if timed_out:
        return f"执行超时（超过 {timeout} 秒），隔离进程及其后代已终止。"
    if returncode != 0 and stderr.startswith("bwrap:"):
        return f"Python 执行不可用：隔离环境启动失败；未执行用户代码。\n{stderr}"
    parts = []
    if stdout:
        parts.append("[stdout]\n" + stdout)
    if stderr:
        parts.append("[stderr]\n" + stderr)
    if not parts:
        parts.append(f"（无输出，退出码 {returncode}）")
    elif returncode != 0:
        parts.append(f"[退出码] {returncode}")
    return "\n".join(parts)


def sandbox_status(cwd: Path) -> tuple[bool, str]:
    result = run_python_code("print('NUVORA_SANDBOX_READY')", 5, cwd)
    ready = result == "[stdout]\nNUVORA_SANDBOX_READY"
    return ready, "bubblewrap + seccomp 隔离可用" if ready else result
