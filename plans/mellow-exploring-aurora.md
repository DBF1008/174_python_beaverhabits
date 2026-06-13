# 用户偏好设置结构化读写 API

## 背景

当前很多用户配置（时区、暗色模式、完成状态映射、自定义 CSS、每周起始日）仅存在于 NiceGUI 的会话存储（`app.storage.user`），外部 REST 客户端不可见，刷新或跨端后丢失。现有 `UserConfigsModel`（JSON blob）只存了 CSS 和 chips，且无 REST API 暴露。

## 目标

1. 新增 `GET/PATCH /api/v1/preferences` REST 端点，支持结构化读写用户偏好
2. 新增 Pydantic 模型做输入验证（时区、CSS 消毒、first_day_of_week 范围）
3. 让时区从浏览器检测后持久化到 DB
4. GUI 页面行为不变，API 与 GUI 共享同一 DB JSON blob
5. 新增回归测试覆盖：默认值、持久化更新、跨会话恢复、非法输入

## 实现步骤

### Step 1: 在 `beaverhabits/app/schemas.py` 中新增 Pydantic 模型

在现有 `app/schemas.py` 末尾添加两个模型：

**`UserPreferencesResponse`** — GET 响应，返回完整偏好（用户设置值或全局默认值回退）：
- `timezone: str`（默认 `settings.TIME_ZONE or "UTC"`）
- `dark_mode: bool | None`（默认 `settings.DARK_MODE`）
- `first_day_of_week: int`（0-6，默认 `settings.FIRST_DAY_OF_WEEK`）
- `default_chips: list[str]`（默认 `settings.DEFAULT_COMPLETION_STATUS_LIST` 的拷贝）
- `default_chips_mapping: dict[str, str]`（默认 `{}`）
- `custom_css: str`（默认 `""`）

**`UserPreferencesUpdate`** — PATCH 请求体，所有字段可选：
- 同上场，类型均 `| None`
- `@field_validator("timezone")` — 用 `pytz.all_timezones_set` 校验
- `@field_validator("custom_css")` — 调用 `views.sanitize_css()` 消毒
- `@field_validator("first_day_of_week")` — 校验 0-6 范围

### Step 2: 新增偏好设置服务层 `beaverhabits/core/preferences.py`

薄服务层，负责 API 字段名 ↔ DB JSON key 的映射和默认值填充：

| API 字段名 | DB JSON key |
|---|---|
| `custom_css` | `css` |
| `default_chips` | `default_chips` |
| `default_chips_mapping` | `default_chips_mapping` |
| `timezone` | `timezone` |
| `dark_mode` | `dark_mode` |
| `first_day_of_week` | `first_day_of_week` |

**`get_preferences(user) -> UserPreferencesResponse`**:
1. 调用 `crud.get_user_configs(user)` 获取原始 JSON
2. 对每个字段：有则用用户值，无则用全局 `settings.*` 回退
3. 返回 `UserPreferencesResponse`

**`update_preferences(user, patch) -> UserPreferencesResponse`**:
1. 将 `patch.model_dump(exclude_none=True)` 转为 DB key 字典
2. 调用 `crud.update_user_configs(user, db_update)` 做 merge 更新
3. 调用 `get_preferences(user)` 返回完整状态

### Step 3: 在 `beaverhabits/routes/api.py` 添加 REST 端点

```
GET  /api/v1/preferences   →  read_preferences(user=Depends(current_active_user))
PATCH /api/v1/preferences   →  patch_preferences(patch, user=Depends(current_active_user))
```

- GET 返回完整偏好对象
- PATCH 接收部分更新，返回更新后的完整偏好对象
- 都复用 `current_active_user` 依赖（支持 JWT / API token / trusted header）

### Step 4: 在 `beaverhabits/views.py` 中扩展 UserConfigs 和缓存

1. 扩展 `UserConfigs` dataclass，新增 `timezone`、`dark_mode`、`first_day_of_week` 字段
2. 更新 `get_user_configs()` 解析新字段
3. 更新 `cache_user_configs()` 将新字段同步到 `app.storage.user`
4. 更新 `update_custom_css` / `update_default_chips` 保持不变（已写同一 DB blob）

### Step 5: 在 `beaverhabits/utils.py` 中持久化浏览器检测的时区

修改 `fetch_user_timezone()`：检测到时区后，若与 DB 中已有值不同，调用 `crud.update_user_configs` 持久化。

需守卫条件：
- 用户已认证
- 新时区与已存储的不同（避免每次页面加载都写 DB）

### Step 6: 新增回归测试 `tests/test_preferences_api.py`

遵循现有 `test_apis.py` 模式（pytest-asyncio + TestClient + Bearer auth）：

**默认值测试：**
- 新用户 GET 返回全局默认值（timezone="UTC", first_day_of_week=0, default_chips=["yes","no"], custom_css=""）
- 未认证请求返回 401

**持久化更新测试：**
- PATCH timezone + first_day_of_week → GET 读回一致
- 部分 PATCH 不影响其他字段（先 PATCH timezone，再 PATCH dark_mode，GET 两者都在）
- 更新 CSS 和 chips 与现有 GUI 路径一致

**跨会话恢复测试：**
- Session 1 登录 → PATCH timezone="Europe/Berlin"
- Session 2 重新登录 → GET 仍返回 "Europe/Berlin"

**非法输入测试：**
- 无效时区 `"Fake/Zone"` → 422
- first_day_of_week=9 → 422
- CSS 包含 `<script>` 标签 → 被消毒（`<script>` 被移除）
- 空 PATCH `{}` → 200，无变化

## 文件变更清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `beaverhabits/app/schemas.py` | 修改 | 添加 `UserPreferencesResponse` + `UserPreferencesUpdate` |
| `beaverhabits/core/preferences.py` | 新建 | 偏好设置服务层（DB↔schema 映射 + 默认值） |
| `beaverhabits/routes/api.py` | 修改 | 添加 GET/PATCH `/preferences` 端点 |
| `beaverhabits/views.py` | 修改 | 扩展 `UserConfigs` dataclass + `cache_user_configs` |
| `beaverhabits/utils.py` | 修改 | `fetch_user_timezone` 持久化时区到 DB |
| `tests/test_preferences_api.py` | 新建 | 回归测试（4 类场景约 12 个用例） |

## 验证方式

```bash
cd /Users/dongbufan/work/ai_color/one_claude_project/success/174_python_beaverhabits/repo
uv run pytest tests/test_preferences_api.py -v
```
