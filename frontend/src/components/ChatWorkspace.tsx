'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { DndContext, DragOverlay, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ArrowDownOutlined, ArrowUpOutlined, CheckCircleOutlined, CloseOutlined, CodeOutlined, CopyOutlined, DeleteOutlined, DownOutlined, EditOutlined, EyeOutlined, FileTextOutlined, FolderOpenOutlined, GlobalOutlined, HolderOutlined, LikeFilled, LikeOutlined, OrderedListOutlined, PaperClipOutlined, PictureOutlined, SafetyOutlined, SendOutlined, BulbOutlined, LoadingOutlined, MenuOutlined, SearchOutlined, TeamOutlined, UpOutlined } from '@ant-design/icons';
import { App, Button, Dropdown, Image as AntdImage } from 'antd';
import { authImageUrl, conversation, confirmPermission, createConversation, setPermissionMode as savePermissionMode, streamMessage, truncateMessages, uploadImage, type AgentTrace, type Message, type PermissionAsk, type ToolCall, type UploadedFile } from '@/lib/api';
import Markdown from '@/components/Markdown';
import ThemeToggle from '@/components/ThemeToggle';
import Logo from '@/components/Logo';
import { useAuth } from '@/store/auth';
import { useUI } from '@/store/ui';

// 「深度思考」「联网搜索」「多 Agent」开关控制：
// 深度思考：开启后改用支持 reasoning 的模型，流式展示思考过程，回答结束后可折叠
const SHOW_DEEP_THINKING = true;
// 联网搜索：开启后后端注册 web_search 工具，模型 tool_call 转交 GLM 联网检索执行
const SHOW_WEB_SEARCH = true;
// 多 Agent：开启后后端组建团队（Friday 作 leader 规划派活，检索员/执行员执行），
// 工具 chip 与运行提示会标注归属 agent；关闭则整轮单 Agent（服务端 MULTI_AGENT_ENABLED 是总闸）
const SHOW_MULTI_AGENT = true;
// Agent 工具不再有入口开关：有工作区的会话（侧边栏「工作区」组标题 + 新建）自动开启，
// 工作区选定后不可更改；无工作区的普通会话不开启。

// AgentScope 内置工具名（区别于检索类工具，运行提示显示「正在执行」）
const BUILTIN_TOOL_NAMES = new Set(['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate']);

// 工具运行提示文案：内置工具（Bash/文件/任务）显示「正在执行」，检索类显示「正在检索」；
// 多 Agent 模式带来源前缀（「检索员正在检索…」「执行员正在执行…」，Friday 是主 Agent 不标）
function toolRunningLabel(tool: string, agent?: string) {
  const action = BUILTIN_TOOL_NAMES.has(tool) ? '执行' : '检索';
  const who = agent && agent !== 'Friday' ? agent : '';
  return `${who}正在${action}`;
}

// 权限模式选项（后端 ChatRequest/会话设置的取值域；BYPASS 不开放）。
// 仅工作区会话显示选择器；选择随会话持久化（chat 生效优先级：会话设置 > 请求参数 > default）。
const PERMISSION_MODE_OPTIONS = [
  { value: 'default', label: '每步确认', hint: '未授权操作弹确认卡片' },
  { value: 'accept_edits', label: '工作区免确认', hint: '工作区内写入自动放行，其余仍确认' },
  { value: 'explore', label: '只读', hint: '一切修改直接拒绝' },
  { value: 'dont_ask', label: '自动', hint: '未授权直接拒绝，不弹确认' },
] as const;

// 距底部小于该像素即视为「贴着底部」，此时流式内容会自动跟随
const STICK_THRESHOLD = 48;

// 单条消息最多可带几张图片（与后端 ChatRequest.attachments 的上限保持一致）
const MAX_ATTACHMENTS = 9;

// 排队消息上限：每条都会在当前回答结束后触发一轮完整生成，过多既展示不下也无意义
const MAX_QUEUE = 5;

// 回答生成中继续发送的消息：进入队列，当前回答结束后自动轮候发出
type QueuedMessage = { id: string; content: string; attachments: string[] };

// 聚焦并把光标移到文本末尾（autoFocus 默认落在开头）
const focusAtEnd = (el: HTMLTextAreaElement | null) => {
  if (!el) return;
  el.focus();
  el.setSelectionRange(el.value.length, el.value.length);
};

