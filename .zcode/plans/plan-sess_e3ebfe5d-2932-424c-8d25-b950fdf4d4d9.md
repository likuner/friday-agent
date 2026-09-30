# 第一批安全修复实施计划

## 修复项与改动

### 1. JWT_SECRET fail-fast（严重漏洞：当前用默认值 "change-me" 签发全部 token）
- `backend/app/config.py`：
  - `env_file` 从相对 CWD 改为绝对路径锚定 `backend/.env`（`Path(__file__).resolve().parent.parent / ".env"`），消除"从仓库根启动时 .env 静默不加载"的坑，使校验判定确定
  - 增加 `model_validator(mode="after")`：`jwt_secret` 为弱值（`""` / `"change-me"` / `"replace-with-a-long-random-secret"`）时抛 ValueError，启动即失败并提示生成方式
- `backend/.env`：追加 `JWT_SECRET=<openssl rand -hex 32>`（先 grep 确认不存在再追加，避免重复）

### 2. /files 静态图片服务鉴权（当前匿名可读任意用户上传图）
- `backend/app/files.py`：新增 `GET /api/files/{name}`（router 已挂 `/files` prefix）：`Depends(current_user)` 登录校验 + 复用 `resolve_stored_image`（SAFE_NAME 白名单防穿越）+ `FileResponse(path, media_type=...)`；上传响应的 `url` 字段同步改为 `/api/files/{name}`
- `backend/app/main.py`：删除 `/files` 的 StaticFiles 挂载（保留 `FILES_DIR.mkdir`）
- `frontend/src/lib/api.ts`：新增 `authImageUrl(token, name)`——带 Bearer fetch → blob → objectURL（模块级小缓存）
- `frontend/src/components/ChatWorkspace.tsx`：`MessageImage` 改用该 helper（useEffect 加载、卸载 revokeObjectURL、失败仍降级占位）；待发送附件预览（原 `:586` 直接 `fileUrl`）同样改走鉴权加载

### 3. /workspaces 工具产出文件鉴权（当前匿名可读 Bash/Write 产出）
- `backend/app/workspace_picker.py`（router 已挂 `/workspaces` prefix）：新增 `GET /api/workspaces/{conversation_id}/{file_path:path}`：
  - `Depends(current_user)` + 会话归属校验（`Conversation.user_id == user.id`，404 掩护）
  - 路径 containment：`(WORKSPACES_DIR / cid / file_path).resolve()` 必须位于该会话目录 resolve 结果之下，防 `..`/嵌套逃逸
  - 返回 `FileResponse`（带 `filename=` 触发下载）
- `backend/app/agent.py:224`：`public_prefix` 从 `/workspaces/{cid}` 改为 `/api/workspaces/{cid}`（模型告知用户的下载路径）
- `backend/app/main.py`：删除 `/workspaces` 的 StaticFiles 挂载（保留 mkdir），更新注释
- `frontend/src/components/ChatWorkspace.tsx` markdown 链接渲染器（`:44-47`）：识别 `/api/workspaces/` 开头的 href，onClick 拦截 → 带 token fetch blob → `a[download]` 触发下载；其余链接行为不变（顺带修复原相对链接在前端域 404 的问题）

### 4. SSE 错误脱敏
- `backend/app/chat.py:195`：`str(exc)` → 固定文案 `"生成回复时出现错误，请稍后重试"`（详细异常保留在 `logger.exception`）

### 5. 验证码内存泄漏
- `backend/app/captcha.py`：`create_captcha()` 生成前清扫过期条目（`now - issued >= _CAPTCHA_TTL` 删除）；verify 侧已有 pop + 时效校验，不动

## 验证
1. 后端 `python -m compileall -q app` + 在 backend/ 下跑现有 pytest 全绿
2. 临时清空 JWT_SECRET 启动 → 确认启动失败（fail-fast 生效）；恢复后正常启动
3. curl 验证：匿名 `GET /files/<name>`、`GET /api/workspaces/...` → 401；登录后带 token → 200；越权会话的工作区文件 → 404
4. 前端 `npm run build` 通过；手测图片渲染、权限确认、注册登录验证码

## 说明
- 图片"知道随机文件名的登录用户可跨用户引用"为已接受残留风险（128-bit 随机名不可枚举），本批不做归属表
- 不改动第二批及以后范围（SSE 限速、错误路径保存半截回复等）