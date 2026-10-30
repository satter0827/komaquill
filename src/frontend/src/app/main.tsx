import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router'
import { App } from './shell'
import { PreferencesProvider } from './preferences'
import '../api/client'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <PreferencesProvider>
          <App />
        </PreferencesProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