// 消息里的图片附件
// 单张图片：文件缺失/被删时降级成占位块，避免浏览器显示裂图
function MessageImage({ name }: { name: string }) {
  const token = useAuth((state) => state.token);
  const [src, setSrc] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setFailed(false);
    setSrc(undefined);
    if (!token) return;
    authImageUrl(token, name).then(
      (url) => { if (alive) setSrc(url); },
      () => { if (alive) setFailed(true); },
    );
    return () => { alive = false; };
  }, [token, name]);
  if (failed || (!src && !token)) {
    return (
      <div className="flex h-[120px] w-[160px] flex-col items-center justify-center gap-1 rounded-xl border border-line bg-soft text-[11px] text-muted">
        <PictureOutlined />
        <span>图片已失效</span>
      </div>
    );
  }
  if (!src) {
    return (
      <div className="flex h-[120px] w-[160px] items-center justify-center rounded-xl border border-line bg-soft text-muted">
        <LoadingOutlined />
      </div>
    );
  }
  // antd Image 自带预览（点击放大 / 缩放 / 旋转 / 下载），且跟随主题
  return (
    <AntdImage
      src={src}
      alt="图片附件"
      onError={() => setFailed(true)}
      width={160}
      height={120}
      style={{ objectFit: 'cover' }}
      className="rounded-xl"
      wrapperClassName="overflow-hidden rounded-xl"
      preview={{ mask: <EyeOutlined /> }}
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

// 待发送附件的缩略图：与消息内图片一样走鉴权取图（复用同一缓存）
function AttachmentThumb({ name }: { name: string }) {
  const token = useAuth((state) => state.token);
  const [src, setSrc] = useState<string>();
  useEffect(() => {
    let alive = true;
    if (!token) return;
    authImageUrl(token, name).then((url) => { if (alive) setSrc(url); }, () => {});
    return () => { alive = false; };
  }, [token, name]);
  return <img src={src} alt="待发送图片" className="h-16 w-16 rounded-lg border border-line bg-soft object-cover" />;
}

// 排队待发送的消息卡片：回答生成中继续发送的消息逐条轮候，
// 支持拖拽换序、「立即发送」打断当前回答优先发出，以及编辑与删除
function QueueCard({ item, onSendNow, onSave, onDelete }: {
  item: QueuedMessage;
  onSendNow: () => void;
  onSave: (content: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.content);
  // 拖拽排序（@dnd-kit）：手柄是唯一触发器；拖动中原卡片变虚线 ghost 占位，
  // 跟随指针的浮层在 DragOverlay 里渲染，其余卡片 transform 让位
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id: item.id });

  const cancel = () => { setEditing(false); setDraft(item.content); };
  const submit = () => {
    if (!draft.trim()) return;
    setEditing(false);
    onSave(draft);
  };

  return (
    <div
      ref={setNodeRef}
      style={{
        // 拖动中的卡片本体留在原位当占位 ghost（跟随指针的浮层由 DragOverlay 渲染）。
        // 不能给它 transform：transform 会撑大滚动容器的可滚动区，指针贴着底边时
        // auto-scroll 反复启停、碰撞目标来回翻转，兄弟卡片就会抖动
        transform: isDragging ? undefined : CSS.Transform.toString(transform),
        // 松手回位时 dnd-kit 不给 transition，兜底一个
        transition: isDragging ? undefined : (transition ?? 'transform 200ms ease'),
      }}
      className={`queue-card-in flex min-h-[52px] items-center gap-3 rounded-2xl border border-line bg-soft px-3.5 py-2 ${isDragging ? 'border-dashed opacity-40' : ''}`}
    >
      <button
        type="button"
        ref={setActivatorNodeRef}
        {...attributes}
        {...listeners}
        title="拖拽调整发送顺序"
        aria-label="拖拽调整发送顺序"
        className="shrink-0 cursor-grab touch-none select-none text-[16px] text-muted-weak transition-colors hover:text-body"
      >
        <HolderOutlined />
      </button>
      {editing ? (
        <div className="min-w-0 flex-1">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') cancel();
              if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); }
            }}
            rows={Math.min(8, Math.max(2, draft.split('\n').length))}
            ref={focusAtEnd}
            className="block w-full resize-none border-0 bg-transparent py-1 text-[13.5px] leading-6 outline-none"
          />
          <div className="flex items-center justify-end gap-1 text-xs">
            <button type="button" onClick={cancel} className="rounded px-2 py-1 text-muted transition-colors hover:bg-hover">取消</button>
            <button type="button" onClick={submit} disabled={!draft.trim()} className="rounded px-2 py-1 font-medium text-brand transition-colors hover:bg-hover disabled:opacity-50">保存</button>
          </div>
        </div>
      ) : (
        <div className="min-w-0 flex-1">
          {item.content && <div className="line-clamp-4 whitespace-pre-wrap text-[13.5px] leading-6 text-body" title={item.content}>{item.content}</div>}
          {item.attachments.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-2">
              {item.attachments.map((name) => <AttachmentThumb key={name} name={name} />)}
            </div>
          )}
        </div>
      )}
      {!editing && (
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={onSendNow}
            title="停止当前回答并立即发送这条消息"
            className="inline-flex items-center gap-1 rounded-full bg-hover px-3.5 py-1.5 text-[13px] font-medium text-ink transition-colors hover:bg-line"
          >
            <ArrowUpOutlined className="text-[12px]" /> 立即
          </button>
          {item.content && (
            <button type="button" onClick={() => { setDraft(item.content); setEditing(true); }} title="编辑这条排队消息" className="grid h-8 w-8 place-items-center rounded-full text-muted transition-colors hover:bg-hover hover:text-ink">
              <EditOutlined />
            </button>
          )}
          <button type="button" onClick={onDelete} title="删除这条排队消息" className="grid h-8 w-8 place-items-center rounded-full text-muted transition-colors hover:bg-hover hover:text-ink">
            <DeleteOutlined />
          </button>
        </div>
      )}
    </div>
  );
}

