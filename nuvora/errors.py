"""会话操作错误，交由 CLI 显示。"""


class ApplicationError(ValueError):
    """操作参数无效。"""


class NotFoundError(ApplicationError):
    """指定资源不存在。"""
