'use client';

// antd v5 静态方法（message/Modal.confirm 等）在 React 19 下需要此补丁。
// 必须放在客户端组件里导入：layout.tsx 是 Server Component，从 RSC 对
// 'use client' 包做副作用导入不会在浏览器执行。
import '@ant-design/v5-patch-for-react-19';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { App, ConfigProvider, theme as antdTheme } from 'antd';

export type Theme = 'light' | 'dark';

export const THEME_COOKIE = 'friday_theme';

const ThemeContext = createContext<{ theme: Theme; toggle: () => void }>({ theme: 'light', toggle: () => {} });

/** 供主题切换按钮使用（图标显示由 CSS 的 dark: 变体决定，不依赖 React 状态）。 */
export const useThemeToggle = () => useContext(ThemeContext);

function persist(theme: Theme) {
  document.documentElement.setAttribute('data-theme', theme);
  // cookie 而非 localStorage：服务端 layout 能读到，SSR 直接输出正确的 antd 主题，
  // 避免刷新时 antd 组件先渲染成浅色再切深色。
  document.cookie = `${THEME_COOKIE}=${theme}; path=/; max-age=31536000; SameSite=Lax`;
}

export default function Providers({ children, initialTheme = 'light' }: Readonly<{ children: React.ReactNode; initialTheme?: Theme }>) {
  // 初值来自服务端读到的 cookie，保证 SSR 与首帧客户端渲染完全一致
  const [theme, setTheme] = useState<Theme>(initialTheme);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark';
      persist(next);
      return next;
    });
  }, []);

  // 从未选择过主题时跟随系统偏好（只有首次访问会有一次切换，之后 cookie 生效）
  useEffect(() => {
    if (document.cookie.includes(`${THEME_COOKIE}=`)) return;
    if (!window.matchMedia('(prefers-color-scheme: dark)').matches) return;
    persist('dark');
    setTheme('dark');
  }, []);

  const value = useMemo(() => ({ theme, toggle }), [theme, toggle]);

  return (
    <ThemeContext.Provider value={value}>
      <ConfigProvider theme={{ algorithm: theme === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm }}>
        {/* 用 App 提供 message/modal/notification 的上下文实例（供 App.useApp() 使用），
            这样它们能读到主题、也遵循统一的动态主题；component={false} 不额外插入 DOM。 */}
        <App component={false}>{children}</App>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
