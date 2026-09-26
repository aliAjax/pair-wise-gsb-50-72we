# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查，以及分期缴纳申请、复核、实缴、催缴与结案证明规则。
- `src/repository.py`：SQLite建表、事务和查询（含分期计划、分期明细和实缴表）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/installments`：结案后申请分期，请求体为`{"reason":"...","items":[{"date":"YYYY-MM-DD","amount":1000.0}]}`。支持2到6期，首期不晚于申请日后15天，相邻间隔不超过30天，总额须等于未缴金额；有逾期未结计划的案件会被拒绝。
- `GET /api/records/{id}/installments`：查询案件的分期计划、每期状态、实缴记录和催缴信息。
- `POST /api/records/{id}/installments/{plan_id}/review`：复核分期计划，请求体为`{"outcome":"approved|returned","note":"..."}`，退回必须填写意见。
- `POST /api/records/{id}/installments/{plan_id}/payments`：批准后登记实缴，请求体为`{"seq":1,"amount":1000.0,"paid_on":"YYYY-MM-DD"}`，`paid_on`可缺省为当日；全部缴清后计划自动转为已缴清。
- `GET /api/dunning`：全部逾期未缴期次的催缴清单。
- `POST /api/records/{id}/certificate`：开具结案证明，请求体为`{"expected_version":1}`，未缴清前会被拒绝。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突，以及分期缴纳全流程、申请校验、逾期催缴、结案证明和重启后的持久化查询。
