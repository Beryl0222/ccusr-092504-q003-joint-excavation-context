# 联合考古语境与主张档案

面向中埃联合考古队的语境档案服务。南部圣湖等新发现不断改写对围墙、
奥西里斯小神殿与功能分区的解释，而双方的探方编号、测绘版本与文物
暂存编号并不一致——本服务让每类材料各自拥有来源与版本，用追加式
事件流保存全部历史，新发现只追加新状态，从不覆盖当时的记录。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记类型。
- `data/sample.json`：中文联调样例。
- `src/joint_excavation_context/`：契约校验、事件存储与各业务投影。
- `tests/`：契约边界检查与端到端机制测试。

## 核心对象（aggregate_type）

调查区 `survey_area`、地层单位 `stratigraphic_unit`、构筑物 `structure`、
探方 `excavation_unit`、出土对象 `archaeological_object`、样品 `sample`、
保管容器 `custody_container`、测绘版本 `survey_revision`、测点
`measurement_point`、研究主张 `research_claim`，以及系统流
`identifier_crosswalk` / `quarantine_marker` / `process_journal`。

## 事件类型（event_type）

| 事件 | 含义 |
| --- | --- |
| `CONTEXT_OPENED` | 开启调查区/地层单位/构筑物/探方/样品等语境实体 |
| `ALIAS_REGISTERED` | 登记某方本地编号与规范标识的对照 |
| `OBSERVATION_CAPTURED` | 在测点捕获观测（含 content_hash） |
| `RELATION_RECORDED` | 记录空间/地层关系，based_on 引用观测 |
| `SURVEY_REVISED` | 登记一版测绘成果，可 supersedes 旧版本 |
| `OBJECT_RECOVERED` | 对象出土，确立首位保管人（唯一） |
| `OBJECT_TRANSFERRED` | 保管交接：清理/送检/归还 |
| `SEAL_ANOMALY_REPORTED` | 封签异常；入库后系统立即冻结容器 |
| `CONTAINER_FROZEN` | 系统追加的容器冻结记录 |
| `QUARANTINE_MARKED` | 系统追加的测点隔离标记 |
| `CLAIM_PROPOSED` | 提出主张，citations 必须引用明确观测 |
| `CLAIM_REVIEWED` | 会审意见；可争论主题须双方授权会审人同意 |
| `CLAIM_SUPERSEDED` | 旧主张失效（invalidate）或缩限（narrow） |
| `RECORD_PUBLISHED` | 发布到公开目录；主张须先通过会审 |
| `PROCESS_STEP_RECORDED` | 批次/发布流程的步骤检查点 |

信封在必填字段之外约定：`source`（party: cn/eg/joint/system、system、
operator）、`batch_id`（离线批次）、`data`（业务负载）。入库时服务写入
`recorded_at`，与现场时间 `occurred_at` 共同支持双时间轴复原。

## 机制

- **编号对照**：`ALIAS_REGISTERED` 把双方探方号、测绘版本号、暂存号
  映射到规范标识；同一本地编号不得对照到两个规范标识。
- **离线冲突隔离**：event_id 相同按幂等去重；同测点同版本同哈希视为
  重复采集；同测点异内容时，只隔离该测点上的观测及其依赖成果
  （关系、测绘版本、出土记录、主张构成的传递闭包），其他区域继续入库。
- **保管链**：出土确立唯一保管人；交接交出方必须是当前保管人，阶段
  只许前进；封签异常立即冻结容器，冻结容器的一切交接被拒绝。
- **主张会审**：双圣湖布局、建筑年代、礼仪功能为可争论主题，须双方
  授权会审人各自同意；新主张可使旧主张失效或缩小适用范围，旧记录
  原文与会审过程保持原样。
- **断点恢复**：批量导入（校验→冲突检测→提交）与会审发布（核验证据
  →核验会审→发布→应用取代→刷新索引）都按步骤落检查点，中断重跑
  只补齐未完成步骤；提交按 event_id 幂等。
- **历史复原**：`spatial_view` / `evidence_chain` 可按 occurred 或
  recorded 时间轴复原任一时点的空间关系与证据链。
- **公开脱敏**：`public_spatial_view` / `public_evidence_chain` 只含已
  发布材料，隐藏精确坐标、未发表材料与内部保管位置。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
