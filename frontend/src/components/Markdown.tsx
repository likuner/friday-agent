'use client';

import { memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { downloadWorkspaceFile } from '@/lib/api';
import { useAuth } from '@/store/auth';

// 定界符归一化：模型（尤其 GLM）常输出 remark-math 不识别的 \[…\] / \(…\)，
// 成对换成 $$…$$ / $…$。代码围栏与行内码必须原样保留（含流式中未闭合的围栏），
// 否则正则、字符串示例里的 \[ 会被误改。
const CODE_LIKE = /```[\s\S]*?(?:```|$)|`[^`\n]+`/g;

function convertMathDelimiters(text: string): string {
  return text
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `$$${body}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${body}$`);
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
};

// 消息正文渲染（含数学公式）。memo 是流式性能关键：打字机每 50ms 整列表重渲染，
// 历史消息 content 不变即跳过整棵 Markdown 重新解析。
// 流式期间未闭合的 $$…/$… 被 remark-math 当普通文本，闭合后一次性渲染成型。
const Markdown = memo(function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks, remarkMath]}
      // strict:false 容忍非严格 LaTeX；错误不抛红而用主题色，流式半截公式不刺眼
      rehypePlugins={[[rehypeKatex, { strict: false, errorColor: 'rgb(var(--brand))' }]]}
      components={markdownComponents}
    >
      {normalizeMathDelimiters(children)}
    </ReactMarkdown>
  );
});

export default Markdown;
