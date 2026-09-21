'use client';

import { useEffect, useState } from 'react';
import { App, Button, Form, Input, Tabs } from 'antd';
import { LockOutlined, ReloadOutlined, UserOutlined } from '@ant-design/icons';
import { useRouter } from 'next/navigation';
import { captcha, login, register } from '@/lib/api';
import ThemeToggle from '@/components/ThemeToggle';
import { useAuth } from '@/store/auth';

type AuthPageProps = { initialMode?: 'login' | 'register' };

export default function AuthPage({ initialMode = 'login' }: AuthPageProps) {
  const { message } = App.useApp();
  const router = useRouter();
  const setAuth = useAuth((state) => state.setAuth);
  const [mode, setMode] = useState<'login' | 'register'>(initialMode);
  const [code, setCode] = useState({ image: '', token: '' });
  const [loading, setLoading] = useState(false);

  const refresh = () => captcha().then(setCode).catch(() => message.error('验证码加载失败'));
  useEffect(() => { refresh(); }, []);

  const submit = async (values: Record<string, string>) => {
    setLoading(true);
    try {
      const payload = { ...values, captcha_token: code.token };
      const result = mode === 'login' ? await login(payload) : await register(payload);
      setAuth(result.access_token, result.user);
      router.push('/chat');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '操作失败');
      refresh();
    } finally {
      setLoading(false);
    }
  };

  return <main className="relative min-h-screen bg-canvas px-5 py-10 sm:grid sm:place-items-center">
    <div className="absolute right-5 top-5"><ThemeToggle /></div>
    <section className="mx-auto grid w-full max-w-[980px] overflow-hidden rounded-[28px] bg-surface shadow-[0_32px_90px_-36px_rgba(57,79,170,.55)] sm:grid-cols-[.92fr_1.08fr]">
      <div className="hidden bg-gradient-to-br from-[#4d6bfe] via-[#5570ff] to-[#7c94ff] p-12 text-white sm:block"><div className="grid h-11 w-11 place-items-center rounded-2xl bg-white/15 text-xl font-bold backdrop-blur">F</div><h1 className="mt-20 text-4xl font-semibold tracking-tight">Friday Agent</h1><p className="mt-5 max-w-[300px] text-[15px] leading-7 text-blue-100">把复杂任务拆开、记住上下文，并以流式方式陪你完成每一步。</p><div className="mt-24 text-sm text-blue-100">安全登录 · 会话持久化 · AgentScope 驱动</div></div>
      <div className="p-7 sm:p-12"><div className="mb-8 sm:hidden"><b className="text-xl">Friday Agent</b></div><Tabs activeKey={mode} onChange={(key) => setMode(key as 'login' | 'register')} items={[{ key: 'login', label: '登录' }, { key: 'register', label: '注册' }]} />
        <Form layout="vertical" onFinish={submit} requiredMark={false} className="mt-5">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}><Input prefix={<UserOutlined />} size="large" placeholder="3-32 位用户名" /></Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, min: 6, message: '密码至少 6 位' }]}><Input.Password prefix={<LockOutlined />} size="large" placeholder="至少 6 位密码" /></Form.Item>
          <Form.Item label="验证码" required>
            <div className="flex gap-2">
              <Form.Item name="captcha" noStyle rules={[{ required: true, message: '请输入验证码' }]}><Input size="large" placeholder="输入图片中的字符" /></Form.Item>
              <button type="button" onClick={refresh} className="h-10 w-[120px] shrink-0 overflow-hidden rounded-lg border border-line-strong bg-soft" title="刷新验证码">{code.image ? <img src={code.image} alt="验证码" className="h-full w-full object-cover" /> : <ReloadOutlined />}</button>
            </div>
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={loading} className="mt-1 h-11 rounded-xl border-0 bg-gradient-to-r from-[#4d6bfe] to-[#6a86ff] shadow-[0_10px_24px_-12px_rgba(77,107,254,.9)] transition-transform hover:scale-[1.01]">{mode === 'login' ? '进入工作台' : '创建账号'}</Button>
        </Form>
      </div>
    </section>
  </main>;
}
