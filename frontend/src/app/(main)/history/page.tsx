'use client';

import { useEffect, useRef, useState } from 'react';
import { App, Button, Empty, Input, Spin, Tag } from 'antd';
import { DeleteOutlined, EditOutlined, FolderOpenOutlined, PushpinOutlined, SearchOutlined } from '@ant-design/icons';
import { conversations, deleteConversation, renameConversation, type Conversation } from '@/lib/api';
import { shortDir } from '@/lib/path';
import LoadMore from '@/components/LoadMore';
import ThemeToggle from '@/components/ThemeToggle';
import { useAuth } from '@/store/auth';
import { useUI } from '@/store/ui';

// 每页 20 条；搜索走服务端（q 参数），输入停止 300ms 后才发请求
const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;

function HistoryContent() {
  const { message, modal } = App.useApp();
  const token = useAuth((state) => state.token);
  // 改名/删除后通知侧栏重拉：Shell 已不再随路由切换重拉（避免点击闪动），同步改由版本号驱动
  const bumpConversations = useUI((state) => state.bumpConversations);
  const [items, setItems] = useState<Conversation[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState('');      // 输入框即时值
  const [keyword, setKeyword] = useState('');  // 防抖后的查询词（真正发给后端的 q）
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // 请求序号：搜索词变化后旧请求的结果一律丢弃，避免慢响应覆盖新结果
  const seq = useRef(0);

  useEffect(() => {
    const timer = setTimeout(() => setKeyword(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  // 关键词变化（含首次进入）：回到第 0 页重拉
  useEffect(() => {
    if (!token) return;
    const current = ++seq.current;
    setLoading(true);
    conversations(token, { q: keyword || undefined, limit: PAGE_SIZE, offset: 0 })
      .then((page) => { if (current !== seq.current) return; setItems(page.items); setTotal(page.total); })
      .catch((error) => { if (current === seq.current) message.error(error instanceof Error ? error.message : '加载失败'); })
      .finally(() => { if (current === seq.current) setLoading(false); });
  }, [token, keyword]);

  const loadMore = async () => {
    if (!token || loading || loadingMore) return;
    const current = seq.current;
    setLoadingMore(true);
    try {
      const page = await conversations(token, { q: keyword || undefined, limit: PAGE_SIZE, offset: items.length });
      if (current !== seq.current) return; // 期间换了搜索词：丢弃这一页
      setItems((old) => {
        const seen = new Set(old.map((row) => row.id));
        return [...old, ...page.items.filter((row) => !seen.has(row.id))];
      });
      setTotal(page.total);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoadingMore(false);
    }
  };

  const rename = (item: Conversation) => { let value = item.title; modal.confirm({ title: '重命名对话', content: <Input defaultValue={value} onChange={(event) => { value = event.target.value; }} />, onOk: async () => { if (!token) return; const updated = await renameConversation(token, item.id, value); setItems((old) => old.map((row) => row.id === item.id ? { ...row, ...updated } : row)); bumpConversations(); } }); };
  const remove = (item: Conversation) => modal.confirm({ title: '删除这个对话？', okButtonProps: { danger: true }, onOk: async () => { if (!token) return; await deleteConversation(token, item.id); setItems((old) => old.filter((row) => row.id !== item.id)); setTotal((count) => Math.max(count - 1, 0)); bumpConversations(); message.success('已删除'); } });

  return (
    <main className="flex h-full min-w-0 flex-col">
      <header className="flex h-[60px] shrink-0 items-center justify-between border-b border-line px-5">
        <h1 className="text-[15px] font-medium">
          历史记录 <span className="ml-2 rounded-full bg-hover px-2 py-1 text-xs text-muted">{keyword ? `搜索到 ${total} 个` : `${total} 个对话`}</span>
        </h1>
        <ThemeToggle />
      </header>
      <div className="scroll-hide min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[880px] px-5 py-6">
          <Input
            prefix={<SearchOutlined />}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索对话标题"
            size="large"
            allowClear
            className="mb-5 rounded-xl"
          />
          {loading ? (
            <div className="py-16 text-center"><Spin /></div>
          ) : items.length ? (
            <>
              <div className="space-y-3">
                {items.map((item) => (
                  <div
                    key={item.id}
                    className="group flex items-center gap-4 rounded-2xl border border-line px-4 py-4 transition hover:border-line-hover hover:shadow-[0_16px_44px_-30px_rgba(77,107,254,.85)]"
                  >
                    <a href={`/chat/${item.id}`} className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="min-w-0 truncate font-medium">{item.title}</span>
                        {/* 工作区会话标志：目录名（窄屏只留文件夹图标），悬停给全路径 */}
                        {item.workspace_root && (
                          <Tag icon={<FolderOpenOutlined className="text-brand-text" />} className="shrink-0" title={item.workspace_root}>
                            <span className="hidden max-w-[160px] truncate align-bottom sm:inline-block">{shortDir(item.workspace_root)}</span>
                          </Tag>
                        )}
                        <Tag color="blue" className="shrink-0">{item.message_count} 条消息</Tag>
                      </div>
                      <p className="mt-1 truncate text-sm text-muted">{item.last_message || '还没有消息'}</p>
                    </a>
                    <div className="flex gap-1 opacity-100 transition md:opacity-0 md:group-hover:opacity-100">
                      <Button type="text" icon={<PushpinOutlined />} />
                      <Button type="text" icon={<EditOutlined />} onClick={() => rename(item)} />
                      <Button danger type="text" icon={<DeleteOutlined />} onClick={() => remove(item)} />
                    </div>
                  </div>
                ))}
              </div>
              {/* 滚到底自动加载下一页 + 按钮兜底；拉完显示「已全部加载」 */}
              <LoadMore
                hasMore={items.length < total}
                loading={loadingMore}
                loaded={items.length}
                total={total}
                onLoad={() => { void loadMore(); }}
              />
            </>
          ) : (
            <Empty description={keyword ? '没有匹配的对话' : '还没有对话'} className="py-20" />
          )}
        </div>
      </div>
    </main>
  );
}
export default function HistoryPage() { return <HistoryContent />; }
