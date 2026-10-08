'use client';
// 临时验证页：公式/货币过滤/代码高亮/复制/mermaid/流式分块（验证完删除）
import { useState } from 'react';
import Markdown from '@/components/Markdown';
import { useThemeToggle } from '@/app/providers';

const SAMPLE = [
  '价格是 $100和$200 两种，It costs $5, $10 or $20 —— 这些都应保持普通文本。',
  '而变量 $x$ 与 $E=mc^2$ 正常渲染。',
  '裸环境：\n\\begin{equation}\n\\nabla \\cdot \\mathbf{E} = \\frac{\\rho}{\\varepsilon_0}\n\\end{equation}',
  '正文含游离 $$ 的段落（不应吞掉后面的内容）：\n$$\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}$$',
  '多行行尾闭合（修复前会吞到文件尾）：\n$$x = y\nz = w$$',
  '模型规范形态不受影响：\n$$\n\\begin{gathered}\nx &= 1\n\\end{gathered}\n$$',
  '```ts\n// 高亮测试\nconst greet = (name: string): string => `Hello, ${name}!`;\nfor (let i = 0; i < 10; i += 1) console.log(i, greet(`user${i}`));\n```',
  '```nonexistentlang\n随便写点东西，未知语言不能崩\n```',
  '```mermaid\nflowchart LR\n    A[用户提问] --> B{RAG 检索}\n    B -->|命中| C[引用回答]\n    B -->|未命中| D[联网搜索]\n    D --> C\n```',
  '公式回归：$\\alpha+\\beta$ 与 $$\\int_a^b f(x)\\,dx = F(b) - F(a)$$',
].join('\n\n');

export default function MathTestPage() {
  const { theme, toggle } = useThemeToggle();
  const [len, setLen] = useState(SAMPLE.length);
  const streaming = len < SAMPLE.length;

  const play = () => {
    setLen(0);
    const timer = setInterval(() => {
      setLen((current) => {
        if (current >= SAMPLE.length) {
          clearInterval(timer);
          return current;
        }
        return Math.min(SAMPLE.length, current + 9);
      });
    }, 30);
  };

  return (
    <main className="min-h-screen bg-canvas p-6">
      <div className="mb-4 flex gap-2">
        <button type="button" onClick={play} className="rounded border border-line px-3 py-1 text-sm">模拟流式输出</button>
        <button type="button" onClick={toggle} className="rounded border border-line px-3 py-1 text-sm">切换主题（{theme}）</button>
      </div>
      <div className="md mx-auto max-w-3xl rounded-xl bg-surface p-6">
        <Markdown streaming={streaming}>{SAMPLE.slice(0, len)}</Markdown>
      </div>
    </main>
  );
}
