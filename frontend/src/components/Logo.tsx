'use client';

import { useId } from 'react';

// 全站统一品牌标识：圆角方形徽标 + 白色「F」，顶臂末端接一颗闪光。
// 纯内联 SVG：任意尺寸都清晰、不依赖字体与图标库，深浅色主题共用同一份，
// 侧栏（32px）、空状态（56px）、消息头像（20px）都渲染这个组件，改一处即全站生效。
// 尺寸交给 className（h-8 w-8 之类）；发光用 drop-shadow 而不是 box-shadow——
// 投影跟着徽标的圆角轮廓走，不会在四角露出方块直角。
export default function Logo({ className = 'h-8 w-8' }: { className?: string }) {
  // 页面上会同时渲染多个 Logo，用 useId 避免 <defs> 里的渐变 id 重复
  const gradient = `friday-logo-${useId().replace(/:/g, '')}`;
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#5b78ff" />
          <stop offset="1" stopColor="#8aa2ff" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9.5" fill={`url(#${gradient})`} />
      {/* F：竖笔 + 中横 + 顶横；顶横右端接一颗同色闪光，两横之间留 3.1 间隙保证小尺寸也能认出是 F */}
      <rect x="10.2" y="7.5" width="3.8" height="17.5" rx="1.9" fill="#fff" />
      <rect x="10.2" y="14.4" width="6.4" height="3.8" rx="1.9" fill="#fff" />
      <rect x="10.2" y="7.5" width="8.6" height="3.8" rx="1.9" fill="#fff" />
      <path
        d="M23.2 5.5c.408 2.608 2.608 2.608 4.4 4.4-2.608.408-2.608 2.608-4.4 4.4-.408-2.608-2.608-2.608-4.4-4.4 2.608-.408 2.608-2.608 4.4-4.4Z"
        fill="#fff"
      />
    </svg>
  );
}
