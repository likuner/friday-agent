'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { CloseOutlined, DeleteOutlined, FolderAddOutlined, FolderOpenOutlined, HistoryOutlined, LoadingOutlined, LogoutOutlined, MessageOutlined, MenuFoldOutlined, MenuUnfoldOutlined, MoreOutlined, PlusCircleOutlined, RobotOutlined } from '@ant-design/icons';
import { App, Button, Dropdown, Tooltip } from 'antd';
import { conversations, createConversation, deleteConversation, pickWorkspace, setWorkspace, type Conversation } from '@/lib/api';
import { shortDir } from '@/lib/path';
import LoadMore from '@/components/LoadMore';
import { useAuth } from '@/store/auth';
import { useUI } from '@/store/ui';

// 侧边栏两组各自分页：每页 20 条，滚到底自动加载下一页（见 LoadMore）
const PAGE_SIZE = 20;
// 侧边栏会话分组：workspace = 有工作区（Agent 工具会话），plain = 普通对话
type Group = 'workspace' | 'plain';

// 折叠态样式统一用 sidebar-collapsed: 变体（见 tailwind.config.ts），由 <html data-sidebar> 决定
// （该属性由服务端根据 cookie 渲染，见 app/layout.tsx）：
// 服务端/客户端输出的 class 一致 → 无水合不一致；首帧前已写好属性 → 刷新不闪动。
export default function Shell({ children }: { children: React.ReactNode }) {
  const { modal, message } = App.useApp();
  const pathname = usePathname();
  const router = useRouter();
  const { token, user, hydrate, logout } = useAuth();
  const { sidebarOpen, setSidebarOpen, conversationsVersion, bumpConversations } = useUI();
  // 两组各自持有「已加载列表 + 服务端总数」，hasMore 由 length < total 推导（删除/新增后自洽）
  const [workspaceItems, setWorkspaceItems] = useState<Conversation[]>([]);
  const [plainItems, setPlainItems] = useState<Conversation[]>([]);
  const [workspaceTotal, setWorkspaceTotal] = useState(0);
  const [plainTotal, setPlainTotal] = useState(0);
  const [loadingKind, setLoadingKind] = useState<Record<Group, boolean>>({ workspace: false, plain: false });
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  // 请求去重（state 异步，滚动可能连触发多次）
  const loadingRef = useRef<Record<Group, boolean>>({ workspace: false, plain: false });

  // 拉某一组的一页：offset=0 替换（首次/刷新），否则追加并按 id 去重（并发/新会话插入兜底）
  const fetchGroup = async (kind: Group, offset: number, replace: boolean) => {
    if (!token || loadingRef.current[kind]) return;
    loadingRef.current[kind] = true;
    setLoadingKind((old) => ({ ...old, [kind]: true }));
    try {
      const page = await conversations(token, { kind, limit: PAGE_SIZE, offset });
      const append = (old: Conversation[]) => {
        if (replace) return page.items;
        const seen = new Set(old.map((item) => item.id));
        return [...old, ...page.items.filter((item) => !seen.has(item.id))];
      };
      if (kind === 'workspace') { setWorkspaceItems(append); setWorkspaceTotal(page.total); }
      else { setPlainItems(append); setPlainTotal(page.total); }
    } catch {
      logout(); router.replace('/login');
    } finally {
      loadingRef.current[kind] = false;
      setLoadingKind((old) => ({ ...old, [kind]: false }));
    }
  };
  useEffect(() => { hydrate(); }, [hydrate]);
  useEffect(() => {
    if (token) { void fetchGroup('workspace', 0, true); void fetchGroup('plain', 0, true); }
    else if (token === null && localStorage.getItem('friday_token') === null) router.replace('/login');
  }, [token, pathname, conversationsVersion]);
  // 路由变化时收起移动端抽屉
  useEffect(() => { setSidebarOpen(false); }, [pathname, setSidebarOpen]);

  const toggleCollapsed = () => {
    const collapsed = document.documentElement.getAttribute('data-sidebar') === 'collapsed';
    const next = collapsed ? 'expanded' : 'collapsed';
    document.documentElement.setAttribute('data-sidebar', next);
    // 写 cookie（而非 localStorage），服务端下次渲染即可直接输出正确状态
    document.cookie = `friday_sidebar=${next}; path=/; max-age=31536000; SameSite=Lax`;
  };
  const add = async () => {
    if (!token) return;
    const item = await createConversation(token);
    setPlainItems((old) => [item, ...old]);
    setPlainTotal((count) => count + 1);
    router.push(`/chat/${item.id}`);
  };
  // 新建工作区对话：先弹原生目录选择框，取消则零副作用；选定才建会话并绑定（工作区此后不可改）
  const addWorkspaceConversation = async () => {
    if (!token || creatingWorkspace) return;
    setCreatingWorkspace(true);
    try {
      const { path } = await pickWorkspace(token);
      if (!path) return;
      const item = await createConversation(token);
      await setWorkspace(token, item.id, path);
      setWorkspaceItems((old) => [{ ...item, workspace_root: path }, ...old]);
      setWorkspaceTotal((count) => count + 1);
      bumpConversations();
      router.push(`/chat/${item.id}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '新建工作区失败');
    } finally {
      setCreatingWorkspace(false);
    }
  };
  // 退出登录属于不可逆操作（要重新登录），先弹二次确认
  const confirmLogout = () => modal.confirm({
    title: '退出登录？',
    content: '退出后需要重新输入账号密码才能继续使用。',
    okText: '退出登录',
    cancelText: '再想想',
    okButtonProps: { danger: true },
    onOk: () => { logout(); router.replace('/login'); },
  });
  const remove = (id: string) => modal.confirm({
    title: '删除这个对话？',
    content: '对话和全部消息将被永久删除。',
    okButtonProps: { danger: true },
    onOk: async () => {
      if (!token) return;
      await deleteConversation(token, id);
      // 命中哪一组就从哪一组移除，总数同步减一（hasMore 由 length/total 推导，保持自洽）
      if (workspaceItems.some((item) => item.id === id)) {
        setWorkspaceItems((old) => old.filter((item) => item.id !== id));
        setWorkspaceTotal((count) => Math.max(count - 1, 0));
      } else {
        setPlainItems((old) => old.filter((item) => item.id !== id));
        setPlainTotal((count) => Math.max(count - 1, 0));
      }
      if (pathname.includes(id)) router.push('/chat');
    },
  });

  const label = (text: string) => <span className="whitespace-nowrap sidebar-collapsed:md:hidden">{text}</span>;
  // 选中态：展开态用左侧 3px 竖条 + 浅蓝底；折叠态胶囊变窄，竖条会糊成月牙色块，故只留浅蓝底
  const navClass = (active: boolean) => `flex items-center gap-2 rounded-xl px-3 py-2 text-sm transition-colors sidebar-collapsed:md:justify-center sidebar-collapsed:md:px-0 ${active ? 'bg-brand-soft font-medium text-brand-text shadow-[inset_3px_0_0_#4d6bfe] sidebar-collapsed:md:shadow-none' : 'text-body hover:bg-hover'}`;
  // 会话行：工作区会话只展示目录名（悬停 title 给全路径），普通对话展示标题
  const row = (item: Conversation, withFolder: boolean) => (
    <div key={item.id} className={`group mb-1 flex items-center rounded-xl ${pathname.includes(item.id) ? 'bg-brand-soft' : 'hover:bg-hover'}`}>
      <Link href={`/chat/${item.id}`} className="flex min-w-0 flex-1 items-center gap-1.5 px-3 py-2 text-[13.5px] text-body">
        {withFolder ? (
          <>
            <FolderOpenOutlined className="shrink-0 text-[12px] text-brand-text" />
            <span className="truncate" title={item.workspace_root || undefined}>{shortDir(item.workspace_root!)}</span>
          </>
        ) : (
          <span className="truncate" title={item.title}>{item.title}</span>
        )}
      </Link>
      <Dropdown menu={{ items: [{ key: 'delete', danger: true, icon: <DeleteOutlined />, label: '删除', onClick: () => remove(item.id) }] }}>
        <button className="mr-2 grid h-7 w-7 shrink-0 place-items-center rounded-lg text-muted opacity-0 group-hover:opacity-100">
          <MoreOutlined />
        </button>
      </Dropdown>
    </div>
  );

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {sidebarOpen && <div className="fixed inset-0 z-30 bg-overlay/30 backdrop-blur-[2px] md:hidden" onClick={() => setSidebarOpen(false)} />}

      {/* overflow-hidden：宽度过渡期间裁掉溢出内容，避免文字溢出到主区域 */}
      <aside className={`fixed inset-y-0 left-0 z-40 flex h-full w-[268px] shrink-0 flex-col overflow-hidden border-r border-line bg-gradient-to-b from-panel via-soft to-panel-to transition-[width,transform] duration-200 sidebar-collapsed:md:w-[72px] md:static md:translate-x-0 ${sidebarOpen ? 'translate-x-0 shadow-[0_0_60px_rgba(15,27,61,.25)]' : '-translate-x-full'}`}>

        {/* 头部：展开态是品牌名 + 折叠按钮；折叠态 logo 隐藏，只留居中的展开按钮（窄栏放不下两个 32px 控件） */}
        <div className="flex h-[60px] shrink-0 items-center px-4 sidebar-collapsed:md:h-14 sidebar-collapsed:md:justify-center sidebar-collapsed:md:px-0">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-[#4d6bfe] to-[#7c94ff] text-white shadow-[0_8px_18px_-8px_rgba(77,107,254,.9)] sidebar-collapsed:md:hidden">
            <RobotOutlined />
          </span>
          <b className="ml-2 min-w-0 flex-1 truncate whitespace-nowrap bg-gradient-to-r from-ink-soft to-[#4d6bfe] bg-clip-text text-[17px] text-transparent sidebar-collapsed:md:hidden">Friday</b>
          <button title="收起菜单" onClick={() => setSidebarOpen(false)} className="ml-auto grid h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover md:hidden">
            <CloseOutlined />
          </button>
          <button title="展开/收起侧边栏" onClick={toggleCollapsed} className="ml-auto hidden h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover sidebar-collapsed:md:ml-0 md:grid">
            <span className="grid place-items-center sidebar-collapsed:md:hidden">
              <MenuFoldOutlined />
            </span>
            <span className="hidden place-items-center sidebar-collapsed:md:grid">
              <MenuUnfoldOutlined />
            </span>
          </button>
        </div>

        {/* 三个入口之间的间距：展开态 12px（+ → 对话）/ 8px（对话 → 历史记录）；折叠态统一 12px */}
        <div className="px-3 pb-3 sidebar-collapsed:md:px-2">
          <Button onClick={add} icon={<PlusCircleOutlined />} block title="开启新对话" className="h-10 text-left sidebar-collapsed:md:px-0">
            <span className="whitespace-nowrap sidebar-collapsed:md:hidden">开启新对话</span>
          </Button>
        </div>

        <nav className="space-y-2 px-3 pb-2 sidebar-collapsed:md:space-y-3 sidebar-collapsed:md:px-2">
          <Link href="/chat" className={navClass(pathname.startsWith('/chat'))}>
            <MessageOutlined />
            {label('对话')}
          </Link>
          <Link href="/history" className={navClass(pathname === '/history')}>
            <HistoryOutlined />
            {label('历史记录')}
            <span className="ml-auto whitespace-nowrap text-xs sidebar-collapsed:md:hidden">{workspaceTotal + plainTotal}</span>
          </Link>
        </nav>

        <div className="scroll-hide min-h-0 flex-1 overflow-y-auto px-3">
          <div className="sidebar-collapsed:md:hidden">
            {/* 「工作区」组头常驻：最右侧 + 是选定工作区的唯一入口（零工作区时也要能新建） */}
            <div className="flex items-center px-3 pb-1 pt-2">
              <p className="text-xs text-muted-weak">工作区</p>
              <Tooltip title="新建工作区" placement="top">
                <button
                  type="button"
                  onClick={addWorkspaceConversation}
                  disabled={creatingWorkspace}
                  aria-label="新建工作区"
                  className="ml-auto grid h-5 w-5 place-items-center rounded-md text-muted-weak hover:bg-hover hover:text-brand-text disabled:opacity-50"
                >
                  {creatingWorkspace ? <LoadingOutlined spin className="text-[11px]" /> : <FolderAddOutlined className="text-[12px]" />}
                </button>
              </Tooltip>
            </div>
            {workspaceItems.map((item) => row(item, true))}
            <LoadMore
              compact
              showEnd={false}
              hasMore={workspaceItems.length < workspaceTotal}
              loading={loadingKind.workspace}
              loaded={workspaceItems.length}
              total={workspaceTotal}
              onLoad={() => { void fetchGroup('workspace', workspaceItems.length, false); }}
            />
            {plainItems.length > 0 && (
              <>
                <p className="px-3 pb-1 pt-2 text-xs text-muted-weak">对话</p>
                {plainItems.map((item) => row(item, false))}
                <LoadMore
                  compact
                  hasMore={plainItems.length < plainTotal}
                  loading={loadingKind.plain}
                  loaded={plainItems.length}
                  total={plainTotal}
                  onLoad={() => { void fetchGroup('plain', plainItems.length, false); }}
                />
              </>
            )}
            {workspaceTotal + plainTotal === 0 && <p className="px-3 py-2 text-xs text-muted-weak">暂无对话</p>}
          </div>
        </div>

        {/* 底部：展开态是头像 + 用户名 + 退出按钮；折叠态只留头像，退出登录收进头像菜单 */}
        <div className="border-t border-line p-3 sidebar-collapsed:md:p-2">
          <div className="flex items-center gap-2 rounded-xl px-2 py-2 sidebar-collapsed:md:justify-center sidebar-collapsed:md:px-0">
            <Dropdown
              trigger={['click']}
              placement="topRight"
              menu={{
                items: [
                  { key: 'user', label: user?.username, disabled: true },
                  { type: 'divider' },
                  { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', onClick: confirmLogout },
                ],
              }}
            >
              <button type="button" title="账号" aria-label="账号菜单" className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-[#4d6bfe] text-sm text-white transition hover:brightness-110">
                {user?.username.slice(0, 1).toUpperCase()}
              </button>
            </Dropdown>
            <span className="min-w-0 flex-1 truncate whitespace-nowrap text-sm sidebar-collapsed:md:hidden">{user?.username}</span>
            <button title="退出登录" onClick={confirmLogout} className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-muted hover:bg-hover sidebar-collapsed:md:hidden">
              <LogoutOutlined />
            </button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}
