'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowDownOutlined, CloseOutlined, CopyOutlined, DownOutlined, EditOutlined, GlobalOutlined, LikeFilled, LikeOutlined, PaperClipOutlined, PictureOutlined, SendOutlined, BulbOutlined, LoadingOutlined, MenuOutlined, SearchOutlined, UpOutlined } from '@ant-design/icons';
import { App, Button, Image as AntdImage } from 'antd';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { conversation, createConversation, fileUrl, streamMessage, truncateMessages, uploadImage, type Message, type ToolCall, type UploadedFile } from '@/lib/api';
import ThemeToggle from '@/components/ThemeToggle';
import { useAuth } from '@/store/auth';
import { useUI } from '@/store/ui';

// 「深度思考」「联网搜索」开关控制：
// 深度思考：开启后改用支持 reasoning 的模型，流式展示思考过程，回答结束后可折叠
const SHOW_DEEP_THINKING = true;
// 联网搜索：开启后后端注册 web_search 工具，模型 tool_call 转交 GLM 联网检索执行
const SHOW_WEB_SEARCH = true;

// 距底部小于该像素即视为「贴着底部」，此时流式内容会自动跟随
const STICK_THRESHOLD = 48;

// 单条消息最多可带几张图片（与后端 ChatRequest.attachments 的上限保持一致）
const MAX_ATTACHMENTS = 9;

// 展示本轮的检索记录：一次回答可能并行检索多个角度，逐个显示实际检索词，避免看起来像重复 chip
// Markdown 渲染定制：引用来源等外链一律新标签页打开，避免把当前对话导航走
const markdownComponents: Components = {
  a: ({ children, href }) => {
    const external = /^https?:\/\//i.test(href || '');
    return <a href={href} {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>{children}</a>;
  },
};

// 消息里的图片附件
// 单张图片：文件缺失/被删时降级成占位块，避免浏览器显示裂图
function MessageImage({ name }: { name: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="flex h-[120px] w-[160px] flex-col items-center justify-center gap-1 rounded-xl border border-line bg-soft text-[11px] text-muted">
        <PictureOutlined />
        <span>图片已失效</span>
      </div>
    );
  }
  // antd Image 自带预览（点击放大 / 缩放 / 旋转 / 下载），且跟随主题
  return (
    <AntdImage
      src={fileUrl(name)}
      alt="图片附件"
      onError={() => setFailed(true)}
      width={160}
      height={120}
      style={{ objectFit: 'cover' }}
      className="rounded-xl"
      wrapperClassName="overflow-hidden rounded-xl"
    />
  );
}

function MessageImages({ names }: { names: string[] }) {
  if (!names.length) return null;
  return (
    <div className="flex flex-wrap justify-end gap-2">
      {names.map((name) => <MessageImage key={name} name={name} />)}
    </div>
  );
}

