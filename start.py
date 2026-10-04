"""首次启动准备虚拟环境，随后进入 NUVORA CLI。"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 11):
        print("NUVORA 需要 Python 3.11 或更新版本，请安装后重新启动。")
        return 1
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = environment / "nuvora-dependencies.sha256"
    fingerprint = hashlib.sha256((root / "requirements.txt").read_bytes() + (root / "requirements.lock.txt").read_bytes()).hexdigest()
    if not python.exists():
        print("首次启动：正在准备本地 Python 环境…")
        result = subprocess.call([sys.executable, "-m", "venv", str(environment)], cwd=root)
        if result:
            print("虚拟环境创建失败，请确认 Python 包含 venv 和 pip。")
            return result
    if not marker.exists() or marker.read_text().strip() != fingerprint:
        print("正在安装已锁定的依赖，首次启动可能需要几分钟…")
        result = subprocess.call([str(python), "-m", "pip", "install", "-r", "requirements.txt"], cwd=root)
        if result:
            print("依赖安装失败，请检查网络后重新启动。")
            return result
        marker.write_text(fingerprint + "\n")
    return subprocess.call([str(python), "-m", "nuvora", *args], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
