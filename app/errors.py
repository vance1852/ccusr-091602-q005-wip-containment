"""领域异常体系。"""


class DomainError(Exception):
    """领域规则违规的基类。"""


class QuantityError(DomainError, ValueError):
    """数量无法解析或违反数量规则(负值、浮点、非有限值)。"""


class ConservationError(QuantityError):
    """拆分/合并/装配/返工/发运前后数量不守恒。"""


class EventShapeError(DomainError, ValueError):
    """谱系事件形状不合法(动作、位置、输入输出数目)。"""


class EventConflictError(DomainError):
    """相同事件标识携带不同内容; 重复扫描必须内容一致。"""


class UnknownObjectError(DomainError):
    """引用了未登记且未出现在任何事件中的对象。"""


class UnknownIncidentError(DomainError):
    """围堵事件标识不存在。"""


class IncidentStateError(DomainError):
    """围堵事件当前状态不允许该操作(如已关闭)。"""


class SeparationOfDutiesError(DomainError, PermissionError):
    """调查员与批准人不得为同一人。"""


class ReleaseError(DomainError):
    """放行依据不足或放行数量越界。"""


class CloseBlockedError(DomainError):
    """关闭条件未满足; 消息中列出全部阻碍。"""


class RequestConflictError(DomainError):
    """相同领料请求标识携带不同内容。"""
