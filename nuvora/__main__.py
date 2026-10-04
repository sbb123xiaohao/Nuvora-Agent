"""python -m nuvora 入口。"""

import sys

if sys.version_info < (3, 11):
    print("NUVORA 需要 Python 3.11 或更新版本。", file=sys.stderr)
    raise SystemExit(1)

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
