"""工作区文件工具：路径校验、受限读取、原子写入和可恢复的错误结果。"""

from __future__ import annotations

import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

MAX_WRITE_CHARS = 1_000_000
MAX_DIR_ENTRIES = 1000


class SandboxViolation(ValueError):
    """路径试图逃逸出工作区。"""


def resolve_in_sandbox(rel_path: str, sandbox_root: Path) -> Path:
    raw = str(rel_path).replace("\\", "/").strip()
    if not raw or "\0" in raw:
        raise SandboxViolation("路径为空或含有非法字符")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise SandboxViolation("仅支持 workspace/ 内的相对路径")
    parts = Path(raw).parts
    if ".." in parts:
        raise SandboxViolation("路径不允许包含 '..'")
    root = Path(sandbox_root).resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise SandboxViolation("路径逃逸出工作区")
    return target


def _file_result(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, ValueError, RuntimeError) as e:
            return f"文件操作失败：{type(e).__name__}: {e}"
    return wrapped


@contextmanager
def _parent_handle(root: Path, target: Path, create: bool = False):
    """POSIX 下逐层打开目录且不跟随链接，避免校验后发生链接替换。"""
    if os.name != "posix":
        if create:
            target.parent.mkdir(parents=True, exist_ok=True)
        yield None, target.name
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root, flags)
    try:
        for part in target.relative_to(root).parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, target.name
    finally:
        os.close(descriptor)


@_file_result
def sandbox_list_dir(sandbox_root: Path, rel_path: str = ".") -> str:
    root = Path(sandbox_root).resolve()
    target = resolve_in_sandbox(rel_path, root)
    if not target.exists():
        return f"（路径不存在：{rel_path}）"
    if not target.is_dir():
        return f"（这是一个文件，不是目录：{rel_path}）"
    descriptor = None
    try:
        if os.name == "posix":
            if target == root:
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            else:
                with _parent_handle(root, target) as (parent, name):
                    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        rows = []
        truncated = False
        with os.scandir(descriptor if descriptor is not None else target) as entries:
            for entry in entries:
                if len(rows) == MAX_DIR_ENTRIES:
                    truncated = True
                    break
                rel_name = (target.relative_to(root) / entry.name).as_posix()
                if entry.is_symlink():
                    rows.append((1, entry.name.lower(), f"[链接] {rel_name}"))
                elif entry.is_dir(follow_symlinks=False):
                    rows.append((0, entry.name.lower(), f"[目录] {rel_name}/"))
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    rows.append((1, entry.name.lower(), f"[文件] {rel_name}  ({size} B)"))
        lines = [row[2] for row in sorted(rows)]
        if truncated:
            lines.append(f"…（最多显示 {MAX_DIR_ENTRIES} 项）")
        return "\n".join(lines) or f"（目录为空：{rel_path}）"
    finally:
        if descriptor is not None:
            os.close(descriptor)


@_file_result
def sandbox_read_file(sandbox_root: Path, rel_path: str, max_chars: int = 20000) -> str:
    root = Path(sandbox_root).resolve()
    target = resolve_in_sandbox(rel_path, root)
    if not target.exists():
        return f"（文件不存在：{rel_path}）"
    if target.is_dir():
        return f"（这是一个目录，请用 list_dir 查看：{rel_path}）"
    max_chars = max(1, min(int(max_chars), 20000))
    with _parent_handle(root, target) as (parent, name):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name if parent is not None else target, flags, dir_fd=parent)
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SandboxViolation("只允许读取普通文本文件")
            text = stream.read(max_chars + 1)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n…（超过 {max_chars} 字符，已截断）"
    return text


@_file_result
def sandbox_write_file(sandbox_root: Path, rel_path: str, content: str) -> str:
    if len(content) > MAX_WRITE_CHARS:
        return f"文件操作失败：内容超过 {MAX_WRITE_CHARS} 字符上限"
    root = Path(sandbox_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = resolve_in_sandbox(rel_path, root)
    if target == root:
        raise SandboxViolation("文件路径不能是工作区根目录")
    existed = target.exists()
    with _parent_handle(root, target, create=True) as (parent, name):
        if parent is None:
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
                    temp_path = stream.name
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, target)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
        else:
            temp_name = f".nuvora-write-{uuid.uuid4().hex}"
            descriptor = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temp_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
    action = "覆盖写入" if existed else "新建"
    return f"已{action}文件 workspace/{target.relative_to(root).as_posix()}（{len(content)} 字符）"
