"""文件工具：所有路径都限制在 workspace/ 沙箱内。"""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(Exception):
    """路径试图逃逸出沙箱时抛出。"""


def resolve_in_sandbox(rel_path: str, sandbox_root: Path) -> Path:
    """把给定的相对路径安全地解析到沙箱内。

    - 拒绝绝对路径（`/...`、`C:/...`）与包含 `..` 的路径
    - 统一斜杠后按相对路径解析
    - 解析符号链接后再校验仍在沙箱内
    """
    raw = str(rel_path).replace("\\", "/").strip()
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise SandboxViolation(
            f"仅支持 workspace/ 内的相对路径，不接受绝对路径：{rel_path}"
        )
    parts = [seg for seg in Path(raw).parts if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise SandboxViolation(f"路径不允许包含 '..'：{rel_path}")
    if not parts:
        raise SandboxViolation(f"路径为空：{rel_path}")
    target = sandbox_root.joinpath(*parts)
    target = target.resolve()
    root = sandbox_root.resolve()
    if not (target == root or root in target.parents):
        raise SandboxViolation(f"路径逃逸出沙箱：{rel_path}")
    return target


def sandbox_list_dir(sandbox_root: Path, rel_path: str = ".") -> str:
    root = resolve_in_sandbox(rel_path, sandbox_root)
    if not root.exists():
        return f"（路径不存在：{rel_path}）"
    if root.is_file():
        return f"（这是一个文件，不是目录：{rel_path}）"
    entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    if not entries:
        return f"（目录为空：{rel_path}）"
    lines = []
    for entry in entries:
        rel_name = entry.relative_to(sandbox_root).as_posix()
        if entry.is_dir():
            lines.append(f"[目录] {rel_name}/")
        else:
            size = entry.stat().st_size
            lines.append(f"[文件] {rel_name}  ({size} B)")
    return "\n".join(lines)


def sandbox_read_file(sandbox_root: Path, rel_path: str, max_chars: int = 20000) -> str:
    target = resolve_in_sandbox(rel_path, sandbox_root)
    if not target.exists():
        return f"（文件不存在：{rel_path}）"
    if target.is_dir():
        return f"（这是一个目录，请用 list_dir 查看：{rel_path}）"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n…（已截断，全文共 {len(text)} 字符）"
    return text


def sandbox_write_file(sandbox_root: Path, rel_path: str, content: str) -> str:
    target = resolve_in_sandbox(rel_path, sandbox_root)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    action = "覆盖写入" if existed else "新建"
    return f"已{action}文件 workspace/{target.relative_to(sandbox_root).as_posix()}（{len(content)} 字符）"