// DragOverlay 里跟随指针的浮层卡片：纯静态展示（不可交互），宽度由 DragOverlay 按原卡片量测值锁定。
// 必须是独立组件——不能复用 QueueCard，否则会注册一个同 id 的 sortable
function QueueCardOverlay({ item }: { item: QueuedMessage }) {
  return (
    <div className="flex min-h-[52px] cursor-grabbing items-center gap-3 rounded-2xl border border-line bg-soft px-3.5 py-2 shadow-[0_18px_36px_-14px_rgba(20,35,80,.45)]">
      <span className="shrink-0 select-none text-[16px] text-muted-weak"><HolderOutlined /></span>
      <div className="min-w-0 flex-1">
        {item.content && <div className="line-clamp-4 whitespace-pre-wrap text-[13.5px] leading-6 text-body">{item.content}</div>}
        {item.attachments.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-2">
            {item.attachments.map((name) => <AttachmentThumb key={name} name={name} />)}
          </div>
        )}
      </div>
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
            ref={focusAtEnd}
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
          {/* 纯多模态（如仅图片、无文字）的消息没有可编辑的文本，不提供复制/编辑重发 */}
          {item.content && (
            <div className="flex items-center gap-1 text-xs text-muted-weak">
              <button onClick={() => onCopy(item.content)} className="rounded px-2 py-1 transition-colors hover:bg-hover">
                <CopyOutlined /> 复制
              </button>
              <button
                onClick={() => { setDraft(item.content); setEditing(true); }}
                disabled={busy}
                title={busy ? '生成中暂不能编辑' : '编辑这条提问并重新发送（该条之后的消息会被删除）'}
                className="rounded px-2 py-1 transition-colors hover:bg-hover disabled:opacity-50"
              >
                <EditOutlined /> 编辑重发
              </button>
            </div>
          )}
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

// 协作过程中的一位成员：思考（限高滚动）+ 工具调用 chip + 汇报文本（Markdown）
function AgentTraceSection({ trace, streaming }: { trace: AgentTrace; streaming: boolean }) {
  const calls = trace.toolCalls || [];
  return (
    <div>
      <div className="flex items-center gap-1.5 text-xs font-medium text-body">
        {streaming ? <LoadingOutlined spin className="text-[#4d6bfe]" /> : <CheckCircleOutlined className="text-emerald-500" />}
        <span>{trace.name}</span>
        <span className="font-normal text-muted-weak">{calls.length ? `${calls.length} 次调用` : '执行中'}</span>
      </div>
      {trace.thinking ? (
        <div className="scroll-hide mt-1.5 max-h-[140px] overflow-y-auto whitespace-pre-wrap border-l-2 border-line-strong pl-2 text-[12px] leading-5 text-muted">{trace.thinking}</div>
      ) : null}
      {calls.length ? <div className="mt-1.5"><ToolCallChips calls={calls} /></div> : null}
      {trace.text ? <div className="md mt-1.5 text-[13px] leading-6 text-body"><Markdown streaming={streaming}>{trace.text}</Markdown></div> : null}
    </div>
  );
}

// 多智能体协作过程面板：每位成员一节，流式期间自动展开、结束后折叠（可手动切换）。
// 成员的思考/调用/汇报不进主回答（主回答是 leader 的综合结果）
function AgentTracesPanel({ traces, streaming, open, onToggle }: { traces: AgentTrace[]; streaming: boolean; open: boolean; onToggle: () => void }) {
  const totalCalls = traces.reduce((sum, trace) => sum + (trace.toolCalls?.length || 0), 0);
  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-line bg-soft">
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs text-muted transition-colors hover:bg-hover">
        <TeamOutlined />
        <span>{streaming ? '协作进行中…' : '协作过程'}</span>
        <span className="truncate text-muted-weak">{traces.map((trace) => trace.name).join('、')}{totalCalls ? ` · ${totalCalls} 次调用` : ''}</span>
        <span className="ml-auto shrink-0 text-[11px]">{open ? '收起' : '展开'}</span>
        {open ? <UpOutlined className="text-[10px]" /> : <DownOutlined className="text-[10px]" />}
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3 py-2.5">
          {traces.map((trace) => <AgentTraceSection key={trace.name} trace={trace} streaming={streaming} />)}
        </div>
      )}
    </div>
  );
}

// 流式期间的状态提示：无正文时的占位（正在思考/正在检索），或调用工具期间显示在已有正文下方
function StreamingHint({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[14.5px] text-muted">
      <LoadingOutlined spin className="text-[#4d6bfe]" /> {label}
      <span className="thinking-dot">…</span>
    </div>
  );
}

// 工具 chip 的展示映射：按工具名选图标与文案（query 字段承载检索词/命令/文件路径/任务标题）
function toolChipInfo(call: ToolCall): { icon: ReactNode; label: string } {
  const q = call.query || '';
  switch (call.name) {
    case 'TeamAssign': return { icon: <TeamOutlined />, label: `已委派：${q || '成员'}` };
    case 'web_search': return { icon: <GlobalOutlined />, label: `联网搜索：${q || '网络'}` };
    case 'Bash': return { icon: <CodeOutlined />, label: `已执行命令：${q || 'shell'}` };
    case 'Read': return { icon: <FileTextOutlined />, label: `已读取：${q || '文件'}` };
    case 'Write': return { icon: <FileTextOutlined />, label: `已写入：${q || '文件'}` };
    case 'Edit': return { icon: <EditOutlined />, label: `已编辑：${q || '文件'}` };
    case 'Glob': return { icon: <FolderOpenOutlined />, label: `已查找文件：${q || '工作区'}` };
    case 'Grep': return { icon: <SearchOutlined />, label: `已搜索内容：${q || '工作区'}` };
    case 'TaskCreate':
    case 'TaskUpdate': return { icon: <OrderedListOutlined />, label: `任务规划：${q || '更新进度'}` };
    case 'TaskGet':
    case 'TaskList': return { icon: <OrderedListOutlined />, label: '查看任务清单' };
    // medical_rag_search 及未知工具按本地文献检索展示
    default: return { icon: <SearchOutlined />, label: `已检索：${q || '医学文献库'}` };
  }
}

// 展示本轮的检索记录：一次回答可能并行检索多个角度，逐个显示实际检索词，避免看起来像重复 chip。
// 多 Agent 模式下 chip 前缀来源 agent 名（检索员/执行员；Friday 是主 Agent 不标）
function ToolCallChips({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-1.5">
      {calls.map((call, index) => {
        const { icon, label } = toolChipInfo(call);
        const byline = call.agent && call.agent !== 'Friday' ? `${call.agent} · ` : '';
        return (
          <span key={index} title={call.query || call.name} className="inline-flex max-w-[320px] items-center gap-1 rounded-full border border-line-brand bg-brand-soft px-2.5 py-1 text-[11px] text-brand-text">
            {icon}
            <span className="truncate">{byline}{label}</span>
          </span>
        );
      })}
    </div>
  );
}

