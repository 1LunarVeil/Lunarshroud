import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import { App } from '@/app/App';
import { AppBoundary } from '@/app/AppBoundary';
import '@/styles/app.css';

/**
 * Single QueryClient for the window.
 *
 * Retry policy: the transport already retries idempotent requests once on a
 * network failure and once on 401 (backend restarted). Query-level retries would
 * multiply that, so a failed request surfaces immediately instead of after 9
 * seconds of silent retrying.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
      staleTime: 2000,
    },
    mutations: { retry: false },
  },
});

const container = document.getElementById('root');
if (!container) {
  throw new Error('missing #root');
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AppBoundary>
        <App />
      </AppBoundary>
      <Toaster
        position="bottom-right"
        closeButton
        toastOptions={{
          className:
            'border border-[var(--border)] bg-[var(--surface)] text-[var(--text)] shadow-[var(--shadow-md)]',
        }}
      />
    </QueryClientProvider>
  </StrictMode>,
);
