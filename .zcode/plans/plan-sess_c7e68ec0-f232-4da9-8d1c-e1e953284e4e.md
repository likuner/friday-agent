# 前端数学公式渲染实现方案

## 现状与问题

- assistant 消息唯一的 Markdown 渲染点在 `frontend/src/components/ChatWorkspace.tsx:586-590`：`ReactMarkdown + remark-gfm + remark-breaks`，无任何公式能力，模型输出的 `$...$` / `$$...$$` / `\(...\)` / `\[…\]` 全部显示为原始文本。
- 流式打字机每 50ms 增量追加内容并整列表 `setMessages` 重渲染；主题为 `data-theme="dark"` + CSS 变量（`rgb(var(--token))`），全局样式只有 `globals.css`。

## 选型：KaTeX + remark-math + rehype-katex

- **KaTeX 而非 MathJax**：同步渲染、体积小一个量级，与现有 50ms 高频重渲染天然兼容（MathJax 异步渲染在流式下会闪烁）；输出 HTML+CSS 且颜色继承 `currentColor`，深浅主题自动适配。
- 版本与现有 unified 11 / react-markdown 9 生态匹配：`katex@^0.16` + `remark-math@^6` + `rehype-katex@7`。

## 改动清单

### 1. 安装依赖（frontend/）
`npm install katex remark-math rehype-katex`

### 2. 新建 `frontend/src/components/Markdown.tsx`（核心）

抽出可复用的 memo 化渲染组件：

- **定界符归一化** `normalizeMathDelimiters(content)`：GLM 等模型常输出 `\[...\]` / `\(...\)`，remark-math 不识别。先把正文按「代码围栏 ``` 与行内码 `」切段，仅在非代码段做成对替换：`\[...\]` → `$$...$$`，`\(...\)` → `$...$`。代码块内的 `\[`（如正则示例）不受影响。
- **插件链**：`remarkPlugins={[remarkGfm, remarkBreaks, remarkMath]}` + `rehypePlugins={[[rehypeKatex, { strict: false, errorColor: 'rgb(var(--brand))' }]]}`（`strict:false` 容忍流式半截公式；错误色用主题变量而非默认红色，深浅色都不刺眼）。
- 把 `ChatWorkspace.tsx:43-66` 的 `markdownComponents`（外链新窗口 + 工作区文件鉴权下载，内含 `useAuth`）原样迁入本文件。
- 组件用 `React.memo` 包裹：流式期间每 50ms 整列表重渲染时，只有正在增长的消息重新解析 Markdown，历史消息全部跳过（顺带修掉现状的性能浪费）。

### 3. `ChatWorkspace.tsx` 接入

- 删除本地 `markdownComponents` 与 `ReactMarkdown/remarkGfm/remarkBreaks/Components` 的 import。
- 586-590 行替换为 `<div className="md"><Markdown>{item.content}</Markdown></div>`。

### 4. `layout.tsx` 引入样式

`import 'katex/dist/katex.min.css';`（加在 antd reset 之后）。Next 16 App Router 自动打包 CSS 与随包的 woff2 字体，无需任何构建配置（当前也没有 next.config）。

### 5. `globals.css` 追加 KaTeX 排版（.md 作用域，约 5 行）

```css
/* 数学公式（KaTeX）：颜色随 currentColor 自动适配深浅主题 */
.md .katex { font-size: 1.06em; }
.md .katex-display { margin: 0 0 12px; overflow-x: auto; padding: 2px 0; }
.md .katex-display:last-child { margin-bottom: 0; }
```

宽公式在窄屏横向滚动，不撑破气泡。

## 流式兼容性（无需额外机制）

remark-math 对未闭合的 `$$…` / `$…` 按普通文本处理：流式过程中半截公式先显示原始 LaTeX，闭合定界符到达后一次性渲染成型——与主流 Chat UI 行为一致，打字机节奏代码不动。

## 不改动范围

- 用户消息气泡（纯文本 by design）与深度思考面板不渲染公式。
- 后端不需要任何改动。

## 涉及文件

| 文件 | 改动 |
|---|---|
| `frontend/package.json` | +3 依赖 |
| `frontend/src/components/Markdown.tsx` | 新建（归一化 + memo + 插件链 + 迁入 a 标签定制） |
| `frontend/src/components/ChatWorkspace.tsx` | 替换渲染调用、删除迁出代码 |
| `frontend/src/app/layout.tsx` | +1 行 CSS import |
| `frontend/src/app/globals.css` | +~5 行 KaTeX 样式 |

## 验证

1. `cd frontend && npm run build` 通过（类型 + 构建）。
2. 临时建 `src/app/__mathtest/page.tsx` 复用 `Markdown` 组件渲染样例（行内 `$E=mc^2$`、块级 `$$\int_a^b$$`、`\[…\]` 归一化、含 `\[` 的代码块不受污染、未闭合 `$$` 半截文本、公式+表格混排），浏览器截图核对深/浅两种主题，完成后删除该路由。
3. 如遇 Turbopack 新类不生效，按 globals.css 内已有注释 touch 该文件强制重建。