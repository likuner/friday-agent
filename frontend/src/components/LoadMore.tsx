'use client';

import { useEffect, useRef } from 'react';
import { LoadingOutlined } from '@ant-design/icons';

// 「加载下一页」的统一交互（侧边栏两组 / 历史记录页共用）：
// - 滚到底自动加载：IntersectionObserver 观察底部哨兵，提前 160px 触发；
// - 按钮兜底：手机或观察器未触发时可手点，触控高度 ≥32px；
// - loading 由 true 回到 false 会重建 observer，新一页仍在视口内就继续自动加载；
// - hasMore=false 显示「已全部加载」收尾，showEnd=false 时该收尾文案不输出（如侧边栏工作区列表）。
export default function LoadMore({
  hasMore, loading, loaded, total, onLoad, compact = false, showEnd = true,
}: {
  hasMore: boolean;
  loading: boolean;
  loaded: number;
  total: number;
  onLoad: () => void;
  compact?: boolean;
  showEnd?: boolean;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  const handler = useRef(onLoad);
  useEffect(() => { handler.current = onLoad; }, [onLoad]);

  useEffect(() => {
    const element = sentinel.current;
    // 老浏览器（iOS < 12.2 等）没有 IntersectionObserver：不自动加载，按钮仍可点
    if (!element || !hasMore || loading || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => { if (entries.some((entry) => entry.isIntersecting)) handler.current(); },
      { rootMargin: '160px' },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasMore, loading]);

  if (!hasMore) {
    return loaded > 0 && showEnd ? (
      <p className={`text-center text-xs text-muted-weak ${compact ? 'py-2' : 'py-5'}`}>已全部加载</p>
    ) : null;
  }

  const remaining = Math.max(total - loaded, 0);
  return (
    <div ref={sentinel} className={compact ? 'pt-1' : 'pt-2'}>
      <button
        type="button"
        onClick={onLoad}
        disabled={loading}
        className={
          compact
            ? 'flex h-8 w-full items-center justify-center gap-1.5 rounded-lg text-xs text-muted-weak transition hover:bg-hover hover:text-brand-text disabled:opacity-60'
            : 'flex h-10 w-full items-center justify-center gap-2 rounded-xl border border-line text-sm text-muted transition hover:border-line-hover hover:text-brand-text disabled:opacity-60'
        }
      >
        {loading ? (<><LoadingOutlined spin /> 加载中…</>) : (<>加载更多{remaining > 0 ? `（还有 ${remaining} 条）` : ''}</>)}
      </button>
    </div>
  );
}
