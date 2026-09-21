const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api';

export type User = { id: string; username: string };
export type Conversation = { id: string; title: string; created_at: string; updated_at: string; message_count: number; last_message?: string | null };
export type Message = { id: string; role: 'user' | 'assistant'; content: string; meta: Record<string, unknown>; created_at: string };
// 一条工具调用记录（meta.toolCalls 的元素）；query 是本次实际检索词
export type ToolCall = { name: string; query?: string };
export type StreamEvent = { type: string; content?: string; name?: string; query?: string; message_id?: string };
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
export async function conversations(token: string) { return api<Conversation[]>('/conversations', {}, token); }
export async function conversation(token: string, id: string) { return api<ConversationDetail>(`/conversations/${id}`, {}, token); }
export async function createConversation(token: string, title = '新的对话') { return api<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({ title }) }, token); }
export async function renameConversation(token: string, id: string, title: string) { return api<Conversation>(`/conversations/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }, token); }
export async function deleteConversation(token: string, id: string) { return api<void>(`/conversations/${id}`, { method: 'DELETE' }, token); }

export async function streamMessage(token: string, id: string, payload: object, onEvent: (event: StreamEvent) => void) {
  const response = await fetch(`${API_URL}/conversations/${id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(payload) });
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
