import type { Metadata } from 'next';
import { cookies } from 'next/headers';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import Providers from './providers';
import 'antd/dist/reset.css';
import './globals.css';

export const metadata: Metadata = { title: 'Friday Agent', description: 'AI Agent 助手' };

// 主题与侧边栏折叠状态都存 cookie，由服务端直接渲染到 <html> 上：
// - 首帧即最终外观，不会出现「展开→收起」的闪动
// - 不需要在组件树里渲染 <script>（React 19 会报 "Encountered a script tag"）
export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const jar = await cookies();
  const theme = jar.get('friday_theme')?.value === 'dark' ? 'dark' : 'light';
  const sidebar = jar.get('friday_sidebar')?.value === 'collapsed' ? 'collapsed' : 'expanded';
  return (
    <html lang="zh-CN" data-theme={theme} data-sidebar={sidebar} suppressHydrationWarning>
      <body suppressHydrationWarning>
        <AntdRegistry>
          <Providers initialTheme={theme}>{children}</Providers>
        </AntdRegistry>
      </body>
    </html>
  );
}
