import type { Metadata } from 'next';
import { cookies } from 'next/headers';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import Providers from './providers';
import 'antd/dist/reset.css';
import './globals.css';

export const metadata: Metadata = { title: 'Friday Agent', description: 'AI Agent 助手' };

// 折叠状态无法在服务端得知（存在 localStorage），因此仍在首帧前用内联脚本写到 <html data-sidebar>，
// 保证首帧就是最终宽度；主题则走 cookie，由服务端直接渲染 <html data-theme>，两者都不会闪。
const SIDEBAR_INIT = `try{document.documentElement.setAttribute('data-sidebar',localStorage.getItem('friday_sidebar_collapsed')==='1'?'collapsed':'expanded')}catch(e){document.documentElement.setAttribute('data-sidebar','expanded')}`;

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const theme = (await cookies()).get('friday_theme')?.value === 'dark' ? 'dark' : 'light';
  return <html lang="zh-CN" data-theme={theme} suppressHydrationWarning><body suppressHydrationWarning><script dangerouslySetInnerHTML={{ __html: SIDEBAR_INIT }} /><AntdRegistry><Providers initialTheme={theme}>{children}</Providers></AntdRegistry></body></html>;
}
