import { Suspense, lazy, type ComponentType } from 'react';
import { useUiStore, type PageId } from '@/lib/stores/ui';
import { LoadingState } from '@/components/ui/Feedback';

/**
 * Route → page component.
 *
 * Every page is lazy so the first paint only loads the shell plus the current
 * page. The Tauri binary embeds all chunks, but code splitting still matters: the
 * parameters page's schema renderer is heavy and must not delay a chat-first
 * launch.
 *
 * `settings` is an overlay mounted by the shell, so when it is the active route
 * the last real page stays mounted underneath. Closing the sheet then returns to a
 * live page instead of remounting it (which would refetch and reset scroll).
 */
type PageComponent = ComponentType;

const ROUTES: Record<PageId, PageComponent> = {
  chat: lazy(() => import('@/features/chat/ChatPage')),
  compare: lazy(() => import('@/features/compare/ComparePage')),
  agent: lazy(() => import('@/features/agent/AgentPage')),
  dashboard: lazy(() => import('@/features/dashboard/DashboardPage')),
  models: lazy(() => import('@/features/models/ModelsPage')),
  params: lazy(() => import('@/features/params/ParamsPage')),
  presets: lazy(() => import('@/features/presets/PresetsPage')),
  knowledge: lazy(() => import('@/features/knowledge/KnowledgePage')),
  serving: lazy(() => import('@/features/serving/ServingPage')),
  observability: lazy(() => import('@/features/observability/ObservabilityPage')),
  settings: lazy(() => import('@/features/chat/ChatPage')),
  help: lazy(() => import('@/features/help/HelpPage')),
};

export function PageHost() {
  const route = useUiStore((state) => state.route);
  const page: PageId = route === 'settings' ? 'chat' : route;
  const Component = ROUTES[page];
  return (
    <Suspense fallback={<LoadingState />}>
      <Component key={page} />
    </Suspense>
  );
}
