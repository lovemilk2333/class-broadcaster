import type {
  Device,
  DeviceConnectionFilter,
  DeviceSortKey,
  Message,
  SelectOption,
  UpdateRecord
} from '../types'

export const pageSizeOptions = [10, 15, 20, 25, 30, 50, 100, 200]

export const displayPositionOptions: SelectOption[] = [
  { label: '左上', value: 'top-left' },
  { label: '上方居中', value: 'top' },
  { label: '右上', value: 'top-right' },
  { label: '左侧居中', value: 'left' },
  { label: '正中央', value: 'center' },
  { label: '右侧居中', value: 'right' },
  { label: '左下', value: 'bottom-left' },
  { label: '下方居中', value: 'bottom' },
  { label: '右下', value: 'bottom-right' }
]

export const connectionModeOptions: SelectOption[] = [
  { label: '自动侦测与回退', value: 'auto' },
  { label: '从模式（客户端主动连接）', value: 'pull' },
  { label: '监听模式（服务端主动连接）', value: 'listen' }
]

export const messageStatusOptions: SelectOption<Message['status']>[] = [
  { label: '待投递', value: 'pending' },
  { label: '已发送', value: 'sent' },
  { label: '已过期', value: 'expired' },
  { label: '已撤回', value: 'withdrawn' }
]

export const messageTtsOptions: SelectOption<'all' | 'enabled' | 'disabled'>[] = [
  { label: '全部语音', value: 'all' },
  { label: '启用 TTS', value: 'enabled' },
  { label: '未启用 TTS', value: 'disabled' }
]

export const deviceStatusOptions: SelectOption<'pending' | 'approved' | 'revoked'>[] = [
  { label: '待批准', value: 'pending' },
  { label: '已批准', value: 'approved' },
  { label: '已撤销', value: 'revoked' }
]

export const deviceOnlineOptions: SelectOption<DeviceConnectionFilter>[] = [
  { label: '已连接', value: 'online' },
  { label: '全部未连接', value: 'offline' },
  { label: '人为终止', value: 'user_exit' },
  { label: '意外终止', value: 'unexpected' },
  { label: '管理端断开', value: 'admin' },
  { label: '更新重启中', value: 'update' },
  { label: '重连中', value: 'reconnecting' },
  { label: '未分类离线', value: 'unknown_offline' }
]

export const deviceModeFilterOptions: SelectOption<'pull' | 'listen'>[] = [
  { label: '从模式', value: 'pull' },
  { label: '监听模式', value: 'listen' }
]

export const deviceSortOptions: SelectOption<DeviceSortKey>[] = [
  { label: '登记时间（新→旧）', value: 'approved_at_desc' },
  { label: '登记时间（旧→新）', value: 'approved_at_asc' },
  { label: '最近在线（新→旧）', value: 'last_seen_desc' },
  { label: '最近在线（旧→新）', value: 'last_seen_asc' },
  { label: '名称 A→Z', value: 'label_asc' },
  { label: '名称 Z→A', value: 'label_desc' },
  { label: '已连接优先', value: 'online_first' },
  { label: '未连接优先', value: 'offline_first' }
]

export const updateStatusOptions: SelectOption<'verifying' | 'published' | 'failed'>[] = [
  { label: '校验中', value: 'verifying' },
  { label: '已发布', value: 'published' },
  { label: '校验失败', value: 'failed' }
]

export const logLevels = ['DEBUG', 'INFO', 'WARN', 'ERROR']

export function statusSeverity(status: Message['status']) {
  if (status === 'sent') return 'success'
  if (status === 'withdrawn') return 'danger'
  return status === 'pending' ? 'info' : 'warn'
}

export function statusLabel(status: Message['status']) {
  return { pending: '待投递', sent: '已发送', expired: '已过期', withdrawn: '已撤回' }[status]
}

export function deviceStatusLabel(status?: Device['status']) {
  if (status === 'approved') return '已批准'
  if (status === 'revoked') return '已取消授权'
  return '待确认'
}

export function deviceStatusSeverity(status?: Device['status']) {
  if (status === 'approved') return 'success'
  if (status === 'revoked') return 'danger'
  return 'warn'
}

export function deviceConnectionLabel(device: Device) {
  if (device.online) return '已连接'
  const label = (device.session_end_label || '').trim()
  if (label) return label
  return '未连接'
}

/** Canonical offline bucket used by the connection-status filter. */
export function deviceConnectionFilterKey(device: Device): DeviceConnectionFilter {
  if (device.online) return 'online'
  const reason = (device.session_end_reason || '').trim()
  if (reason === 'user_exit') return 'user_exit'
  if (reason === 'unexpected') return 'unexpected'
  if (reason === 'admin') return 'admin'
  if (reason === 'update') return 'update'
  if (reason === 'reconnecting') return 'reconnecting'
  return 'unknown_offline'
}

export function deviceMatchesConnectionFilter(device: Device, filter: DeviceConnectionFilter): boolean {
  if (!filter) return true
  if (filter === 'online') return !!device.online
  if (filter === 'offline') return !device.online
  return deviceConnectionFilterKey(device) === filter
}

export function deviceConnectionSeverity(device: Device) {
  if (device.online) return 'success'
  const reason = device.session_end_reason || ''
  if (reason === 'user_exit') return 'warn' // 人为终止
  if (reason === 'unexpected') return 'danger' // 意外终止
  if (reason === 'admin') return 'secondary'
  if (reason === 'update' || reason === 'reconnecting') return 'info'
  return 'secondary'
}

export function deviceConnectionTitle(device: Device, formatDateTime: (value?: number) => string) {
  if (device.online) return 'TLS 会话在线'
  const parts = [deviceConnectionLabel(device)]
  if (device.session_end_detail) parts.push(device.session_end_detail)
  if (device.session_end_at) parts.push(formatDateTime(device.session_end_at))
  return parts.join(' · ')
}

export function updateStatusLabel(status?: string) {
  if (status === 'verifying') return '校验中'
  if (status === 'published') return '已发布'
  if (status === 'failed') return '校验失败'
  if (status === 'withdrawn') return '已撤回'
  return status || '未知'
}

export function updateStatusSeverity(status?: string) {
  if (status === 'verifying') return 'warn'
  if (status === 'published') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'withdrawn') return 'secondary'
  return 'secondary'
}

export function updateStatusTitle(update: UpdateRecord) {
  if (update.status === 'failed' && update.status_detail) return update.status_detail
  if (update.status === 'verifying') return '正在校验包内 files-v1 清单 SHA-256'
  return updateStatusLabel(update.status)
}

export function updateComponentsLabel(update: UpdateRecord) {
  const items = (update.components?.length ? update.components : update.component ? [update.component] : [])
    .map((item) => {
      if (item === 'client') return '客户端'
      if (item === 'updater') return '更新器'
      if (item === 'bundle') return '客户端与更新器'
      return item
    })
  return items.length ? items.join(' + ') : '未知'
}

export function displayPositionLabel(value?: string | null) {
  const label = displayPositionOptions.find((option) => option.value === value)?.label
  return label ?? '未指定'
}

export function displayDurationLabel(value?: number | null) {
  if (value == null) return '未指定'
  return value === 0 ? '手动关闭' : `比率 ${value}`
}

export function logSeverity(level?: string) {
  switch ((level ?? '').toUpperCase()) {
    case 'ERROR': return 'danger'
    case 'WARN': return 'warn'
    case 'INFO': return 'info'
    default: return 'secondary'
  }
}
