"""领域异常类型。

事件摄入（record_event / register_lot）属于流式入口，数据问题以状态字典返回；
围堵、检验、放行、领料等事务性命令通过抛出下列异常拒绝非法操作。
"""


class DomainError(Exception):
    """领域错误基类。"""


class ValidationError(DomainError):
    """输入参数或结构不合法。"""


class ConservationError(DomainError):
    """拆合过程数量不守恒。"""


class UnknownCaseError(DomainError):
    """围堵事件（case）不存在。"""


class UnknownRecordError(DomainError):
    """检验 / 偏差 / 放行等记录不存在。"""


class CaseClosedError(DomainError):
    """事件已关闭，禁止继续写入。"""


class SeparationOfDutiesError(DomainError):
    """职责分离冲突：调查员与批准人不可为同一人。"""


class InsufficientBasisError(DomainError):
    """放行依据（检验合格量 + 偏差批准量）不足以覆盖放行数量。"""


class OpenItemsError(DomainError):
    """关闭事件时仍存在未结事项（在冻数量 / 未定位数量 / 未决召回 / 待结论检验）。"""

    def __init__(self, message, open_items):
        super().__init__(message)
        self.open_items = open_items