// 用户消息：图片单独展示在气泡之外，只有文字进蓝色气泡；气泡下方提供复制与编辑重发
function UserMessage({ item, busy, onCopy, onResend }: { item: Message; busy: boolean; onCopy: (text: string) => void; onResend: (item: Message, content: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.content);
  const names = (item.meta?.attachments as string[]) || [];
  if (!names.length && !item.content) return null;

  const cancel = () => { setEditing(false); setDraft(item.content); };
  const submit = () => {
    if (!draft.trim()) return;
    setEditing(false);
    onResend(item, draft);
  };

  return (
    <div className="flex max-w-[85%] flex-col items-end gap-2">
      <MessageImages names={names} />
      {editing ? (
        <div className="w-full rounded-2xl border border-line-focus bg-surface p-3 shadow-[0_12px_28px_-18px_rgba(77,107,254,.6)]">
          <div className="mb-1 text-xs font-medium text-[#dbe4ff]">你</div>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') cancel();
              if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); }
            }}
            rows={Math.min(8, Math.max(2, draft.split('\n').length))}
            autoFocus
            className="block w-full resize-none border-0 bg-transparent py-1 text-[14.5px] leading-6 outline-none"
          />
          <div className="mt-1 flex items-center justify-end gap-1 text-xs">
            <button type="button" onClick={cancel} className="rounded px-2 py-1 text-muted transition-colors hover:bg-hover">取消</button>
            <button type="button" onClick={submit} disabled={!draft.trim()} title="保存后会删除这条之后的消息并重新发送" className="rounded px-2 py-1 font-medium text-brand transition-colors hover:bg-hover disabled:opacity-50">保存并重发</button>
          </div>
        </div>
      ) : (
        <>
          {item.content && (
            <div className="rounded-2xl rounded-tr-md bg-gradient-to-br from-[#4d6bfe] to-[#7288ff] px-4 py-3 text-white shadow-[0_12px_28px_-14px_rgba(77,107,254,.75)]">
              <div className="mb-1 text-xs font-medium text-[#dbe4ff]">你</div>
              <div className="whitespace-pre-wrap text-[14.5px] leading-7">{item.content}</div>
            </div>
          )}
          <div className="flex items-center gap-1 text-xs text-muted-weak">
            {item.content && (
              <button onClick={() => onCopy(item.content)} className="rounded px-2 py-1 transition-colors hover:bg-hover"><CopyOutlined /> 复制</button>
            )}
            <button onClick={() => { setDraft(item.content); setEditing(true); }} disabled={busy} title={busy ? '生成中暂不能编辑' : '编辑这条提问并重新发送（该条之后的消息会被删除）'} className="rounded px-2 py-1 transition-colors hover:bg-hover disabled:opacity-50"><EditOutlined /> 编辑重发</button>
          </div>
        </>
      )}
    </div>
  );
}

// 思考过程面板：流式期间展开，回答结束后自动收起，可手动展开/折叠
function ThinkingPanel({ text, streaming, open, onToggle }: { text: string; streaming: boolean; open: boolean; onToggle: () => void }) {
  if (!text) return null;
  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-line bg-soft">
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs text-muted transition-colors hover:bg-hover">
        <BulbOutlined />
        <span>{streaming ? '思考中…' : '思考过程'}</span>
        <span className="ml-auto text-[11px]">{open ? '收起' : '展开'}</span>
        {open ? <UpOutlined className="text-[10px]" /> : <DownOutlined className="text-[10px]" />}
      </button>
      {open && <div className="scroll-hide max-h-[320px] overflow-y-auto whitespace-pre-wrap border-t border-line px-3 py-2 text-[13px] leading-6 text-body">{text}</div>}
    </div>
  );
}

// 流式期间的状态提示：无正文时的占位（正在思考/正在检索），或调用工具期间显示在已有正文下方
function StreamingHint({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[14.5px] text-muted">
      <LoadingOutlined spin className="text-[#4d6bfe]" /> {label}<span className="thinking-dot">…</span>
    </div>
  );
}

function ToolCallChips({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-1.5">
      {calls.map((call, index) => {
        // web_search = GLM 联网检索；其余（medical_rag_search）= 本地文献库检索
        const isWeb = call.name === 'web_search';
        const label = isWeb ? `联网搜索：${call.query || '网络'}` : `已检索：${call.query || '医学文献库'}`;
        return (
          <span key={index} title={call.query || call.name} className="inline-flex max-w-[320px] items-center gap-1 rounded-full border border-line-brand bg-brand-soft px-2.5 py-1 text-[11px] text-brand-text">
            {isWeb ? <GlobalOutlined /> : <SearchOutlined />}
            <span className="truncate">{label}</span>
          </span>
        );
      })}
    </div>
  );
}

