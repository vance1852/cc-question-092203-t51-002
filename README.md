# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池）
- 仪表盘统计（`/api/dashboard/stats`）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 站点与车辆的退役（可审计的软删除）

为满足合规审计要求，站点/车辆的删除遵循“存在历史不得物理删除”的原则：

- **退役**：`POST /api/stations/{id}/retire`、`POST /api/vehicles/{id}/retire`
  将实体标记为退役（`is_retired`、`retired_at`）。退役操作幂等，重复退役不改变
  退役时间与名称历史；已退役的站点/车辆不能再登记换电。
- **默认列表隐藏**：`GET /api/stations`、`GET /api/vehicles` 默认只返回在役实体；
  退役实体需通过明确筛选查回：`?retired=true`（只看退役）或
  `?include_retired=true`（全部）。按 id 的详情接口始终可查，且返回 `name_history`。
- **历史名称可审计**：改名（站点 `name` / 车辆 `plate`）会把旧名称及变更时间
  追加到 `name_history`，只增不改，退役后仍可查回曾用名。
- **物理删除**：`DELETE` 仅允许删除**未被任何换电记录引用**的误建数据；
  存在换电历史时返回稳定的 `409 冲突`（提示改为退役），实体与历史记录均保留。
- **事务一致性**：数据库已开启外键约束（SQLite `PRAGMA foreign_keys=ON`）；
  任何约束异常都会整体回滚并转换为一致的 `409` 响应，不会留下半次更新，
  回滚后会话恢复干净，下一次正常请求仍可完成。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