// 权限确认卡片：DEFAULT / ACCEPT_EDITS 模式下未授权操作推到前端，允许 / 总是允许 / 拒绝
// （超时无人应答由后端自动拒绝并推 permission_resolved 事件）
function PermissionCard({ ask, onConfirm }: { ask: PermissionAsk; onConfirm: (approved: boolean, always: boolean) => void }) {
  const statusText = ask.status === 'approved' ? '已允许' : ask.status === 'denied' ? '已拒绝' : '超时已自动拒绝';
  return (
    <div className="mb-2 rounded-xl border border-[#d8b75c] bg-[#fdf6e3] px-3 py-2 text-[13px] dark:border-[#6b5a24] dark:bg-[#332b14]">
      <div className="mb-1.5 font-medium text-body">权限确认：{ask.agent || 'Friday'} 请求执行以下操作</div>
      {ask.calls.map((call) => (
        <div key={call.id} className="mb-1 truncate font-mono text-[12px] text-muted" title={call.query}>
          {call.name}{call.query ? `：${call.query}` : ''}
        </div>
      ))}
      {ask.status === 'pending' ? (
        <div className="mt-2 flex gap-2">
          <Button size="small" type="primary" onClick={() => onConfirm(true, false)}>允许</Button>
          <Button size="small" onClick={() => onConfirm(true, true)}>总是允许</Button>
          <Button size="small" danger onClick={() => onConfirm(false, false)}>拒绝</Button>
        </div>
      ) : (
        <div className="mt-1 text-xs text-muted-weak">{statusText}</div>
      )}
    </div>
  );
}

