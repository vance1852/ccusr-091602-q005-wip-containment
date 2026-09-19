# 在制品质量围堵图谱

本项目依据生产谱系识别质量事件影响范围。拆分、合并、装配、返工和发运均作为带数量的关系保存，任何计算都必须能回到来源事件。

`domain_contract.json` 给出谱系动作、围堵结论和对象位置。数量使用十进制定点字符串传输，禁止用二进制浮点数承担守恒判断。

## 架构

```
app/
  quantity.py   十进制定点数量: float 一律拒绝, 守恒判断不受舍入污染
  model.py      谱系事件(不可变值对象)与对象台账; 构造时强制拆合守恒
  graph.py      谱系图: 由(台账, 事件集合)纯函数推导在库量/位置/下游传播
  incident.py   围堵聚合: 圈定条件、版本化影响范围、检验/偏差/放行记录
  service.py    ContainmentService 门面: 全部公开方法线程安全
```

## 核心语义

- **谱系事件**：`split`/`merge`/`assemble`/`rework`/`ship`，每个事件输入合计必须等于输出合计（入口强制）。事件排序键为 `(occurred_at, event_id)`，与到达顺序无关；同标识同内容的重复扫描幂等去重，同标识不同内容立即拒绝。
- **围堵圈定**：调查员按设备、物料批次或时间窗圈定（可组合）。命中事件涉及的对象成为种子，沿事件输出方向传播到不动点，每个受影响对象记录首次引入它的事件与上游对象作为路径证据。
- **版本单调**：每次新事件到达自动重算所有未关闭围堵；影响对象集合只增不减（迟到事件只能扩展），`holds`/`recalls` 分类按当前图谱位置推导——已发运对象单列召回，不伪装成在库冻结。
- **领料判定**：`decide_issue` 纯查询、`commit_issue` 原子判定并扣减（同 `request_id` 幂等）。结论为三态：`allow` / `block`（附传播路径证据）/ `review_required`（部分放行超额或偏差待批）。已发运对象一律阻止并指向召回流程。
- **检验与放行**：检验记录取样数量与结论覆盖数量，复验指向原检验；放行数量必须落在合格检验覆盖量或已批准偏差量的剩余额度内，且不超过冻结在库量。调查员与批准人（放行/偏差/报废）不得为同一人，服务层强制。
- **关闭**：`pre_close_report` 给出未定位数量（通报量 − 谱系定位量）、每条传播路径、已释放依据、召回清单与全部关闭阻碍；`close_incident` 要求无未定位、无待批偏差、召回全部确认、边界内在库量全部已放行或报废。

## 确定性

图谱与围堵边界是 `(台账, 事件集合)` 的纯函数：乱序谱系、重复扫描、部分放行与并发领料（内部锁下原子判定，不会超发）都得到相同的围堵边界。`snapshot()` 可导出全量状态做等价对比。

## 运行基础检查

```bash
python3 -m unittest discover -s tests -v
```

## 最小示例

```python
from app import ContainmentService, GenealogyEvent

svc = ContainmentService()
svc.register_object("LOT-A", "1000", material_lot="M1")
svc.ingest_event(GenealogyEvent(
    "EV-1", "split", {"LOT-A": "600"}, {"WIP-B": "600"},
    "2026-09-01T08:00:00", "line", equipment="EQ-7"))

# AOI 漏检通报: 圈定设备 EQ-7
svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7",
                  reported_qty={"M1": "1000"})
svc.containment_boundary("INC-1")        # 冻结清单 / 召回清单 / 传播路径

svc.decide_issue("WIP-B", "100")         # -> block, 附路径证据
svc.record_inspection("INC-1", "INSP-1", "WIP-B", sampled_qty="60",
                      covered_qty="600", result="pass", inspector="QA-1")
svc.release("INC-1", "REL-1", "WIP-B", qty="600",
            basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")
svc.commit_issue("REQ-1", "WIP-B", "100", requester="LINE-3")  # -> allow
```
