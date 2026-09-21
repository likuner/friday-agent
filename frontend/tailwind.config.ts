import type { Config } from 'tailwindcss';
import plugin from 'tailwindcss/plugin';

// 颜色统一映射到 globals.css 里的语义 token（R G B 通道值），
// 这样 bg-surface/75 这类透明度修饰符依然可用，切主题只改 CSS 变量。
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      colors: {
        canvas: token('canvas'),
        panel: token('panel'),
        'panel-to': token('panel-to'),
        surface: token('surface'),
        soft: token('soft'),
        hover: token('hover'),
        'brand-soft': token('brand-soft'),

        line: token('line'),
        'line-strong': token('line-strong'),
        'line-brand': token('line-brand'),
        'line-hover': token('line-hover'),
        'line-focus': token('line-focus'),
        'accent-line': token('accent-line'),

        ink: token('ink'),
        'ink-soft': token('ink-soft'),
        body: token('body'),
        muted: token('muted'),
        'muted-weak': token('muted-weak'),
        weak: token('weak'),
        'quote-text': token('quote-text'),

        brand: token('brand'),
        'brand-text': token('brand-text'),

        'quote-bg': token('quote-bg'),
        'table-head': token('table-head'),
        'code-bg': token('code-bg'),
        marker: token('marker'),
        'scroll-thumb': token('scroll-thumb'),
        'scroll-thumb-hover': token('scroll-thumb-hover'),
        overlay: token('overlay'),
      },
    },
  },
  plugins: [
    plugin(({ addVariant }) => {
      // 侧边栏折叠状态由 <html data-sidebar="collapsed"> 驱动（首帧前由内联脚本写好），
      // 这样服务端与客户端渲染出的 class 完全一致：既不产生水合不一致，也没有刷新闪动。
      addVariant('sidebar-collapsed', 'html[data-sidebar="collapsed"] &');
    }),
  ],
};
export default config;
