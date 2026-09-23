'use client';

import { MoonOutlined, SunOutlined } from '@ant-design/icons';
import { useThemeToggle } from '@/app/providers';

// 主题状态放在 Providers（初值由服务端 cookie 决定）；这里只需要切换动作。
// 图标用 CSS 的 dark: 变体控制，首帧即正确，不依赖水合。
export default function ThemeToggle({ className = '' }: { className?: string }) {
  const { toggle } = useThemeToggle();
  return (
    <button
      type="button"
      onClick={toggle}
      title="切换深浅色主题"
      aria-label="切换深浅色主题"
      className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-hover hover:text-ink ${className}`}
    >
      {/* 必须用 span 包一层：直接给 antd 图标加 hidden 会被 .anticon{display:inline-flex} 覆盖（同优先级但 antd 样式后注入） */}
      <span className="grid place-items-center dark:hidden">
        <MoonOutlined />
      </span>
      <span className="hidden place-items-center dark:grid">
        <SunOutlined />
      </span>
    </button>
  );
}
