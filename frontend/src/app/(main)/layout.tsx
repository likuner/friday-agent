import Shell from '@/components/Shell';

// 侧边栏只在 (main) 路由组挂载一次：/chat、/chat/[id]、/history 之间切换时
// Shell 不会重新挂载，因此折叠状态、对话列表不会重置或重新拉取（避免点击闪动）。
export default function MainLayout({ children }: { children: React.ReactNode }) {
  return <Shell>{children}</Shell>;
}
