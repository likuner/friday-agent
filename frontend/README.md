# Friday Agent 前端

基于 Next.js App Router、TypeScript、Ant Design、Tailwind CSS 和 Zustand 的 AI Agent 工作台。

## 环境要求

- Node.js 20+
- npm 10+
- 已启动后端 API，默认地址为 `http://localhost:8000`

## 安装依赖

```bash
npm install
```

## 配置环境变量

```bash
cp .env.example .env.local
```

默认配置：

```env
NEXT_PUBLIC_API_URL=http://localhost:8000/api
```

## 启动开发服务器

```bash
npm run dev
```

打开 <http://localhost:3000>。

## 页面

- `/login`：用户名、密码和图片验证码登录/注册
- `/chat`：新建对话和 AI Agent 流式聊天
- `/chat/{conversationId}`：查看指定对话
- `/history`：搜索、重命名、删除历史对话

## 构建检查

```bash
npm run build
```

## 技术说明

- 登录成功后，JWT 和用户信息保存在浏览器本地存储中。
- 聊天回复通过后端 SSE 接口增量渲染。
- Zustand 管理登录状态，后端请求集中在 `src/lib/api.ts`。
- 界面布局和交互参考项目根目录 `html/` 下的原型文档。
