import { create } from 'zustand';

/**
 * Cross-page handoff state.
 *
 * 1.0 passed values between pages through `window.__pendingXxx` globals. That worked,
 * but it was invisible to the type system, ungreppable through the router, and nothing
 * cleared a value if the destination page never mounted — so a stale `__pendingModelPath`
 * could silently prefill the parameter editor on a later, unrelated navigation.
 *
 * Here each handoff is **consumed**: the destination reads it once and clears it, and a
 * value written by a page that is then abandoned is discarded the next time anything
 * else consumes the slot.
 */
interface HandoffState {
  /** Model the parameter editor should open with. */
  modelPath: string | null;
  /** Instance the chat page should switch to. */
  chatInstanceId: string | null;
  /** Ask the chat page to start a new conversation. */
  newChat: boolean;
  /** Instance the logs view should select. */
  logsInstanceId: string | null;

  requestModel: (path: string) => void;
  takeModel: () => string | null;
  requestChatInstance: (id: string) => void;
  takeChatInstance: () => string | null;
  requestNewChat: () => void;
  takeNewChat: () => boolean;
  requestLogs: (id: string) => void;
  takeLogs: () => string | null;
}

export const useHandoffStore = create<HandoffState>((set, get) => ({
  modelPath: null,
  chatInstanceId: null,
  newChat: false,
  logsInstanceId: null,

  requestModel: (path) => set({ modelPath: path }),
  takeModel: () => {
    const value = get().modelPath;
    if (value !== null) set({ modelPath: null });
    return value;
  },

  requestChatInstance: (id) => set({ chatInstanceId: id }),
  takeChatInstance: () => {
    const value = get().chatInstanceId;
    if (value !== null) set({ chatInstanceId: null });
    return value;
  },

  requestNewChat: () => set({ newChat: true }),
  takeNewChat: () => {
    const value = get().newChat;
    if (value) set({ newChat: false });
    return value;
  },

  requestLogs: (id) => set({ logsInstanceId: id }),
  takeLogs: () => {
    const value = get().logsInstanceId;
    if (value !== null) set({ logsInstanceId: null });
    return value;
  },
}));
