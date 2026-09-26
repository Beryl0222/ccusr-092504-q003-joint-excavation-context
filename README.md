# 联合考古语境与主张档案

本项目提供联合考古语境与主张档案所需的领域事件交换约定、基础校验库与档案服务。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实，避免跨系统交换时丢失来源顺序。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/joint_excavation_context/contracts.py`：不依赖第三方包的基础校验器。
- `src/joint_excavation_context/store.py`：只追加事件存储，幂等去重，支持历史时点回放。
- `src/joint_excavation_context/registry.py`：双方探方号、测绘版本、文物暂存编号的别名登记。
- `src/joint_excavation_context/ingest.py`：现场数据接收，含测点冲突隔离。
- `src/joint_excavation_context/claims.py`：研究主张生命周期与双方会审。
- `src/joint_excavation_context/custody.py`：保管链与封签冻结。
- `src/joint_excavation_context/jobs.py`：可恢复作业（批量导入、会审发布）。
- `src/joint_excavation_context/views.py`：历史时点复原与公开查询视图。
- `tests/`：契约边界与各服务行为检查。

## 聚合与事件

核心聚合包括 survey_area、excavation_unit、stratigraphic_unit、structure、archaeological_object、survey_revision、sample、custody_transfer、research_claim，各自携带来源与版本。事件类型覆盖语境开立、观测采集、保管交接、主张提交与会审、失效与范围收缩、发表、别名登记、封签异常、容器冻结与解除、冲突隔离。

## 服务行为

- **编号映射**：双方沿用各自探方编号、测绘版本与暂存编号，经 `AliasRegistry` 解析到统一聚合；同一外部编号映射到不同聚合即报冲突。
- **现场接收**：离线采集的事件按 event_id 幂等去重，晚到事件正常受理；同一测点出现异内容观测时，只隔离依赖该测点的成果，其他区域继续入库，人工核对后解除。
- **主张会审**：双圣湖布局、建筑年代、礼仪功能等可争论解释必须引用已入库的明确观测，并经双方各自有权限的会审人批准后方可发布；新发现使旧主张失效或缩小适用范围时只设有效截止，不覆盖当时的记录。
- **保管链**：出土、清理、送检、归还各阶段维持唯一保管责任方，交接必须衔接当前责任方并顺序推进；封签异常立即冻结对应容器，解冻后方可继续交接。
- **断点续跑**：批量导入与会审发布按步骤记录检查点，中断后重跑只补齐未完成步骤。
- **时点复原与公开查询**：可按任一历史时点复原空间关系与证据链；公开查询仅含已发表材料，并隐藏精确坐标、未发表材料与内部保管位置。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
