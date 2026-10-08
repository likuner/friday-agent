'use client';

import { memo, useEffect, useMemo, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import rehypeHighlight from 'rehype-highlight';
import type { PluggableList } from 'unified';
import type { Element, Root } from 'hast';
import { authAssetUrl, downloadWorkspaceFile } from '@/lib/api';
import { useAuth } from '@/store/auth';
import { useThemeToggle } from '@/app/providers';

/* ---------- 定界符归一化 ---------- */

// 模型（尤其 GLM）常输出 remark-math 不识别的 \[…\]、\(…\) 与不带 $$ 的顶级环境，
// 统一转成 $…$ / $$…$$。代码围栏与行内码必须原样保留（含流式中未闭合的围栏），
// 否则正则、字符串示例里的 \[ 会被误改。
const CODE_LIKE = /```[\s\S]*?(?:```|$)|`[^`\n]+`/g;
// 顶级展示环境；aligned/cases/matrix 等只出现在 $$ 内部，列入会被二次包裹
const BARE_ENV = /\\begin\{((?:equation|align|gather|eqnarray|multline)\*?)}([\s\S]*?)\\end\{\1\}/g;

function convertMathDelimiters(text: string): string {
  return text
    // 行首 $$…$$（flow 位置）一律重排成「开闭定界符各占一行」。micromark 的
    // flow 数学与代码围栏同构：开行 $$ 之后的内容会像 info string 一样被丢弃，
    // 行尾的闭合 $$ 也匹配不到（只认行首），会一路吞到下一个行首 $$——正文里
    // 的代码围栏都可能被卷进公式。只认行首是为了不与正文里游离的行内 $$ 跨段
    // 配对（行内 $$ 不产生 flow 数学，原样保留即可）。
    .replace(/^([ \t]{0,3})\$\$([\s\S]*?)\$\$/gm, (_match, indent: string, body: string) => `${indent}$$\n${body.trim()}\n$$\n`)
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `\n$$\n${body.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${body}$`)
    // 裸环境补 $$。守卫看前后修剪空白后是否紧邻 $$：覆盖「$$\begin…（同行）」
    // 与重排后「$$\n\begin…（下一行）」两种已包裹形态，避免二次包裹。
    .replace(BARE_ENV, (match, _env: string, _body: string, offset: number, whole: string) => {
      const before = whole.slice(0, offset).trimEnd();
      const after = whole.slice(offset + match.length).trimStart();
      return before.endsWith('$$') || after.startsWith('$$') ? match : `\n$$\n${match}\n$$\n`;
    });
}

function normalizeMathDelimiters(content: string): string {
  let out = '';
  let last = 0;
  for (const match of content.matchAll(CODE_LIKE)) {
    const index = match.index ?? 0;
    out += convertMathDelimiters(content.slice(last, index));
    out += match[0];
    last = index + match[0].length;
  }
  return out + convertMathDelimiters(content.slice(last));
}

/* ---------- 货币误判过滤 ---------- */

// remark-math 对单 $ 无语义判断，中文无空格书写「$100和$200」、英文「$5, $10」
// 都会被当成行内公式。在进 KaTeX 前把「内容含 CJK 或纯数字/标点」的行内公式
// 还原成普通文本；块级 $$ 不动（货币金额不会跨 $$）。
const CJK_RE = /[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]/;
const CURRENCY_RE = /^[\d\s.,，。%‰+-]+$/;

function unmathCurrency(tree: Root) {
  const visit = (node: Root | Element) => {
    const children = node.children as Array<Element | Root['children'][number]>;
    for (let i = 0; i < children.length; i += 1) {
      const child = children[i];
      if (child.type !== 'element') continue;
      const cls = child.properties?.className;
      if (
        child.tagName === 'code' &&
        Array.isArray(cls) &&
        cls.includes('language-math') &&
        cls.includes('math-inline')
      ) {
        const value = child.children
          .filter((item) => item.type === 'text')
          .map((item) => (item.type === 'text' ? item.value : ''))
          .join('');
        if (CJK_RE.test(value) || CURRENCY_RE.test(value.trim())) {
          children[i] = { type: 'text', value: `$${value}$` };
          continue;
        }
      }
      visit(child);
    }
  };
  visit(tree);
}

/* ---------- 代码块：高亮 + 复制按钮 / mermaid ---------- */

// 递归取 React 子树里的纯文本（hljs 会把代码切成多层 span）
function reactText(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(reactText).join('');
  if (typeof node === 'object' && 'props' in node) {
    return reactText((node as { props?: { children?: ReactNode } }).props?.children);
  }
  return '';
}

function childClassName(node: ReactNode): string {
  if (node && typeof node === 'object' && 'props' in node) {
    const cls = (node as { props?: { className?: unknown } }).props?.className;
    if (typeof cls === 'string') return cls;
    if (Array.isArray(cls)) return cls.join(' ');
  }
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = childClassName(item);
      if (found) return found;
    }
  }
  return '';
}

// mermaid 懒加载：约 1MB 且多数会话用不到，动态 import 不进主包。
// securityLevel:strict 会净化节点文本；渲染结果只含 SVG，注入风险可控。
function MermaidBlock({ code }: { code: string }) {
  const { theme } = useThemeToggle();
  const [svg, setSvg] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setFailed(false);
    // 300ms 防抖：流式期间图源码每 50ms 变化，等稳定后再渲染；
    // 半张图解析失败属正常，回退为源码展示，流完自然恢复
    const timer = setTimeout(() => {
      import('mermaid')
        .then((module) => {
          module.default.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: theme === 'dark' ? 'dark' : 'default',
          });
          return module.default.render(`mmd-${crypto.randomUUID()}`, code);
        })
        .then(
          (result) => { if (alive) setSvg(result.svg); },
          () => { if (alive) setFailed(true); },
        );
    }, 300);
    return () => { alive = false; clearTimeout(timer); };
  }, [code, theme]);
  if (failed) {
    return (
      <div className="code-block">
        <pre>{code}</pre>
        <div className="code-head"><span className="code-lang">mermaid 渲染失败</span></div>
      </div>
    );
  }
  return (
    <div className="mermaid-box">
      {svg
        ? <div className="mermaid-svg" dangerouslySetInnerHTML={{ __html: svg }} />
        : <span className="mermaid-hint">图表生成中…</span>}
    </div>
  );
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const raw = useMemo(() => reactText(children), [children]);
  const lang = useMemo(() => childClassName(children).match(/language-([\w+-]+)/)?.[1] || '', [children]);
  if (lang === 'mermaid') return <MermaidBlock code={raw} />;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板权限被拒时静默失败，按钮态不变
    }
  };
  return (
    <div className="code-block">
      <pre>{children}</pre>
      <div className="code-head">
        {lang ? <span className="code-lang">{lang}</span> : null}
        <button type="button" className="code-copy" onClick={copy}>{copied ? '已复制' : '复制'}</button>
      </div>
    </div>
  );
}

/* ---------- markdown 图片：工作区鉴权 ---------- */

// 正文里的 /api/ 开头图片（工作区产出）带不上 Authorization，取 blob 后展示；
// 外链与其他相对路径直出。降级为文字占位避免裂图。
function MdImage({ src, alt }: { src?: string; alt?: string }) {
  const token = useAuth((state) => state.token);
  const authorized = !!src && src.startsWith('/api/');
  const [resolved, setResolved] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setFailed(false);
    setResolved(undefined);
    if (!authorized || !token || !src) return;
    authAssetUrl(token, src).then(
      (url) => { if (alive) setResolved(url); },
      () => { if (alive) setFailed(true); },
    );
    return () => { alive = false; };
  }, [authorized, src, token]);
  if (authorized) {
    if (failed) return <span className="md-img-fallback">图片加载失败</span>;
    if (!resolved) return <span className="md-img-fallback">图片加载中…</span>;
    return <img src={resolved} alt={alt || ''} loading="lazy" />;
  }
  return <img src={src} alt={alt || ''} loading="lazy" />;
}

/* ---------- 渲染管线 ---------- */

// Markdown 渲染定制：引用来源等外链一律新标签页打开，避免把当前对话导航走
const markdownComponents: Components = {
  a: ({ children, href }) => {
    const token = useAuth((state) => state.token);
    const external = /^https?:\/\//i.test(href || '');
    // 工作区产出文件（/api/workspaces/<cid>/<file>）是鉴权路由：
    // 浏览器直接导航带不上 Authorization，拦截点击改为取 blob 后下载
    if (href && /\/api\/workspaces\//.test(href)) {
      return (
        <a
          href={href}
          onClick={(event) => {
            event.preventDefault();
            if (token) {
              downloadWorkspaceFile(token, href).catch((error) => console.warn('工作区文件下载失败', error));
            }
          }}
        >
          {children}
        </a>
      );
    }
    return <a href={href} {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>{children}</a>;
  },
  img: ({ src, alt }) => <MdImage src={typeof src === 'string' ? src : undefined} alt={typeof alt === 'string' ? alt : undefined} />,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
};

const REMARK_PLUGINS: PluggableList = [remarkGfm, remarkBreaks, remarkMath];
// strict:false 容忍非严格 LaTeX；错误不抛红而用主题色，流式半截公式不刺眼。
// 顺序：先拦货币误判 → KaTeX 消费公式 → 剩余代码块做高亮（未知语言自动跳过）
const REHYPE_PLUGINS: PluggableList = [
  () => unmathCurrency,
  [rehypeKatex, { strict: false, errorColor: 'rgb(var(--brand))' }],
  rehypeHighlight,
];

function MarkdownBody({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={markdownComponents}>
      {children}
    </ReactMarkdown>
  );
}

/* ---------- 流式分块 ---------- */

// 流式期间正文每 50ms 追加，整条消息重解析会让已完成的公式（KaTeX）和代码
// （hljs）逐 tick 重算。按空行把正文切成块，已完成块 memo 住、只重解析生长中
// 的尾块；围栏代码与跨空行的 $$ 块不切断。流式结束后整体单次渲染，避免分块
// 把松散列表/引用拆开的样子留在最终结果里。
function splitStableBlocks(content: string): string[] {
  const lines = content.split('\n');
  const blocks: string[] = [];
  let current: string[] = [];
  let fence = false;
  let math = false;
  for (const line of lines) {
    if (/^\s{0,3}(?:```|~~~)/.test(line)) fence = !fence;
    else if (/^\s{0,3}\$\$/.test(line) && (line.match(/\$\$/g)?.length ?? 0) % 2 === 1) math = !math;
    current.push(line);
    if (!fence && !math && line.trim() === '') {
      blocks.push(current.join('\n'));
      current = [];
    }
  }
  if (current.length) blocks.push(current.join('\n'));
  return blocks;
}

const StableBlock = memo(function StableBlock({ children }: { children: string }) {
  return <MarkdownBody>{children}</MarkdownBody>;
});

// 消息正文渲染入口。memo 是流式性能关键：打字机每 50ms 整列表重渲染，
// 历史消息 content 不变即跳过整棵 Markdown 重新解析。
// 流式期间未闭合的 $$…/$… 被 remark-math 当普通文本，闭合后一次性渲染成型。
const Markdown = memo(function Markdown({ children, streaming = false }: { children: string; streaming?: boolean }) {
  const content = useMemo(() => normalizeMathDelimiters(children), [children]);
  if (!streaming) return <MarkdownBody>{content}</MarkdownBody>;
  const blocks = splitStableBlocks(content);
  return <>{blocks.map((block, index) => <StableBlock key={index}>{block}</StableBlock>)}</>;
});

export default Markdown;
