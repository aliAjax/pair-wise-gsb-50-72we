# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查。
- `src/installments.py`：结案后分期缴纳规则（2到6期、首期15天内、间隔30天内、总额等于未缴金额、催缴计算）。
- `src/repository.py`：SQLite建表、事务和查询（含分期计划与实缴表）。
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
- `POST /api/records/{id}/installments`：结案后申请分期缴纳（纳税人代表），请求体为`{"installments":[{"due_date":"YYYY-MM-DD","amount":...},...]}`。
- `POST /api/records/{id}/installments/{plan}/review`：复核分期申请（复核人员），请求体为`{"decision":"approve|return","note":"..."}`。
- `POST /api/records/{id}/installments/{plan}/payments`：登记某期实缴（复核人员），请求体为`{"seq":1,"amount":...,"paid_date":"YYYY-MM-DD"}`。
- `GET /api/records/{id}/installments`：分期计划总览，含每期状态、实缴明细和逾期催缴。
- `GET /api/records/{id}/closure-certificate`：开具结案证明，缴清前返回409。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
