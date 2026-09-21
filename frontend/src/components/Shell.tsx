'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { CloseOutlined, DeleteOutlined, HistoryOutlined, LogoutOutlined, MessageOutlined, MenuFoldOutlined, MenuUnfoldOutlined, MoreOutlined, PlusCircleOutlined, RobotOutlined } from '@ant-design/icons';
import { App, Button, Dropdown } from 'antd';
import { conversations, createConversation, deleteConversation, type Conversation } from '@/lib/api';
import { useAuth } from '@/store/auth';
import { useUI } from '@/store/ui';

// 折叠态样式统一用 sidebar-collapsed: 变体（见 tailwind.config.ts），由 <html data-sidebar> 决定：
// 服务端/客户端输出的 class 一致 → 无水合不一致；首帧前已写好属性 → 刷新不闪动。
export default function Shell({ children }: { children: React.ReactNode }) {
  const { modal } = App.useApp();
  const pathname = usePathname();
  const router = useRouter();
  const { token, user, hydrate, logout } = useAuth();
  const { sidebarOpen, setSidebarOpen } = useUI();
  const [items, setItems] = useState<Conversation[]>([]);

  const load = () => token && conversations(token).then(setItems).catch(() => { logout(); router.replace('/login'); });
  useEffect(() => { hydrate(); }, [hydrate]);
  useEffect(() => { if (token) load(); else if (token === null && localStorage.getItem('friday_token') === null) router.replace('/login'); }, [token, pathname]);
  // 路由变化时收起移动端抽屉
  useEffect(() => { setSidebarOpen(false); }, [pathname, setSidebarOpen]);

  const toggleCollapsed = () => {
    const collapsed = document.documentElement.getAttribute('data-sidebar') === 'collapsed';
    const next = collapsed ? 'expanded' : 'collapsed';
    document.documentElement.setAttribute('data-sidebar', next);
    localStorage.setItem('friday_sidebar_collapsed', next === 'collapsed' ? '1' : '0');
  };
  const add = async () => { if (!token) return; const item = await createConversation(token); setItems((old) => [item, ...old]); router.push(`/chat/${item.id}`); };
  // 退出登录属于不可逆操作（要重新登录），先弹二次确认
  const confirmLogout = () => modal.confirm({
    title: '退出登录？',
    content: '退出后需要重新输入账号密码才能继续使用。',
    okText: '退出登录',
    cancelText: '再想想',
    okButtonProps: { danger: true },
    onOk: () => { logout(); router.replace('/login'); },
  });
  const remove = (id: string) => modal.confirm({ title: '删除这个对话？', content: '对话和全部消息将被永久删除。', okButtonProps: { danger: true }, onOk: async () => { if (!token) return; await deleteConversation(token, id); setItems((old) => old.filter((item) => item.id !== id)); if (pathname.includes(id)) router.push('/chat'); } });

  const label = (text: string) => <span className="whitespace-nowrap sidebar-collapsed:md:hidden">{text}</span>;
  const navClass = (active: boolean) => `flex items-center gap-2 rounded-xl px-3 py-2 text-sm transition-colors sidebar-collapsed:md:justify-center sidebar-collapsed:md:px-0 ${active ? 'bg-brand-soft font-medium text-brand-text shadow-[inset_3px_0_0_#4d6bfe]' : 'text-body hover:bg-hover'}`;

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {sidebarOpen && <div className="fixed inset-0 z-30 bg-overlay/30 backdrop-blur-[2px] md:hidden" onClick={() => setSidebarOpen(false)} />}

      {/* overflow-hidden：宽度过渡期间裁掉溢出内容，避免文字溢出到主区域 */}
      <aside className={`fixed inset-y-0 left-0 z-40 flex h-full w-[268px] shrink-0 flex-col overflow-hidden border-r border-line bg-gradient-to-b from-panel via-soft to-panel-to transition-[width,transform] duration-200 sidebar-collapsed:md:w-[84px] md:static md:translate-x-0 ${sidebarOpen ? 'translate-x-0 shadow-[0_0_60px_rgba(15,27,61,.25)]' : '-translate-x-full'}`}>

        {/* 头部：折叠后 px-4(32px) 装不下 32px logo + 32px 折叠按钮，收窄为 md:px-2 */}
        <div className="flex h-[60px] shrink-0 items-center px-4 sidebar-collapsed:md:px-2">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-[#4d6bfe] to-[#7c94ff] text-white shadow-[0_8px_18px_-8px_rgba(77,107,254,.9)]"><RobotOutlined /></span>
          <b className="ml-2 min-w-0 flex-1 truncate whitespace-nowrap bg-gradient-to-r from-ink-soft to-[#4d6bfe] bg-clip-text text-[17px] text-transparent sidebar-collapsed:md:hidden">Friday</b>
          <button title="收起菜单" onClick={() => setSidebarOpen(false)} className="ml-auto grid h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover md:hidden"><CloseOutlined /></button>
          <button title="展开/收起侧边栏" onClick={toggleCollapsed} className="ml-auto hidden h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover md:grid"><span className="grid place-items-center sidebar-collapsed:md:hidden"><MenuFoldOutlined /></span><span className="hidden place-items-center sidebar-collapsed:md:grid"><MenuUnfoldOutlined /></span></button>
        </div>

        <div className="px-3 pb-2 sidebar-collapsed:md:px-2">
          <Button onClick={add} icon={<PlusCircleOutlined />} block title="开启新对话" className="h-10 text-left sidebar-collapsed:md:px-0"><span className="whitespace-nowrap sidebar-collapsed:md:hidden">开启新对话</span></Button>
        </div>

        <nav className="space-y-1 px-3 pb-2 sidebar-collapsed:md:px-2">
          <Link href="/chat" className={navClass(pathname.startsWith('/chat'))}><MessageOutlined />{label('对话')}</Link>
          <Link href="/history" className={navClass(pathname === '/history')}><HistoryOutlined />{label('历史记录')}<span className="ml-auto whitespace-nowrap text-xs sidebar-collapsed:md:hidden">{items.length}</span></Link>
        </nav>

        <div className="scroll-hide min-h-0 flex-1 overflow-y-auto px-3">
          <div className="sidebar-collapsed:md:hidden">
            <p className="px-3 py-2 text-xs text-muted-weak">最近对话</p>
            {items.map((item) => <div key={item.id} className={`group mb-1 flex items-center rounded-xl ${pathname.includes(item.id) ? 'bg-brand-soft' : 'hover:bg-hover'}`}><Link href={`/chat/${item.id}`} className="min-w-0 flex-1 truncate px-3 py-2 text-[13.5px] text-body">{item.title}</Link><Dropdown menu={{ items: [{ key: 'delete', danger: true, icon: <DeleteOutlined />, label: '删除', onClick: () => remove(item.id) }] }}><button className="mr-2 grid h-7 w-7 place-items-center rounded-lg text-muted opacity-0 group-hover:opacity-100"><MoreOutlined /></button></Dropdown></div>)}
          </div>
        </div>

        {/* 底部：折叠后 p-3(24px)+gap-2(8px) 会让 32px 头像 + 32px 退出按钮溢出，收窄为 md:p-2 并去掉间距 */}
        <div className="border-t border-line p-3 sidebar-collapsed:md:p-2">
          <div className="flex items-center gap-2 rounded-xl px-2 py-2 sidebar-collapsed:md:justify-center sidebar-collapsed:md:gap-0 sidebar-collapsed:md:px-0">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-[#4d6bfe] text-sm text-white">{user?.username.slice(0, 1).toUpperCase()}</span>
            <span className="min-w-0 flex-1 truncate whitespace-nowrap text-sm sidebar-collapsed:md:hidden">{user?.username}</span>
            <button title="退出登录" onClick={confirmLogout} className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover"><LogoutOutlined /></button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}
