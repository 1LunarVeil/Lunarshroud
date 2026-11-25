import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { chatsApi, instancesApi } from '@/lib/api';
import type { Conversation, ConversationDetail } from '@/lib/api/types';

/** Query keys, centralised so invalidation cannot drift from the readers. */
export const chatKeys = {
  conversations: (limit = 400) => ['chats', 'list', limit] as const,
  conversation: (id: string | null) => ['chats', 'detail', id ?? 'none'] as const,
  search: (query: string) => ['chats', 'search', query] as const,
  instances: () => ['chat', 'instances'] as const,
  promptLibrary: () => ['prompts'] as const,
};

export function useConversations(limit = 400) {
  return useQuery({
    queryKey: chatKeys.conversations(limit),
    queryFn: () => chatsApi.list(limit),
    staleTime: 3000,
  });
}

export function useConversation(id: string | null) {
  return useQuery({
    queryKey: chatKeys.conversation(id),
    queryFn: () => chatsApi.detail(id as string),
    enabled: Boolean(id),
    staleTime: 0,
  });
}

export function useChatSearch(query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: chatKeys.search(trimmed),
    queryFn: () => chatsApi.search(trimmed, 30),
    enabled: trimmed.length > 0,
    staleTime: 2000,
  });
}

/** Running instances that can accept chat completions. */
export function useChatInstances() {
  return useQuery({
    queryKey: chatKeys.instances(),
    queryFn: () => instancesApi.list(),
    staleTime: 1500,
    select: (list) =>
      list.filter(
        (instance) =>
          instance.serves_chat &&
          (instance.status === 'running' || instance.status === 'starting'),
      ),
  });
}

export function useCreateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: chatsApi.create,
    onSuccess: (conversation) => {
      client.setQueryData<Conversation[]>(
        chatKeys.conversations(),
        (current) => (current ? [conversation, ...current] : [conversation]),
      );
      void client.invalidateQueries({ queryKey: ['chats', 'list'] });
    },
  });
}

export function useUpdateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      patch,
    }: {
      id: string;
      patch: Parameters<typeof chatsApi.update>[1];
    }) => chatsApi.update(id, patch),
    onSuccess: (conversation) => {
      client.setQueryData<ConversationDetail>(
        chatKeys.conversation(conversation.id),
        (current) => (current ? { ...current, ...conversation } : current),
      );
      void client.invalidateQueries({ queryKey: ['chats', 'list'] });
    },
  });
}

export function useDeleteConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => chatsApi.remove(id),
    onSuccess: (_result, id) => {
      client.removeQueries({ queryKey: chatKeys.conversation(id) });
      void client.invalidateQueries({ queryKey: ['chats', 'list'] });
    },
  });
}

export function useBranchConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, messageId }: { id: string; messageId?: string | number }) =>
      chatsApi.branch(id, messageId === undefined ? {} : { message_id: messageId }),
    onSuccess: (conversation) => {
      client.setQueryData(chatKeys.conversation(conversation.id), conversation);
      void client.invalidateQueries({ queryKey: ['chats', 'list'] });
    },
  });
}

export function usePromptLibrary() {
  return useQuery({
    queryKey: chatKeys.promptLibrary(),
    queryFn: () => import('@/lib/api').then(({ promptsApi }) => promptsApi.list()),
    staleTime: 30_000,
  });
}
