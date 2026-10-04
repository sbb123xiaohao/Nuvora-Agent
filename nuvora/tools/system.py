"""基础系统工具：时间、环境信息。"""

from __future__ import annotations

import platform
import sys
from datetime import datetime
from pathlib import Path

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def current_time_text() -> str:
    now = datetime.now().astimezone()
    utc = datetime.utcnow()
    return (
        f"本地时间：{now.isoformat(timespec='seconds')}（{WEEKDAY_CN[now.weekday()]}）\n"
        f"UTC：{utc.isoformat(timespec='seconds') + 'Z'}"
    )


def system_info_text(project_root: Path) -> str:
    return (
        f"操作系统：{platform.system()} {platform.release()}（{platform.machine()}）\n"
        f"Python：{sys.version.split()[0]}\n"
        f"NUVORA 项目目录：{project_root}\n"
        f"当前工作目录：{Path.cwd()}"
    )
