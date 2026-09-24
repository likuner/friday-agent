const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api';

export type User = { id: string; username: string };
export type Conversation = { id: string; title: string; created_at: string; updated_at: string; message_count: number; last_message?: string | null; workspace_root?: string | null; permission_mode?: string | null };
export type Message = { id: string; role: 'user' | 'assistant'; content: string; meta: Record<string, unknown>; created_at: string };
// 一条工具调用记录（meta.toolCalls 的元素）；query 是本次实际检索词
export type ToolCall = { name: string; query?: string };
// permission_ask / permission_resolved / sent / tool_call 等 SSE 事件并集。
// reply_id 是服务端 parked reply 的关联标识（协议字段，供排查对账，前端不消费）
export type StreamEvent = { type: string; content?: string; name?: string; query?: string; message_id?: string; reply_id?: string; calls?: { id: string; name: string; query?: string }[]; approved?: boolean; timed_out?: boolean };
// 权限确认卡片状态（SSE permission_ask / permission_resolved 事件驱动）
export type PermissionAsk = { calls: { id: string; name: string; query?: string }[]; status: 'pending' | 'approved' | 'denied' | 'timeout' };
// 上传后的图片附件（后端返回的 name 即文件名）
export type UploadedFile = { name: string; url: string; size: number };
export type ConversationDetail = Conversation & { messages: Message[] };

export async function api<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || '请求失败');
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export async function captcha() { return api<{ image: string; token: string }>('/auth/captcha'); }
export async function login(payload: object) { return api<{ access_token: string; user: User }>('/auth/login', { method: 'POST', body: JSON.stringify(payload) }); }
export async function register(payload: object) { return api<{ access_token: string; user: User }>('/auth/register', { method: 'POST', body: JSON.stringify(payload) }); }
// 会话分页列表：服务端按标题关键词（q）+ 工作区分组（kind）过滤后分页，每页默认 20 条
export type ConversationPage = { items: Conversation[]; total: number; has_more: boolean };
export type ConversationQuery = { q?: string; kind?: 'all' | 'workspace' | 'plain'; limit?: number; offset?: number };
export async function conversations(token: string, query: ConversationQuery = {}) {
  const params = new URLSearchParams();
  if (query.q) params.set('q', query.q);
  if (query.kind) params.set('kind', query.kind);
  if (query.limit != null) params.set('limit', String(query.limit));
  if (query.offset != null) params.set('offset', String(query.offset));
  const suffix = params.toString();
  return api<ConversationPage>(`/conversations${suffix ? `?${suffix}` : ''}`, {}, token);
}
export async function conversation(token: string, id: string) { return api<ConversationDetail>(`/conversations/${id}`, {}, token); }
export async function createConversation(token: string, title = '新的对话') { return api<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({ title }) }, token); }
export async function renameConversation(token: string, id: string, title: string) { return api<Conversation>(`/conversations/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }, token); }
export async function deleteConversation(token: string, id: string) { return api<void>(`/conversations/${id}`, { method: 'DELETE' }, token); }
// 编辑重发用：删除指定消息及其后的全部消息，返回删除条数
export async function truncateMessages(token: string, conversationId: string, messageId: string) { return api<{ deleted: number }>(`/conversations/${conversationId}/messages/${messageId}`, { method: 'DELETE' }, token); }

export async function streamMessage(token: string, id: string, payload: object, onEvent: (event: StreamEvent) => void, signal?: AbortSignal) {
  const response = await fetch(`${API_URL}/conversations/${id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(payload), signal });
  if (!response.ok || !response.body) throw new Error('流式请求失败');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split('\n\n');
    buffer = chunks.pop() || '';
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((item) => item.startsWith('data: '));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

// 图片存在后端 /files 下，静态资源不在 /api 前缀里
export const fileUrl = (name: string) => `${API_URL.replace(/\/api\/?$/, '')}/files/${name}`;

// 应答权限确认卡片：approved=false 时模型收到 denied 结果并继续生成
export async function confirmPermission(token: string, conversationId: string, approved: boolean, always = false) {
  return api<{ ok: boolean }>(`/conversations/${conversationId}/permissions/confirm`, { method: 'POST', body: JSON.stringify({ approved, always }) }, token);
}

// 会话工作区：pick 在后端所在机器弹系统原生目录选择框（本机部署即用户屏幕）；
// set 落库（选定后不可更改，再调 409）。工作区/权限数据随会话列表与详情带出。
export async function pickWorkspace(token: string) { return api<{ path: string | null }>('/workspaces/pick', { method: 'POST' }, token); }
export async function setWorkspace(token: string, conversationId: string, path: string) { return api<{ workspace_root: string }>(`/conversations/${conversationId}/workspace`, { method: 'PUT', body: JSON.stringify({ path }) }, token); }
// 记住会话的权限模式（随时可改；chat 生效优先级：会话设置 > 请求参数 > default）
export async function setPermissionMode(token: string, conversationId: string, mode: string) { return api<{ permission_mode: string }>(`/conversations/${conversationId}/permission-mode`, { method: 'PUT', body: JSON.stringify({ mode }) }, token); }

export async function uploadImage(token: string, file: File): Promise<UploadedFile> {
  const form = new FormData();
  form.append('file', file);
  const response = await fetch(`${API_URL}/files`, { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || '上传失败');
  }
  return response.json();
}
