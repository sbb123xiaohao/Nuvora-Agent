"""应用错误；由入口层映射为 HTTP 状态或终端提示。"""


class ApplicationError(ValueError):
    """操作参数无效。"""


class BusyError(ApplicationError):
    """当前回合占用了可写资源。"""


class ClosedError(ApplicationError):
    """应用正在关闭或已关闭。"""


class NotFoundError(ApplicationError):
    """指定资源不存在。"""
