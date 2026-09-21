import { create } from 'zustand';
import type { User } from '@/lib/api';

type AuthState = { token: string | null; user: User | null; hydrate: () => void; setAuth: (token: string, user: User) => void; logout: () => void };

export const useAuth = create<AuthState>((set) => ({
  token: null,
  user: null,
  hydrate: () => {
    const token = localStorage.getItem('friday_token');
    const raw = localStorage.getItem('friday_user');
    set({ token, user: raw ? JSON.parse(raw) : null });
  },
  setAuth: (token, user) => { localStorage.setItem('friday_token', token); localStorage.setItem('friday_user', JSON.stringify(user)); set({ token, user }); },
  logout: () => { localStorage.removeItem('friday_token'); localStorage.removeItem('friday_user'); set({ token: null, user: null }); },
}));
