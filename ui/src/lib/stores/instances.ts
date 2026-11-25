import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { instancesApi } from '@/lib/api';
import type { Instance } from '@/lib/api/types';
import { create } from 'zustand';

/**
 * Instance registry: the single source of truth for "what is running".
 *
 * Fed by `WS /ws/instances`, which pushes a full snapshot whenever any instance
 * changes state, with a 5s polling fallback for the first paint and for the case
 * where the socket is still reconnecting.
 *
 * Why a store rather than a query: several panes (sidebar, toolbar capsule,
 * dashboard, chat) all need the same list, and a WebSocket push is not a request.
 * Everything else uses TanStack Query; this is the one push-fed dataset.
 */

interface InstanceState {
  instances: Instance[];
  socketStatus: 'connecting' | 'connected' | 'reconnecting' | 'closed';
  /** Last time a snapshot arrived, for the "stale" hint in the status bar. */
  updatedAt: number | null;
  setSnapshot: (instances: Instance[]) => void;
  setSocketStatus: (status: InstanceState['socketStatus']) => void;
}

export const useInstanceStore = create<InstanceState>((set) => ({
  instances: [],
  socketStatus: 'connecting',
  updatedAt: null,
  setSnapshot: (instances) => set({ instances, updatedAt: Date.now() }),
  setSocketStatus: (socketStatus) => set({ socketStatus }),
}));

/** Selector helpers so components do not re-render on unrelated fields. */
export const selectChatInstances = (state: InstanceState): Instance[] =>
  state.instances.filter((instance) => instance.serves_chat && instance.status === 'running');

export const selectRunningCount = (state: InstanceState): number =>
  state.instances.filter((instance) => instance.status === 'running' || instance.status === 'starting').length;

function isInstancesMessage(
  message: unknown,
): message is { type: 'instances'; instances: Instance[] } {
  if (!message || typeof message !== 'object') return false;
  const record = message as { type?: unknown; instances?: unknown };
  return record.type === 'instances' && Array.isArray(record.instances);
}

/**
 * Subscribe once, at the app root.
 *
 * Kept as a hook rather than a module side effect so the socket is closed on
 * unmount (important in tests, and in React StrictMode's double-invoke).
 */
export function useInstanceFeed(): void {
  const setSnapshot = useInstanceStore((state) => state.setSnapshot);
  const setSocketStatus = useInstanceStore((state) => state.setSocketStatus);

  useEffect(() => {
    let alive = true;

    // Polling fallback: gives the first paint data even if the socket is still
    // handshaking, and recovers if the socket stays down.
    const poll = async () => {
      try {
        const list = await instancesApi.list();
        if (alive) setSnapshot(list);
      } catch {
        // The socket owns the status line; a failed poll must not clear the list.
      }
    };
    void poll();
    const timer = setInterval(poll, 5000);

    const watcher = instancesApi.watch({
      onMessage: (message: unknown) => {
        if (!alive) return;
        if (isInstancesMessage(message)) setSnapshot(message.instances);
      },
      onStatus: (status) => {
        if (!alive) return;
        setSocketStatus(status === 'connected' ? 'connected' : status === 'closed' ? 'closed' : 'reconnecting');
      },
    });

    return () => {
      alive = false;
      clearInterval(timer);
      watcher.close();
    };
  }, [setSnapshot, setSocketStatus]);
}

/** Look up one instance by id without subscribing to the whole list. */
export function useInstance(id: string | null | undefined): Instance | null {
  const instances = useInstanceStore((state) => state.instances);
  if (!id) return null;
  return instances.find((instance) => instance.id === id) ?? null;
}

/** Backend overview (CPU/RAM/GPU), polled slowly — this is ambient, not live. */
export function useSystemOverview(enabled = true) {
  return useQuery({
    queryKey: ['system', 'overview'],
    queryFn: () => import('@/lib/api').then(({ systemApi }) => systemApi.overview()),
    enabled,
    refetchInterval: 8000,
    staleTime: 4000,
  });
}
