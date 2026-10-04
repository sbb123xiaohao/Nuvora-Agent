"""Linux Python 执行隔离与有界输出；不提供无隔离的工具执行入口。"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
MAX_OUTPUT_BYTES = MAX_OUTPUT_CHARS * 4


class SandboxUnavailable(RuntimeError):
    pass


def clean_environment() -> dict[str, str]:
    """仅传递固定运行参数，不继承 API Key、代理或宿主配置。"""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }


@contextmanager
def seccomp_policy():
    """导出不可撤销的系统调用过滤器，作为命名空间隔离的附加限制。"""
    name = ctypes.util.find_library("seccomp")
    if not name:
        raise SandboxUnavailable("缺少 libseccomp，请安装系统的 libseccomp 运行库")
    library = ctypes.CDLL(name, use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.seccomp_export_bpf.restype = ctypes.c_int
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxUnavailable("无法创建 seccomp 策略")
    descriptor = None
    try:
        for call in (
            "socket", "socketcall", "io_uring_setup", "io_uring_enter", "io_uring_register",
            "mount", "umount2", "pivot_root", "setns", "unshare", "ptrace",
            "open_by_handle_at", "bpf", "perf_event_open", "keyctl", "add_key", "request_key",
        ):
            number = library.seccomp_syscall_resolve_name(call.encode("ascii"))
            if number < 0:
                if call == "socket":
                    raise SandboxUnavailable("当前体系结构不支持网络系统调用过滤")
                continue
            if library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) < 0:
                raise SandboxUnavailable("无法加载 seccomp 系统调用限制")
        descriptor = os.memfd_create("nuvora-seccomp", os.MFD_CLOEXEC)
        if library.seccomp_export_bpf(context, descriptor) < 0:
            raise SandboxUnavailable("无法导出 seccomp 策略")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        library.seccomp_release(context)


def sandbox_command(cwd: Path, code: str, timeout: int, policy_fd: int) -> list[str]:
    if sys.platform != "linux":
        raise SandboxUnavailable("Python 执行需要 Linux/WSL 的隔离环境")
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
    if not bwrap:
        raise SandboxUnavailable("缺少 bubblewrap，请安装 bubblewrap 后运行 doctor")
    workspace = Path(cwd).resolve(strict=True)
    if not workspace.is_dir():
        raise SandboxUnavailable("工作区必须是一个目录")
    from ..config import PROJECT_ROOT

    mounts = set()
    for source in ("/usr/lib", "/usr/lib64", "/lib", "/lib64", sys.base_prefix, sys.prefix):
        path = Path(source)
        if path.exists():
            resolved = path.resolve()
            if resolved == Path("/") or workspace.is_relative_to(resolved) or PROJECT_ROOT.is_relative_to(resolved):
                raise SandboxUnavailable("Python 运行时目录与私有项目目录重叠，拒绝扩大文件权限")
            mounts.add(str(path))
    command = [
        bwrap, "--unshare-user", "--unshare-all", "--disable-userns",
        "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
    ]
    for key, value in clean_environment().items():
        command.extend(["--setenv", key, value])
    for path in sorted(mounts, key=lambda value: (len(Path(value).parts), value)):
        command.extend(["--ro-bind", path, path])
    bootstrap = (
        "import resource, sys\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({timeout}, {timeout + 1}))\n"
        "resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (33554432, 33554432))\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"
        "resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "exec(compile(sys.argv[1], '<nuvora-python>', 'exec'), {'__name__': '__main__'})\n"
    )
    command.extend([
        "--proc", "/proc", "--dev", "/dev", "--size", "67108864", "--tmpfs", "/tmp",
        "--bind", str(workspace), "/workspace", "--chdir", "/workspace",
        "--seccomp", str(policy_fd), "--", sys.executable, "-I", "-B", "-c", bootstrap, code,
    ])
    return command


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(command: list[str], timeout: int, cwd: Path, pass_fds: tuple[int, ...] = ()) -> tuple[int, str, str, bool]:
    """私有执行辅助函数：并行排空管道，但每个流只保留有限字节。"""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        cwd=cwd, env=clean_environment(), start_new_session=True, close_fds=True,
        pass_fds=pass_fds, bufsize=0,
    )
    streams = [(process.stdout, bytearray(), [False]), (process.stderr, bytearray(), [False])]

    def drain(pipe, buffer, truncated):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                remaining = MAX_OUTPUT_BYTES - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
        except (OSError, ValueError):
            pass

    workers = [threading.Thread(target=drain, args=stream, daemon=True) for stream in streams]
    started = []
    timed_out = False
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        # PID 命名空间的 init 随 bubblewrap 退出，所有后代随之结束。
        # 同时清理外层进程组，涵盖启动失败和等待超时。
        _kill_process_group(process)
        process.wait(timeout=5)
        for worker in started:
            worker.join(timeout=2)
        for pipe, _, _ in streams:
            pipe.close()
    outputs = []
    for _, buffer, truncated in streams:
        value = buffer.decode("utf-8", errors="replace")
        if len(value) > MAX_OUTPUT_CHARS or truncated[0]:
            value = value[:MAX_OUTPUT_CHARS] + "\n…（输出已截断）"
        outputs.append(value.strip())
    return process.returncode, outputs[0], outputs[1], timed_out
