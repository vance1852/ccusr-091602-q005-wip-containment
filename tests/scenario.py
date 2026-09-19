"""共享测试场景: AOI 漏检通报前后的生产谱系。

谱系(数量单位: 件):
  LOT-A 1000 (仓库, 批次 M1)
    --EQ-7 split--> WIP-B 600 (线边)            [LOT-A 余 400]
  WIP-B 600 + LOT-C 300 (批次 M2, 干净批次)
    --EQ-9 merge--> WIP-D 900
    --EQ-9 assemble--> FG-E 900 (成品仓)
    --EQ-9 split--> FG-E1 500 + FG-E2 400
  FG-E1 500 --ship--> SHP-F 500 (客户)
  LOT-X 200 (批次 M9): 无关产线, 不应被卷入

圈定设备 EQ-7 后, 影响范围应为:
  LOT-A, WIP-B, WIP-D, FG-E, FG-E1, FG-E2, SHP-F
其中在库冻结: LOT-A(400), FG-E2(400); 召回: SHP-F(500);
WIP-B/WIP-D/FG-E/FG-E1 已转化为下游, 物理量由下游对象承接。
"""

from app.model import GenealogyEvent
from app.service import ContainmentService

T = "2026-09-01T"


def base_events():
    return [
        GenealogyEvent("EV-1", "split", {"LOT-A": "600"}, {"WIP-B": "600"},
                       T + "08:00:00", "line", equipment="EQ-7", material_lot="M1"),
        GenealogyEvent("EV-2", "merge", {"WIP-B": "600", "LOT-C": "300"}, {"WIP-D": "900"},
                       T + "08:30:00", "line", equipment="EQ-9"),
        GenealogyEvent("EV-3", "assemble", {"WIP-D": "900"}, {"FG-E": "900"},
                       T + "09:00:00", "warehouse", equipment="EQ-9"),
        GenealogyEvent("EV-4", "split", {"FG-E": "500"}, {"FG-E1": "500"},
                       T + "09:30:00", "warehouse", equipment="EQ-9"),
        GenealogyEvent("EV-5", "split", {"FG-E": "400"}, {"FG-E2": "400"},
                       T + "09:31:00", "warehouse", equipment="EQ-9"),
        GenealogyEvent("EV-6", "ship", {"FG-E1": "500"}, {"SHP-F": "500"},
                       T + "10:00:00", "customer"),
    ]


def register_objects(svc):
    svc.register_object("LOT-A", "1000", location="warehouse", material_lot="M1")
    svc.register_object("LOT-C", "300", location="warehouse", material_lot="M2")
    svc.register_object("LOT-X", "200", location="warehouse", material_lot="M9")
    return svc


def build_service():
    svc = ContainmentService()
    register_objects(svc)
    svc.ingest_events(base_events())
    return svc