export default function ChatWorkspace({ conversationId }: { conversationId?: string }) {
  const { message } = App.useApp();
  const token = useAuth((state) => state.token);
  const setSidebarOpen = useUI((state) => state.setSidebarOpen);
  const [id, setId] = useState(conversationId);
  const [title, setTitle] = useState('新的对话');
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const [searching, setSearching] = useState(false);
  const [loading, setLoading] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const streamBuffer = useRef('');
  const thinkingBuffer = useRef('');
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // 正在流式输出的 assistant 消息 id：用于判断思考面板该展开还是收起
  const [streamingId, setStreamingId] = useState<string | null>(null);
  // 模型正在调用工具（tool_call 事件后、正文恢复流式前）：期间正文下方持续显示「正在检索…」
  const [toolRunning, setToolRunning] = useState(false);
  // 用户手动展开/收起思考过程的覆盖值
  const [thinkingOpen, setThinkingOpen] = useState<Record<string, boolean>>({});
  // 当前流式请求的中止控制器（「停止生成」按钮使用）
  const abortRef = useRef<AbortController | null>(null);
  // 待发送的图片附件（已上传到后端 files）
  const [attachments, setAttachments] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  // 「有帮助」点赞状态（仅前端会话内，暂不落库）
  const [liked, setLiked] = useState<Record<string, boolean>>({});
  // 是否跟随底部：用户主动上滑阅读时置 false，滚回底部附近自动恢复
  const stick = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  const jumpToBottom = () => {
    stick.current = true;
    setAtBottom(true);
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' });
  };

  // 用户上滑后不再被流式输出拉回底部；回到底部附近则恢复自动跟随
  const handleScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const following = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD;
    stick.current = following;
    setAtBottom(following);
  };

  useEffect(() => () => { if (flushTimer.current) clearInterval(flushTimer.current); }, []);

  useEffect(() => { stick.current = true; setAtBottom(true); if (token && conversationId) conversation(token, conversationId).then((data) => { setId(data.id); setTitle(data.title); setMessages(data.messages); }); else { setId(undefined); setTitle('新的对话'); setMessages([]); } }, [token, conversationId]);
  // 只有用户本来贴着底部才自动跟随：流式输出期间上滑阅读不会被拉回底部
  useEffect(() => { const el = scroller.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [messages]);

  const stopGenerating = () => {
    abortRef.current?.abort();
  };

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      message.success('复制成功');
    } catch {
      message.error('复制失败，请手动选择复制');
    }
  };

  const toggleLike = (id: string) => {
    const next = !liked[id];
    setLiked((old) => ({ ...old, [id]: next }));
    message.success(next ? '已点赞，感谢反馈' : '已取消点赞');
  };

  const pickImage = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(event.target.files || []);
    event.target.value = ''; // 清空以便连续选择同一批文件
    if (!picked.length || !token) return;

    const room = MAX_ATTACHMENTS - attachments.length;
    if (room <= 0) {
      message.warning(`最多只能添加 ${MAX_ATTACHMENTS} 张图片`);
      return;
    }
    const accepted = picked.slice(0, room);
    if (picked.length > room) message.warning(`最多 ${MAX_ATTACHMENTS} 张，已只添加前 ${room} 张`);

    const images = accepted.filter((file) => file.type.startsWith('image/'));
    if (images.length < accepted.length) message.warning('已跳过非图片文件');
    if (!images.length) return;

    // 多张并发上传，单张失败不影响其它
    setUploading(true);
    const results = await Promise.allSettled(images.map((file) => uploadImage(token, file)));
    const uploaded = results.filter((r) => r.status === 'fulfilled').map((r) => r.value);
    const failed = results.length - uploaded.length;
    if (uploaded.length) setAttachments((old) => [...old, ...uploaded]);
    if (uploaded.length > 1) message.success(`${uploaded.length} 张图片上传成功`);
    else if (uploaded.length === 1) message.success('图片上传成功');
    if (failed) message.error(`${failed} 张图片上传失败`);
    setUploading(false);
  };

  // override：编辑重发时直接给定内容与附件名，不读取（也不清空）输入框里的草稿
  const send = async (override?: { content: string; attachments: string[] }) => {
    const content = (override?.content ?? input).trim();
    const sentAttachments = override ? override.attachments : attachments.map((item) => item.name);
    if ((!content && !sentAttachments.length) || loading || !token) return;
    setLoading(true);
    if (!override) { setInput(''); setAttachments([]); }
    stick.current = true; setAtBottom(true);
    let target = id;
    if (!target) { const created = await createConversation(token, content.slice(0, 30)); target = created.id; setId(target); setTitle(created.title); window.history.replaceState(null, '', `/chat/${target}`); }
    const userMessage: Message = { id: crypto.randomUUID(), role: 'user', content, meta: { attachments: sentAttachments }, created_at: new Date().toISOString() };
    const assistantId = crypto.randomUUID();
    setMessages((old) => [...old, userMessage, { id: assistantId, role: 'assistant', content: '', meta: {}, created_at: new Date().toISOString() }]);
    streamBuffer.current = '';
    thinkingBuffer.current = '';
    setStreamingId(assistantId);
    setToolRunning(false);
    // 打字机节奏放缓冲区：每个 token 都 setState 会导致整列表+Markdown 高频重渲染。
    // flusher 必须随发送立即启动，否则流式期间界面会一直停在"正在思考…"。
    const streamDone = { current: false };
    let finishFlush: () => void = () => {};
    const flushed = new Promise<void>((resolve) => { finishFlush = resolve; });
    flushTimer.current = setInterval(() => {
      const buffered = streamBuffer.current;
      const pendingThinking = thinkingBuffer.current;
      // 思考与正文两个缓冲都排空，且上游结束，才算这一次流式真正收尾
      if (!buffered && !pendingThinking) {
        if (streamDone.current) { if (flushTimer.current) clearInterval(flushTimer.current); flushTimer.current = null; finishFlush(); }
        return;
      }
      if (pendingThinking) {
        const take = Math.min(pendingThinking.length, Math.max(4, Math.ceil(pendingThinking.length / 8)));
        thinkingBuffer.current = pendingThinking.slice(take);
        const reveal = pendingThinking.slice(0, take);
        setMessages((old) => old.map((item) => item.id === assistantId ? { ...item, meta: { ...item.meta, thinking: `${(item.meta?.thinking as string) || ''}${reveal}` } } : item));
      }
      if (buffered) {
        const take = Math.min(buffered.length, Math.max(2, Math.ceil(buffered.length / 8)));
        streamBuffer.current = buffered.slice(take);
        const reveal = buffered.slice(0, take);
        setMessages((old) => old.map((item) => item.id === assistantId ? { ...item, content: item.content + reveal } : item));
      }
    }, 50);
    const controller = new AbortController();
    abortRef.current = controller;
    try { await streamMessage(token, target, { content, deep_thinking: thinking, web_search: searching, attachments: sentAttachments }, (event) => {
      // 流首 sent 事件：把乐观渲染的本地 id 替换成库中真实 id，编辑重发的截断才能按 id 定位
      if (event.type === 'sent' && event.message_id) { setMessages((old) => old.map((row) => row.id === userMessage.id ? { ...row, id: event.message_id! } : row)); return; }
      if (event.type === 'thinking') thinkingBuffer.current += event.content || ''; else if (event.type === 'text') { setToolRunning(false); streamBuffer.current += event.content || ''; } else if (event.type === 'tool_call') { setToolRunning(true); setMessages((old) => old.map((row) => row.id === assistantId ? { ...row, meta: { ...row.meta, toolCalls: [...((row.meta?.toolCalls as ToolCall[]) || []), { name: event.name || 'medical_rag_search', query: event.query }] } } : row)); } }, controller.signal); } catch (error) {
      // 用户主动中止不算错误：保留已生成的部分并标记
      if (error instanceof DOMException && error.name === 'AbortError') {
        setMessages((old) => old.map((item) => item.id === assistantId ? { ...item, meta: { ...item.meta, stopped: true } } : item));
        message.info('已停止生成');
      } else {
        message.error(error instanceof Error ? error.message : '发送失败');
      }
    } finally { abortRef.current = null; streamDone.current = true; await flushed; setLoading(false); setStreamingId(null); setToolRunning(false); }
  };

  // 编辑重发：把该条及其后的消息截断（前端 + 后端），再以编辑后的内容走正常发送链路
  const resendFrom = async (item: Message, content: string) => {
    if (loading || !token || !id) return;
    const index = messages.findIndex((row) => row.id === item.id);
    if (index < 0) return;
    try {
      await truncateMessages(token, id, item.id);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '截断历史消息失败，请稍后重试');
      return;
    }
    setMessages((old) => old.slice(0, index));
    await send({ content, attachments: (item.meta?.attachments as string[]) || [] });
  };

  return <div className="flex h-full min-w-0 flex-col bg-canvas"><header className="flex h-[60px] shrink-0 items-center justify-between border-b border-line/80 bg-surface/75 px-4 backdrop-blur-md sm:px-6"><div className="flex min-w-0 items-center gap-1.5"><button title="打开菜单" onClick={() => setSidebarOpen(true)} className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-body hover:bg-hover md:hidden"><MenuOutlined /></button><div className="min-w-0"><h1 className="truncate text-[15px] font-medium">{title}</h1><span className="text-xs text-muted-weak">{messages.length} 条消息</span></div></div><ThemeToggle /></header><div className="relative flex min-h-0 flex-1 flex-col"><div ref={scroller} onScroll={handleScroll} className="scroll-hide min-h-0 flex-1 overflow-y-auto"><div className="mx-auto w-full max-w-[800px] space-y-7 px-4 py-7 sm:px-6">{messages.length === 0 ? <div className="pt-14 text-center"><div className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-[#4d6bfe] text-2xl text-white"><RobotIcon /></div><h2 className="mt-5 text-2xl font-semibold">Hi，我是 Friday</h2><p className="mx-auto mt-2 max-w-[460px] text-[13.5px] leading-6 text-muted">我可以帮你写代码、读文件、写作、做方案，也能联网查最新资料。</p></div> : messages.map((item) => { if (item.role === 'user') return <div key={item.id} className="msg-in flex justify-end"><UserMessage item={item} busy={loading} onCopy={copyText} onResend={resendFrom} /></div>; const thinkingText = (item.meta?.thinking as string) || ''; const isStreamingThis = item.id === streamingId; const thinkingExpanded = thinkingOpen[item.id] ?? isStreamingThis; return <div key={item.id} className="msg-in flex gap-3"><div className="min-w-0 flex-1"><div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-muted"><span className="grid h-5 w-5 place-items-center rounded-md bg-gradient-to-br from-[#4d6bfe] to-[#7c94ff] text-[9px] text-white">F</span>Friday Agent</div><ToolCallChips calls={(item.meta?.toolCalls as ToolCall[]) || []} />{thinkingText && <ThinkingPanel text={thinkingText} streaming={isStreamingThis} open={thinkingExpanded} onToggle={() => setThinkingOpen((old) => ({ ...old, [item.id]: !thinkingExpanded }))} />}{item.content ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={markdownComponents}>{item.content}</ReactMarkdown>{isStreamingThis && toolRunning && <div className="mt-1"><StreamingHint label="正在检索" /></div>}<div className="mt-2 flex gap-1 text-xs text-muted-weak"><button onClick={() => copyText(item.content)} className="rounded px-2 py-1 transition-colors hover:bg-hover"><CopyOutlined /> 复制</button><button onClick={() => toggleLike(item.id)} className={`rounded px-2 py-1 transition-colors hover:bg-hover ${liked[item.id] ? 'text-brand' : ''}`}>{liked[item.id] ? <LikeFilled /> : <LikeOutlined />} 有帮助</button></div></div> : <StreamingHint label={isStreamingThis && toolRunning ? '正在检索' : '正在思考'} />}{item.meta?.stopped ? <div className="mt-2 text-xs text-muted-weak">已停止生成</div> : null}</div></div>; })}<div ref={bottom} /></div></div>{!atBottom && <button onClick={jumpToBottom} title="回到底部" aria-label="回到底部" className="absolute bottom-4 left-1/2 z-10 grid h-9 w-9 -translate-x-1/2 place-items-center rounded-full border border-line-strong bg-surface text-muted shadow-[0_10px_24px_-12px_rgba(20,35,80,.6)] transition-colors hover:bg-hover hover:text-ink"><ArrowDownOutlined /></button>}</div><div className="shrink-0 px-4 pb-5 pt-1 sm:px-6"><div className="mx-auto max-w-[800px] rounded-[24px] border border-line-strong bg-surface/95 px-4 pb-2.5 pt-3 shadow-[0_18px_44px_-24px_rgba(20,35,80,.35)] backdrop-blur transition-all duration-200 focus-within:border-line-focus focus-within:shadow-[0_18px_46px_-20px_rgba(77,107,254,.5)]">{attachments.length > 0 && <div className="mb-2 flex flex-wrap gap-2">{attachments.map((item) => <div key={item.name} className="relative"><img src={fileUrl(item.name)} alt="待发送图片" className="h-16 w-16 rounded-lg border border-line object-cover" /><button type="button" title="移除图片" onClick={() => setAttachments((old) => old.filter((row) => row.name !== item.name))} className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full border border-line bg-surface text-[10px] text-muted hover:text-ink"><CloseOutlined /></button></div>)}</div>}<textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }} rows={2} placeholder="给 Friday 发送消息…" className="block w-full resize-none border-0 bg-transparent py-1 text-[14.5px] leading-6 outline-none" /><div className="mt-2 flex items-center gap-2"><button title={`上传图片（最多 ${MAX_ATTACHMENTS} 张）`} onClick={() => fileInput.current?.click()} disabled={uploading} className="grid h-8 w-8 place-items-center rounded-lg text-weak hover:bg-hover disabled:opacity-50">{uploading ? <LoadingOutlined spin /> : <PaperClipOutlined />}</button><input ref={fileInput} type="file" accept="image/*" multiple className="hidden" onChange={pickImage} />{SHOW_DEEP_THINKING && <button onClick={() => setThinking(!thinking)} className={`rounded-full border px-3 py-1 text-xs ${thinking ? 'border-[#4d6bfe] bg-brand-soft text-brand-text' : 'border-line-strong text-weak'}`}><BulbOutlined /> 深度思考</button>}{SHOW_WEB_SEARCH && <button onClick={() => setSearching(!searching)} className={`rounded-full border px-3 py-1 text-xs ${searching ? 'border-[#4d6bfe] bg-brand-soft text-brand-text' : 'border-line-strong text-weak'}`}><GlobalOutlined /> 联网搜索</button>}{loading ? <button type="button" onClick={stopGenerating} title="停止生成" aria-label="停止生成" className="ml-auto grid h-8 w-8 shrink-0 place-items-center rounded-full border border-line-brand bg-brand-soft text-brand-text shadow-[0_6px_16px_-10px_rgba(77,107,254,.9)] transition-colors hover:border-line-hover hover:bg-hover hover:text-ink"><span className="block h-3 w-3 rounded-[3px] bg-current" /></button> : <Button type="primary" shape="circle" icon={<SendOutlined />} disabled={!input.trim() && !attachments.length} onClick={() => send()} className="ml-auto h-9 w-9 border-0 bg-gradient-to-br from-[#4d6bfe] to-[#7288ff] shadow-[0_8px_18px_-8px_rgba(77,107,254,.9)] transition-transform hover:scale-105" />}</div></div><p className="mt-2 text-center text-[11.5px] text-muted-weak">内容由 AI 生成，请仔细甄别</p></div></div>;
}
function RobotIcon() { return <span className="text-2xl">F</span>; }
