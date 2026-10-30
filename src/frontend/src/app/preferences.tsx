import { createContext, useContext, useEffect } from 'react'
import type { ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getPreferences, savePreferences } from '../api/generated/sdk.gen'
import type { Preferences } from '../api/generated/types.gen'
import { failure } from '../api/client'
import { formatMessage, messageCatalog } from './messages'
import type { MessageKey } from './messages'

export type EffectivePreferences = Required<Preferences>
export const defaultPreferences: EffectivePreferences = {
  revision: 0,
  locale: 'ja',
  player_id: null,
  font_size: 'normal',
  density: 'comfortable',
  response_length: 'standard',
  image_frequency: 'manual',
  llm_profile: 'fake',
}

type PreferenceContext = {
  value: EffectivePreferences
  update: (changes: Partial<Preferences>) => Promise<void>
  pending: boolean
  ready: boolean
  error: unknown
}
const Context = createContext<PreferenceContext>({
  value: defaultPreferences,
  update: async () => {},
  pending: false,
  ready: true,
  error: null,
})

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const cache = useQueryClient()
  const query = useQuery({
    queryKey: ['preferences'],
    queryFn: async () => (await getPreferences()).data!,
  })
  const value = { ...defaultPreferences, ...query.data }
  const save = useMutation({
    mutationFn: async (changes: Partial<Preferences>) => {
      const current = cache.getQueryData<Preferences>(['preferences']) ?? value
      return (await savePreferences({ body: { ...current, ...changes } })).data!
    },
    onSuccess: (data) => cache.setQueryData(['preferences'], data),
    onError: () => {
      void cache.invalidateQueries({ queryKey: ['preferences'] })
    },
  })
  useEffect(() => {
    document.documentElement.lang = value.locale
    document.documentElement.dataset.fontSize = value.font_size
    document.documentElement.dataset.density = value.density
  }, [value.locale, value.font_size, value.density])
  return (
    <Context.Provider
      value={{
        value,
        update: async (changes) => {
          await save.mutateAsync(changes)
        },
        pending: save.isPending,
        ready: !!query.data,
        error: save.error ?? query.error,
      }}
    >
      {query.error && (
        <p role="alert" className="error">
          {failure(query.error, value.locale)}
        </p>
      )}
      {children}
    </Context.Provider>
  )
}

export function usePreferences() {
  return useContext(Context)
}
export function useMessages() {
  return messageCatalog(usePreferences().value.locale)
}
export function useFormatMessage() {
  const { value } = usePreferences()
  return (key: MessageKey, values: Record<string, string | number>) =>
    formatMessage(key, values, value.locale)
}
export function useFailure() {
  const { value } = usePreferences()
  return (error: unknown) => failure(error, value.locale)
}
