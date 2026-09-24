import { create } from 'zustand';

type UIState = {
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  // 会话列表版本号：懒创建（replaceState 不触发路由变化）、工作区/权限变更后 bump，
  // Shell 依赖它重拉列表——否则新会话不出现在侧边栏、分组不刷新
  conversationsVersion: number;
  bumpConversations: () => void;
};

export const useUI = create<UIState>((set) => ({
  sidebarOpen: false,
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
  conversationsVersion: 0,
  bumpConversations: () => set((state) => ({ conversationsVersion: state.conversationsVersion + 1 })),
}));
