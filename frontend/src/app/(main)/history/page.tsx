'use client';

import { useEffect, useMemo, useState } from 'react';
import { App, Button, Empty, Input, Tag } from 'antd';
import { DeleteOutlined, EditOutlined, PushpinOutlined, SearchOutlined } from '@ant-design/icons';
import { conversations, deleteConversation, renameConversation, type Conversation } from '@/lib/api';
import ThemeToggle from '@/components/ThemeToggle';
import { useAuth } from '@/store/auth';

function HistoryContent() {
  const { message, modal } = App.useApp();
  const token = useAuth((state) => state.token);
  const [items, setItems] = useState<Conversation[]>([]);
  const [query, setQuery] = useState('');
  useEffect(() => { if (token) conversations(token).then(setItems); }, [token]);
  const filtered = useMemo(() => items.filter((item) => `${item.title} ${item.last_message || ''}`.toLowerCase().includes(query.toLowerCase())), [items, query]);
  const rename = (item: Conversation) => { let value = item.title; modal.confirm({ title: '重命名对话', content: <Input defaultValue={value} onChange={(event) => { value = event.target.value; }} />, onOk: async () => { if (!token) return; const updated = await renameConversation(token, item.id, value); setItems((old) => old.map((row) => row.id === item.id ? { ...row, ...updated } : row)); } }); };
  const remove = (item: Conversation) => modal.confirm({ title: '删除这个对话？', okButtonProps: { danger: true }, onOk: async () => { if (!token) return; await deleteConversation(token, item.id); setItems((old) => old.filter((row) => row.id !== item.id)); message.success('已删除'); } });
  return <main className="flex h-full min-w-0 flex-col"><header className="flex h-[60px] shrink-0 items-center justify-between border-b border-line px-5"><h1 className="text-[15px] font-medium">历史记录 <span className="ml-2 rounded-full bg-hover px-2 py-1 text-xs text-muted">{items.length} 个对话</span></h1><ThemeToggle /></header><div className="scroll-hide min-h-0 flex-1 overflow-y-auto"><div className="mx-auto max-w-[880px] px-5 py-6"><Input prefix={<SearchOutlined />} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索对话标题或内容关键词" size="large" className="mb-5 rounded-xl" />{filtered.length ? <div className="space-y-3">{filtered.map((item) => <div key={item.id} className="group flex items-center gap-4 rounded-2xl border border-line px-4 py-4 transition hover:border-line-hover hover:shadow-[0_16px_44px_-30px_rgba(77,107,254,.85)]"><a href={`/chat/${item.id}`} className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="truncate font-medium">{item.title}</span><Tag color="blue">{item.message_count} 条消息</Tag></div><p className="mt-1 truncate text-sm text-muted">{item.last_message || '还没有消息'}</p></a><div className="flex gap-1 opacity-100 transition md:opacity-0 md:group-hover:opacity-100"><Button type="text" icon={<PushpinOutlined />} /><Button type="text" icon={<EditOutlined />} onClick={() => rename(item)} /><Button danger type="text" icon={<DeleteOutlined />} onClick={() => remove(item)} /></div></div>)}</div> : <Empty description="没有匹配的对话" className="py-20" />}</div></div></main>;
}
export default function HistoryPage() { return <HistoryContent />; }