export default function ChatWorkspace({ conversationId }: { conversationId?: string }) {
  const { message } = App.useApp();
  const token = useAuth((state) => state.token);
  const setSidebarOpen = useUI((state) => state.setSidebarOpen);
  const bumpConversations = useUI((state) => state.bumpConversations);
  const [id, setId] = useState(conversationId);
  const [title, setTitle] = useState('新的对话');
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const [searching, setSearching] = useState(false);
  // 多 Agent 开关（默认开）：关闭后整轮单 Agent，行为与原有模式一致
  const [multiAgent, setMultiAgent] = useState(true);
  // 会话工作区与权限模式：由会话详情带出（工作区会话自动开启 Agent 工具）；
  // 工作区在侧边栏「工作区」组标题 + 时选定，会话内不可更改
  const [workspaceRoot, setWorkspaceRoot] = useState<string | null>(null);
  const [permissionMode, setPermissionMode] = useState<(typeof PERMISSION_MODE_OPTIONS)[number]['value']>('default');
  const [loading, setLoading] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const streamBuffer = useRef('');
  const thinkingBuffer = useRef('');
  // 成员（检索员/执行员）汇报与思考的打字机缓冲：按 agent 分组，与主回答（Friday）独立
  const agentBuffers = useRef<Record<string, { text: string; thinking: string }>>({});
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // 正在流式输出的 assistant 消息 id：用于判断思考面板该展开还是收起
  const [streamingId, setStreamingId] = useState<string | null>(null);
  // 模型正在调用工具（tool_call 事件后、正文恢复流式前）：期间正文下方持续显示「正在检索/执行…」
  const [toolRunning, setToolRunning] = useState(false);
  // 最近一次调用的工具名：内置工具（Bash/文件/任务）提示「正在执行」，检索类提示「正在检索」
  const [runningTool, setRunningTool] = useState('');
  // 最近一次工具调用的来源 agent（「检索员/执行员」；运行提示前缀用，Friday 不标）
  const [runningAgent, setRunningAgent] = useState('');
  // 用户手动展开/收起思考过程的覆盖值
  const [thinkingOpen, setThinkingOpen] = useState<Record<string, boolean>>({});
  // 用户手动展开/收起协作过程面板的覆盖值（默认：流式期间展开，结束后折叠）
  const [traceOpen, setTraceOpen] = useState<Record<string, boolean>>({});
  // 当前流式请求的中止控制器（「停止生成」按钮使用）
  const abortRef = useRef<AbortController | null>(null);
  // 发送队列：回答生成中继续发送的消息在此排队，流结束后由 drainQueue 轮候发出。
  // 控制流一律读 ref（state 只负责渲染），避免异步收尾链路拿到过期闭包
  const [queue, setQueue] = useState<QueuedMessage[]>([]);
  const queueRef = useRef<QueuedMessage[]>([]);
  const busyRef = useRef(false);
  // 「立即发送」的插队项：abort 当前流后，收尾时优先发出它而不是队首
  const immediateNextRef = useRef<QueuedMessage | null>(null);
  // 显式停止/发送出错后置位：本轮收尾不自动续发队列
  const skipNextDrainRef = useRef(false);
  // latest-ref：出队发送时调用最新一次渲染的 send（读到最新的开关与会话 id）
  const sendRef = useRef<(override?: { content: string; attachments: string[] }) => void>(() => {});
  const updateQueue = (next: QueuedMessage[]) => { queueRef.current = next; setQueue(next); };
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

  // 会话切换：重置滚动/工作区/权限模式（工作区与权限随详情返回，无则默认）
  useEffect(() => {
    stick.current = true; setAtBottom(true); setWorkspaceRoot(null); setPermissionMode('default');
    // 清空排队状态：在途流的收尾不得把排队消息发进新切换的会话
    updateQueue([]);
    immediateNextRef.current = null;
    skipNextDrainRef.current = true;
    if (token && conversationId) conversation(token, conversationId).then((data) => {
      setId(data.id); setTitle(data.title); setMessages(data.messages);
      setWorkspaceRoot(data.workspace_root || null);
      if (data.permission_mode && PERMISSION_MODE_OPTIONS.some((option) => option.value === data.permission_mode)) setPermissionMode(data.permission_mode as typeof permissionMode);
    });
    else { setId(undefined); setTitle('新的对话'); setMessages([]); }
  }, [token, conversationId]);
  // 只有用户本来贴着底部才自动跟随：流式输出期间上滑阅读不会被拉回底部
  useEffect(() => { const el = scroller.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [messages]);

  // 停止当前生成：队列保留、不自动续发（排队卡片仍可编辑/删除/立即发送），
  // 之后手动发送的新消息会插队先行，其回答结束后队列继续轮候
  const stopGenerating = () => {
    if (queueRef.current.length) skipNextDrainRef.current = true;
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
    if ((!content && !sentAttachments.length) || !token) return;
    // 正在回答时不再丢弃输入：进入发送队列，当前回答结束后自动轮候发出
    if (busyRef.current) {
      if (queueRef.current.length >= MAX_QUEUE) {
        message.warning(`排队消息最多 ${MAX_QUEUE} 条`);
        return; // 保留输入框内容，便于用户先处理队列
      }
      updateQueue([...queueRef.current, { id: crypto.randomUUID(), content, attachments: sentAttachments }]);
      if (!override) { setInput(''); setAttachments([]); }
      return;
    }
    busyRef.current = true;
    skipNextDrainRef.current = false; // 清掉可能残留的「停止后不续发」标记
    setLoading(true);
    if (!override) { setInput(''); setAttachments([]); }
    stick.current = true; setAtBottom(true);
    let target = id;
    if (!target) { const created = await createConversation(token, content.slice(0, 30)); target = created.id; setId(target); setTitle(created.title); window.history.replaceState(null, '', `/chat/${target}`); bumpConversations(); }
    const userMessage: Message = { id: crypto.randomUUID(), role: 'user', content, meta: { attachments: sentAttachments }, created_at: new Date().toISOString() };
    const assistantId = crypto.randomUUID();
    setMessages((old) => [...old, userMessage, { id: assistantId, role: 'assistant', content: '', meta: {}, created_at: new Date().toISOString() }]);
    streamBuffer.current = '';
    thinkingBuffer.current = '';
    agentBuffers.current = {};
    setStreamingId(assistantId);
    setToolRunning(false);
    // 打字机节奏放缓冲区：每个 token 都 setState 会导致整列表+Markdown 高频重渲染。
    // flusher 必须随发送立即启动，否则流式期间界面会一直停在"正在思考…"。
    const streamDone = { current: false };
    let finishFlush: () => void = () => {};
    const flushed = new Promise<void>((resolve) => { finishFlush = resolve; });
    // 把某个成员的缓冲按打字机节奏并入消息 meta.agentTraces（不存在则建档）
    const applyTraceReveals = (reveals: Record<string, { text?: string; thinking?: string }>) => {
      setMessages((old) => old.map((item) => {
        if (item.id !== assistantId) return item;
        const traces = [...((item.meta?.agentTraces as AgentTrace[]) || [])];
        for (const [name, reveal] of Object.entries(reveals)) {
          const index = traces.findIndex((trace) => trace.name === name);
          const base = index >= 0 ? traces[index] : { name, text: '', thinking: '', toolCalls: [] as ToolCall[] };
          const next: AgentTrace = {
            ...base,
            text: (base.text || '') + (reveal.text || ''),
            thinking: (base.thinking || '') + (reveal.thinking || ''),
          };
          if (index >= 0) traces[index] = next; else traces.push(next);
        }
        return { ...item, meta: { ...item.meta, agentTraces: traces } };
      }));
    };
    flushTimer.current = setInterval(() => {
      const buffered = streamBuffer.current;
      const pendingThinking = thinkingBuffer.current;
      // 思考、正文与各成员缓冲全部排空，且上游结束，才算这一次流式真正收尾
      const agentReveals: Record<string, { text?: string; thinking?: string }> = {};
      for (const [name, buf] of Object.entries(agentBuffers.current)) {
        const reveal: { text?: string; thinking?: string } = {};
        if (buf.thinking) {
          const take = Math.min(buf.thinking.length, Math.max(4, Math.ceil(buf.thinking.length / 8)));
          reveal.thinking = buf.thinking.slice(0, take);
          buf.thinking = buf.thinking.slice(take);
        }
        if (buf.text) {
          const take = Math.min(buf.text.length, Math.max(2, Math.ceil(buf.text.length / 8)));
          reveal.text = buf.text.slice(0, take);
          buf.text = buf.text.slice(take);
        }
        if (reveal.text || reveal.thinking) agentReveals[name] = reveal;
      }
      if (Object.keys(agentReveals).length) applyTraceReveals(agentReveals);
      const agentPending = Object.values(agentBuffers.current).some((buf) => buf.text || buf.thinking);
      if (!buffered && !pendingThinking && !agentPending) {
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
    try { await streamMessage(token, target, { content, deep_thinking: thinking, web_search: searching, agent_tools: !!workspaceRoot, permission_mode: permissionMode, multi_agent: multiAgent, attachments: sentAttachments }, (event) => {
      // 事件按来源分流：Friday（主 Agent）进主回答/主思考/主 chip 行；成员
      // （检索员/执行员）的思考与汇报进各自的打字机缓冲、chip 进 meta.agentTraces，
      // 由「协作过程」面板分组展示
      const worker = event.agent && event.agent !== 'Friday' ? event.agent : '';
      const workerBuffer = () => (agentBuffers.current[worker] || (agentBuffers.current[worker] = { text: '', thinking: '' }));
      // 流首 sent 事件：把乐观渲染的本地 id 替换成库中真实 id，编辑重发的截断才能按 id 定位
      if (event.type === 'sent' && event.message_id) { setMessages((old) => old.map((row) => row.id === userMessage.id ? { ...row, id: event.message_id! } : row)); return; }
      if (event.type === 'thinking') { if (worker) workerBuffer().thinking += event.content || ''; else thinkingBuffer.current += event.content || ''; }
      else if (event.type === 'text') { setToolRunning(false); if (worker) workerBuffer().text += event.content || ''; else streamBuffer.current += event.content || ''; }
      else if (event.type === 'tool_call') {
        setToolRunning(true); setRunningTool(event.name || ''); setRunningAgent(event.agent || '');
        const entry: ToolCall = { name: event.name || 'medical_rag_search', query: event.query, agent: event.agent };
        if (worker) {
          setMessages((old) => old.map((row) => {
            if (row.id !== assistantId) return row;
            const traces = [...((row.meta?.agentTraces as AgentTrace[]) || [])];
            const index = traces.findIndex((trace) => trace.name === worker);
            const base = index >= 0 ? traces[index] : { name: worker, text: '', thinking: '', toolCalls: [] as ToolCall[] };
            const next = { ...base, toolCalls: [...(base.toolCalls || []), entry] };
            if (index >= 0) traces[index] = next; else traces.push(next);
            return { ...row, meta: { ...row.meta, agentTraces: traces } };
          }));
        } else {
          setMessages((old) => old.map((row) => row.id === assistantId ? { ...row, meta: { ...row.meta, toolCalls: [...((row.meta?.toolCalls as ToolCall[]) || []), entry] } } : row));
        }
      }
      else if (event.type === 'permission_ask') { setToolRunning(false); setMessages((old) => old.map((row) => row.id === assistantId ? { ...row, meta: { ...row.meta, permissionAsk: { calls: event.calls || [], status: 'pending', agent: event.agent } as PermissionAsk } } : row)); }
      else if (event.type === 'permission_resolved') { setMessages((old) => old.map((row) => row.id === assistantId ? { ...row, meta: { ...row.meta, permissionAsk: { ...(((row.meta?.permissionAsk as PermissionAsk) || { calls: [] })), status: event.approved ? 'approved' : (event.timed_out ? 'timeout' : 'denied') } as PermissionAsk } } : row)); }
    }, controller.signal); } catch (error) {
      // 用户主动中止不算错误：保留已生成的部分并标记
      if (error instanceof DOMException && error.name === 'AbortError') {
        setMessages((old) => old.map((item) => item.id === assistantId ? { ...item, meta: { ...item.meta, stopped: true } } : item));
        message.info('已停止生成');
      } else {
        skipNextDrainRef.current = true; // 出错后不自动续发队列，避免连环失败
        message.error(error instanceof Error ? error.message : '发送失败');
      }
    } finally { abortRef.current = null; streamDone.current = true; await flushed; busyRef.current = false; setLoading(false); setStreamingId(null); setToolRunning(false); setRunningAgent(''); drainQueue(); }
  };

  // 权限确认卡片应答：先乐观置状态防重复点击，再调确认接口；后端随后的
  // permission_resolved 事件会用权威结果（含超时）覆盖同一状态
  const answerPermission = async (approved: boolean, always: boolean) => {
    if (!token || !id) return;
    setMessages((old) => old.map((row) => row.id === streamingId ? { ...row, meta: { ...row.meta, permissionAsk: { ...(((row.meta?.permissionAsk as PermissionAsk) || { calls: [] })), status: approved ? 'approved' : 'denied' } as PermissionAsk } } : row));
    try {
      await confirmPermission(token, id, approved, always);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '应答失败');
    }
  };

  // 切换权限模式：乐观更新 + 存库（会话记住；后端 chat 以会话设置为最高优先级）
  const changePermissionMode = async (mode: string) => {
    if (!token || !id || mode === permissionMode) return;
    if (!PERMISSION_MODE_OPTIONS.some((option) => option.value === mode)) return;
    const previous = permissionMode;
    setPermissionMode(mode as typeof permissionMode);
    try {
      await savePermissionMode(token, id, mode);
    } catch (error) {
      setPermissionMode(previous);
      message.error(error instanceof Error ? error.message : '保存权限模式失败');
    }
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

  // 出队调度：一次流式收尾后调用（停止/出错经 skipNextDrainRef 跳过本轮）；
  // 「立即发送」的插队项优先于队首
  const drainQueue = () => {
    if (busyRef.current) return;
    if (skipNextDrainRef.current) { skipNextDrainRef.current = false; return; }
    const immediate = immediateNextRef.current;
    if (immediate) {
      immediateNextRef.current = null;
      sendRef.current(immediate);
      return;
    }
    const [next, ...rest] = queueRef.current;
    if (next) {
      updateQueue(rest);
      sendRef.current(next);
    }
  };

  // 立即发送：打断当前回答（走停止链路，已生成部分照常落库并标记），收尾后优先发出这条
  const sendNow = (item: QueuedMessage) => {
    if (immediateNextRef.current) return; // 已有一条在等插队，忽略连点避免丢消息
    updateQueue(queueRef.current.filter((row) => row.id !== item.id));
    if (!busyRef.current) { sendRef.current(item); return; }
    immediateNextRef.current = item;
    abortRef.current?.abort();
  };

  const saveQueued = (id: string, content: string) => {
    updateQueue(queueRef.current.map((row) => (row.id === id ? { ...row, content } : row)));
  };

  const deleteQueued = (id: string) => {
    updateQueue(queueRef.current.filter((row) => row.id !== id));
  };

  // 排队卡片拖拽换序（@dnd-kit）：PointerSensor 位移 4px 才激活（避免误触），
  // KeyboardSensor 支持手柄聚焦后空格+方向键调整顺序
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  // 拖动中的排队消息 id：DragOverlay 据此渲染跟随指针的浮层
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const draggingItem = queue.find((row) => row.id === draggingId);
  const handleQueueDragEnd = ({ active, over }: DragEndEvent) => {
    setDraggingId(null);
    if (!over || active.id === over.id) return;
    const from = queueRef.current.findIndex((row) => row.id === active.id);
    const to = queueRef.current.findIndex((row) => row.id === over.id);
    if (from < 0 || to < 0) return;
    updateQueue(arrayMove(queueRef.current, from, to));
  };

  sendRef.current = send;

  return (
    <div className="flex h-full min-w-0 flex-col bg-canvas">
      <header className="flex h-[60px] shrink-0 items-center justify-between border-b border-line/80 bg-surface/75 px-4 backdrop-blur-md sm:px-6">
        <div className="flex min-w-0 items-center gap-1.5">
          <button
            title="打开菜单"
            onClick={() => setSidebarOpen(true)}
            className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-body hover:bg-hover md:hidden"
          >
            <MenuOutlined />
          </button>
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-medium">{title}</h1>
            <span className="text-xs text-muted-weak">{messages.length} 条消息</span>
          </div>
          {workspaceRoot && (
            <span className="ml-1 hidden min-w-0 items-center gap-1 rounded-full border border-line bg-soft px-2 py-0.5 text-[11px] text-muted sm:inline-flex" title={workspaceRoot}>
              <FolderOpenOutlined className="shrink-0 text-brand-text" />
              <span className="max-w-[220px] truncate">{workspaceRoot.split('/').filter(Boolean).slice(-2).join('/')}</span>
            </span>
          )}
          {workspaceRoot && (
            <span className="ml-1 grid h-6 w-6 shrink-0 place-items-center rounded-full border border-line bg-soft text-[11px] text-brand-text sm:hidden" title={workspaceRoot}>
              <FolderOpenOutlined />
            </span>
          )}
        </div>
        <ThemeToggle />
      </header>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div ref={scroller} onScroll={handleScroll} className="scroll-hide min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[800px] space-y-7 px-4 py-7 sm:px-6">
            {messages.length === 0 ? (
              <div className="pt-14 text-center">
                <Logo className="mx-auto h-14 w-14 drop-shadow-[0_10px_24px_rgba(77,107,254,.35)]" />
                <h2 className="mt-5 text-2xl font-semibold">Hi，我是 Friday</h2>
                <p className="mx-auto mt-2 max-w-[460px] text-[13.5px] leading-6 text-muted">
                  我可以帮你写代码、读文件、写作、做方案，也能联网查最新资料。
                </p>
              </div>
            ) : (
              messages.map((item) => {
                if (item.role === 'user') {
                  return (
                    <div key={item.id} className="msg-in flex justify-end">
                      <UserMessage item={item} busy={loading} onCopy={copyText} onResend={resendFrom} />
                    </div>
                  );
                }
                const thinkingText = (item.meta?.thinking as string) || '';
                const isStreamingThis = item.id === streamingId;
                const thinkingExpanded = thinkingOpen[item.id] ?? isStreamingThis;
                const agentTraces = (item.meta?.agentTraces as AgentTrace[]) || [];
                const traceExpanded = traceOpen[item.id] ?? isStreamingThis;
                return (
                  <div key={item.id} className="msg-in flex gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-muted">
                        <Logo className="h-5 w-5" />
                        Friday Agent
                      </div>
                      <ToolCallChips calls={(item.meta?.toolCalls as ToolCall[]) || []} />
                      {(item.meta?.permissionAsk as PermissionAsk | undefined)?.calls?.length ? (
                        <PermissionCard ask={item.meta!.permissionAsk as PermissionAsk} onConfirm={answerPermission} />
                      ) : null}
                      {agentTraces.length > 0 && (
                        <AgentTracesPanel
                          traces={agentTraces}
                          streaming={isStreamingThis}
                          open={traceExpanded}
                          onToggle={() => setTraceOpen((old) => ({ ...old, [item.id]: !traceExpanded }))}
                        />
                      )}
                      {thinkingText && (
                        <ThinkingPanel
                          text={thinkingText}
                          streaming={isStreamingThis}
                          open={thinkingExpanded}
                          onToggle={() => setThinkingOpen((old) => ({ ...old, [item.id]: !thinkingExpanded }))}
                        />
                      )}
                      {item.content ? (
                        <div className="md">
                          <Markdown streaming={isStreamingThis}>{item.content}</Markdown>
                          {isStreamingThis && toolRunning && (
                            <div className="mt-1">
                              <StreamingHint label={toolRunningLabel(runningTool, runningAgent)} />
                            </div>
                          )}
                          <div className="mt-2 flex gap-1 text-xs text-muted-weak">
                            <button onClick={() => copyText(item.content)} className="rounded px-2 py-1 transition-colors hover:bg-hover">
                              <CopyOutlined /> 复制
                            </button>
                            <button
                              onClick={() => toggleLike(item.id)}
                              className={`rounded px-2 py-1 transition-colors hover:bg-hover ${liked[item.id] ? 'text-brand' : ''}`}
                            >
                              {liked[item.id] ? <LikeFilled /> : <LikeOutlined />} 有帮助
                            </button>
                          </div>
                        </div>
                      ) : (
                        <StreamingHint label={isStreamingThis && toolRunning ? toolRunningLabel(runningTool, runningAgent) : '正在思考'} />
                      )}
                      {item.meta?.stopped ? <div className="mt-2 text-xs text-muted-weak">已停止生成</div> : null}
                    </div>
                  </div>
                );
              })
            )}
            <div ref={bottom} />
          </div>
        </div>
        {!atBottom && (
          <button
            onClick={jumpToBottom}
            title="回到底部"
            aria-label="回到底部"
            className="absolute bottom-4 left-1/2 z-10 grid h-9 w-9 -translate-x-1/2 place-items-center rounded-full border border-line-strong bg-surface text-muted shadow-[0_10px_24px_-12px_rgba(20,35,80,.6)] transition-colors hover:bg-hover hover:text-ink"
          >
            <ArrowDownOutlined />
          </button>
        )}
      </div>
      <div className="shrink-0 px-4 pb-5 pt-1 sm:px-6">
        {queue.length > 0 && (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={({ active }) => setDraggingId(String(active.id))}
            onDragEnd={handleQueueDragEnd}
            onDragCancel={() => setDraggingId(null)}
          >
            <SortableContext items={queue} strategy={verticalListSortingStrategy}>
              <div className="scroll-hide mx-auto mb-2 max-h-[40vh] max-w-[800px] space-y-2 overflow-y-auto">
                {queue.map((item) => (
                  <QueueCard
                    key={item.id}
                    item={item}
                    onSendNow={() => sendNow(item)}
                    onSave={(content) => saveQueued(item.id, content)}
                    onDelete={() => deleteQueued(item.id)}
                  />
                ))}
              </div>
            </SortableContext>
            <DragOverlay>
              {draggingItem ? <QueueCardOverlay item={draggingItem} /> : null}
            </DragOverlay>
          </DndContext>
        )}
        <div className="mx-auto max-w-[800px] rounded-[24px] border border-line-strong bg-surface/95 px-4 pb-2.5 pt-3 shadow-[0_18px_44px_-24px_rgba(20,35,80,.35)] backdrop-blur transition-all duration-200 focus-within:border-line-focus focus-within:shadow-[0_18px_46px_-20px_rgba(77,107,254,.5)]">
          {attachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {attachments.map((item) => (
                <div key={item.name} className="relative">
                  <AttachmentThumb name={item.name} />
                  <button
                    type="button"
                    title="移除图片"
                    onClick={() => setAttachments((old) => old.filter((row) => row.name !== item.name))}
                    className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full border border-line bg-surface text-[10px] text-muted hover:text-ink"
                  >
                    <CloseOutlined />
                  </button>
                </div>
              ))}
            </div>
          )}
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder={loading ? '继续输入以排队后续修改' : '给 Friday 发送消息…'}
            className="block w-full resize-none border-0 bg-transparent py-1 text-[14.5px] leading-6 outline-none"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              title={`上传图片（最多 ${MAX_ATTACHMENTS} 张）`}
              onClick={() => fileInput.current?.click()}
              disabled={uploading}
              className="grid h-8 w-8 place-items-center rounded-lg text-weak hover:bg-hover disabled:opacity-50"
            >
              {uploading ? <LoadingOutlined spin /> : <PaperClipOutlined />}
            </button>
            <input ref={fileInput} type="file" accept="image/*" multiple className="hidden" onChange={pickImage} />
            {SHOW_DEEP_THINKING && (
              <button
                onClick={() => setThinking(!thinking)}
                className={`rounded-full border px-3 py-1 text-xs ${thinking ? 'border-[#4d6bfe] bg-brand-soft text-brand-text' : 'border-line-strong text-weak'}`}
              >
                <BulbOutlined /> 深度思考
              </button>
            )}
            {SHOW_WEB_SEARCH && (
              <button
                onClick={() => setSearching(!searching)}
                className={`rounded-full border px-3 py-1 text-xs ${searching ? 'border-[#4d6bfe] bg-brand-soft text-brand-text' : 'border-line-strong text-weak'}`}
              >
                <GlobalOutlined /> 联网搜索
              </button>
            )}
            {SHOW_MULTI_AGENT && (
              <button
                title="开启后由团队协作完成：Friday 规划派活，检索员/执行员分别负责检索与文件命令执行"
                onClick={() => setMultiAgent(!multiAgent)}
                className={`rounded-full border px-3 py-1 text-xs ${multiAgent ? 'border-[#4d6bfe] bg-brand-soft text-brand-text' : 'border-line-strong text-weak'}`}
              >
                <TeamOutlined /> 多 Agent
              </button>
            )}
            {workspaceRoot && (
              <Dropdown
                trigger={['click']}
                menu={{
                  items: PERMISSION_MODE_OPTIONS.map((option) => ({
                    key: option.value,
                    label: (
                      <span className="flex flex-col py-0.5">
                        <span>{option.label}</span>
                        <span className="text-[11px] text-muted-weak">{option.hint}</span>
                      </span>
                    ),
                  })),
                  selectedKeys: [permissionMode],
                  onClick: ({ key }) => changePermissionMode(key),
                }}
              >
                <button
                  title="Agent 工具权限模式（随会话记住）"
                  className="rounded-full border border-[#4d6bfe] bg-brand-soft px-3 py-1 text-xs text-brand-text"
                >
                  <SafetyOutlined /> {PERMISSION_MODE_OPTIONS.find((option) => option.value === permissionMode)?.label} <DownOutlined className="text-[9px]" />
                </button>
              </Dropdown>
            )}
            {loading && !input.trim() && !attachments.length ? (
              <button
                type="button"
                onClick={stopGenerating}
                title="停止生成"
                aria-label="停止生成"
                className="ml-auto grid h-8 w-8 shrink-0 place-items-center rounded-full border border-line-brand bg-brand-soft text-brand-text shadow-[0_6px_16px_-10px_rgba(77,107,254,.9)] transition-colors hover:border-line-hover hover:bg-hover hover:text-ink"
              >
                <span className="block h-3 w-3 rounded-[3px] bg-current" />
              </button>
            ) : (
              <Button
                type={loading ? 'default' : 'primary'}
                shape="circle"
                icon={<SendOutlined />}
                disabled={!input.trim() && !attachments.length}
                onClick={() => send()}
                title={loading ? '加入发送队列，当前回答结束后自动发送' : '发送'}
                className={loading ? 'ml-auto h-8 w-8 shrink-0' : 'ml-auto h-9 w-9 border-0 bg-gradient-to-br from-[#4d6bfe] to-[#7288ff] shadow-[0_8px_18px_-8px_rgba(77,107,254,.9)] transition-transform hover:scale-105'}
              />
            )}
          </div>
        </div>
        <p className="mt-2 text-center text-[11.5px] text-muted-weak">内容由 AI 生成，请仔细甄别</p>
      </div>
    </div>
  );
}
