# 在制品质量围堵图谱

本项目依据生产谱系识别质量事件影响范围。拆分、合并、装配、返工和发运均作为带数量的关系保存，任何计算都必须能回到来源事件。

`domain_contract.json` 给出谱系动作、围堵结论和对象位置。数量使用十进制定点字符串传输，禁止用二进制浮点数承担守恒判断。

## 核心不变式

- **数量守恒**：每条谱系事件 `sum(inputs) == sum(outputs)`，否则拒绝入图；消耗超过产出、重复创建等数据冲突被标记并进入关闭报告。
- **确定性**：图状态是「根批次登记 + 事件集合」的纯函数，按 `(occurred_at, event_id)` 规范序全量重建。乱序到达、重复扫描（同 `event_id` 幂等）不改变最终围堵边界。
- **围堵版本只增不减**：迟到事件只能扩展受影响集合与路径证据，已记录的影响范围永不消失。
- **发运单列**：位于 `transit` / `customer` 的受影响对象进入召回清单，绝不伪装成在库冻结。
- **数量级放行**：放行必须逐笔关联到检验合格量或偏差批准量；偏差批准人不得与调查员为同一人。

## 模块

| 模块 | 职责 |
| --- | --- |
| `app/quantity.py` | 十进制定点数量解析与格式化（拒绝 float） |
| `app/model.py` | 谱系事件模型；动作 / 处置 / 位置枚举以 `domain_contract.json` 为准 |
| `app/graph.py` | 谱系图：摄入去重、守恒校验、确定性重建、前向传播与路径枚举 |
| `app/containment.py` | 围堵事件：范围圈定、单调版本、检验 / 偏差 / 放行 / 报废台账 |
| `app/service.py` | 服务门面：领料判定、关闭报告、边界摘要；单锁保证并发一致 |

## 快速上手

```python
from app import QualityContainmentService

svc = QualityContainmentService()
svc.register_lot("L-RAW-1", "1000", "warehouse", material_batch="B-1")
svc.record_event({
    "event_id": "E1", "action": "split", "occurred_at": "2026-09-02T01:00:00Z",
    "inputs": [{"lot_id": "L-RAW-1", "qty": "1000"}],
    "outputs": [{"lot_id": "L-A", "qty": "600", "location": "line"},
                {"lot_id": "L-B", "qty": "400", "location": "warehouse"}],
})

# 调查员圈定范围：设备 / 物料批次 / 时间窗
svc.open_case("C-1", "investigator-wang",
              {"type": "material_batch", "material_batch": "B-1", "declared_qty": "1000"})

# 领料判定：allow / block / review_required，均带可解释原因
svc.request_material("REQ-1", "L-B", "10", "worker")     # block：处于围堵冻结中

# 检验与部分放行（数量关联到具体检验）
svc.record_inspection("C-1", "I-1", "L-B", "sampling", "100", "pass", "inspector-zhao")
svc.release("C-1", "R-1", "L-B", "100", "investigator-wang", inspection_ids=["I-1"])
svc.request_material("REQ-2", "L-B", "100", "worker")    # allow：放行依据 R-1

# 偏差批准（批准人 ≠ 调查员）
svc.approve_deviation("C-1", "D-1", "L-B", "50", "manager-li", "特采")

# 关闭前视图：未定位数量、每条传播路径、已释放依据、召回清单
report = svc.closure_report("C-1")
svc.close_case("C-1", "quality-manager", acknowledge_open_items=True)
```

## 领料结论语义

| 结论 | 触发条件 |
| --- | --- |
| `allow` | 未命中任何未结围堵且库存充足；或已放行额度足以覆盖申请数量 |
| `block` | 处于围堵冻结中且无可用放行额度；已发运且被召回；库存不足 |
| `review_required` | 谱系数据缺失 / 冲突；存在待结论（pending）检验；干净对象却已发运 |

相同 `request_id` 重试返回首次结论（幂等）；并发请求在同一临界区内判定与扣减，不会超发。

## 运行基础检查

```bash
python3 -m unittest discover -s tests -v
```
