import { FilterMatchMode, FilterService } from '@primevue/core/api'
import type { Device, DeviceConnectionFilter, Message, UpdateRecord } from '../types'
import { deviceMatchesConnectionFilter } from './labels'
import { deviceSearchHaystack } from './devices'
import { formatPublicKeyFingerprint } from './format'

/** Custom match-mode names registered with FilterService. */
export const CustomFilterMatchMode = {
  MESSAGE_TTS: 'messageTts',
  DEVICE_CONNECTION: 'deviceConnection',
  DEVICE_MODE: 'deviceMode',
  DEVICE_SEARCH: 'deviceSearch',
  UPDATE_SEARCH: 'updateSearch',
  MESSAGE_SEARCH: 'messageSearch'
} as const

let registered = false

/** Register custom FilterService rules once (idempotent). */
export function ensureCustomFiltersRegistered() {
  if (registered) return
  registered = true

  FilterService.register(CustomFilterMatchMode.MESSAGE_TTS, (value: unknown, filter: unknown) => {
    if (!filter || filter === 'all') return true
    const enabled = Boolean(value)
    if (filter === 'enabled') return enabled
    if (filter === 'disabled') return !enabled
    return true
  })

  FilterService.register(CustomFilterMatchMode.DEVICE_CONNECTION, (value: unknown, filter: unknown) => {
    // `value` is the whole Device row when used with a synthetic field accessor,
    // or we pass the device via customFilter in views. Prefer filter helpers.
    if (!filter) return true
    if (value && typeof value === 'object') {
      return deviceMatchesConnectionFilter(value as Device, filter as DeviceConnectionFilter)
    }
    return true
  })

  FilterService.register(CustomFilterMatchMode.DEVICE_MODE, (value: unknown, filter: unknown) => {
    if (!filter) return true
    const mode = value === 'listen' ? 'listen' : 'pull'
    return mode === filter
  })

  FilterService.register(CustomFilterMatchMode.DEVICE_SEARCH, (value: unknown, filter: unknown) => {
    const needle = String(filter ?? '').trim().toLowerCase()
    if (!needle) return true
    if (value && typeof value === 'object') {
      return deviceSearchHaystack(value as Device).includes(needle)
    }
    return String(value ?? '').toLowerCase().includes(needle)
  })

  FilterService.register(CustomFilterMatchMode.UPDATE_SEARCH, (value: unknown, filter: unknown) => {
    const needle = String(filter ?? '').trim().toLowerCase()
    if (!needle) return true
    if (value && typeof value === 'object') {
      const item = value as UpdateRecord
      const haystack = [
        item.update_id,
        item.version,
        item.client_version,
        item.updater_version,
        item.sha256,
        item.component,
        ...(item.components ?? []),
        item.status,
        item.status_detail,
        item.force ? 'force 强制' : ''
      ].map((part) => String(part || '').toLowerCase()).join(' ')
      return haystack.includes(needle)
    }
    return String(value ?? '').toLowerCase().includes(needle)
  })

  FilterService.register(CustomFilterMatchMode.MESSAGE_SEARCH, (value: unknown, filter: unknown) => {
    const needle = String(filter ?? '').trim().toLowerCase()
    if (!needle) return true
    if (value && typeof value === 'object') {
      const message = value as Message
      const target = (message.target_client_ids ?? []).map(formatPublicKeyFingerprint).join(' ').toLowerCase()
      return (
        message.content.toLowerCase().includes(needle) ||
        target.includes(needle) ||
        message.message_id.toLowerCase().includes(needle)
      )
    }
    return String(value ?? '').toLowerCase().includes(needle)
  })
}

export { FilterMatchMode, FilterService }

export function emptyFilter(matchMode = FilterMatchMode.EQUALS) {
  return { value: null as unknown, matchMode }
}

export function textFilter(value = '', matchMode = FilterMatchMode.CONTAINS) {
  return { value, matchMode }
}
