import { defineStore } from 'pinia'

const STORAGE_KEY = 'mkcb-admin-settings'
const positions = new Set(['top-left', 'top', 'top-right', 'left', 'center', 'right', 'bottom-left', 'bottom', 'bottom-right'])

type SettingsState = {
  defaultDisplayPosition: string
  defaultDisplayDurationRatio: number
  pageSize: number
}

function readSettings(): SettingsState {
  const fallback: SettingsState = { defaultDisplayPosition: 'center', defaultDisplayDurationRatio: 0.4, pageSize: 25 }
  if (typeof window === 'undefined') return fallback
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<SettingsState>
    const position = typeof parsed.defaultDisplayPosition === 'string' && positions.has(parsed.defaultDisplayPosition)
      ? parsed.defaultDisplayPosition
      : fallback.defaultDisplayPosition
    const duration = typeof parsed.defaultDisplayDurationRatio === 'number' && Number.isFinite(parsed.defaultDisplayDurationRatio) && parsed.defaultDisplayDurationRatio >= 0
      ? Math.min(120, parsed.defaultDisplayDurationRatio)
      : fallback.defaultDisplayDurationRatio
    const pageSize = [10, 15, 20, 25, 30, 50, 100, 200].includes(Number(parsed.pageSize)) ? Number(parsed.pageSize) : fallback.pageSize
    return { defaultDisplayPosition: position, defaultDisplayDurationRatio: duration, pageSize }
  } catch {
    return fallback
  }
}

export const useSettingsStore = defineStore('settings', {
  state: readSettings,
  actions: {
    setDisplayDefaults(position: string, duration: number) {
      if (positions.has(position)) this.defaultDisplayPosition = position
      if (Number.isFinite(duration) && duration >= 0) this.defaultDisplayDurationRatio = Math.min(120, duration)
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
          defaultDisplayPosition: this.defaultDisplayPosition,
          defaultDisplayDurationRatio: this.defaultDisplayDurationRatio,
          pageSize: this.pageSize
        }))
      }
    },
    setPageSize(value: number) {
      if (![10, 15, 20, 25, 30, 50, 100, 200].includes(value)) return
      this.pageSize = value
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
          defaultDisplayPosition: this.defaultDisplayPosition,
          defaultDisplayDurationRatio: this.defaultDisplayDurationRatio,
          pageSize: this.pageSize
        }))
      }
    }
  }
})
