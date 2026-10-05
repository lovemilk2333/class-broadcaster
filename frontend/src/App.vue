<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Column from 'primevue/column'
import ConfirmDialog from 'primevue/confirmdialog'
import DataTable from 'primevue/datatable'
import DatePicker from 'primevue/datepicker'
import Dialog from 'primevue/dialog'
import { Form } from '@primevue/forms'
import type { FormSubmitEvent } from '@primevue/forms'
import InputText from 'primevue/inputtext'
import SafeInputNumber from './components/SafeInputNumber.vue'
import MultiSelect from 'primevue/multiselect'
import PanelMenu from 'primevue/panelmenu'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import Toast from 'primevue/toast'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Sidebar from 'primevue/sidebar'
import TablePagination from './components/TablePagination.vue'
import { useSettingsStore } from './stores/settings'

type Message = {
  message_id: string
  queue_seq: number
  priority: number
  content: string
  target_client_ids: string[]
  status: 'pending' | 'sent' | 'expired' | 'withdrawn'
  created_at: number
  expires_at: number
  display_position?: string | null
  display_duration_ratio?: number | null
  tts_enabled?: boolean
  deliveries?: Record<string, 'pending' | 'sent' | 'received' | 'displayed' | 'spoken' | 'failed' | 'withdrawn'>
  delivery_history?: Record<string, 'received' | 'displayed' | 'spoken' | 'failed'>
}

type Device = {
  client_id: string
  label: string
  client_version?: string
  status?: 'pending' | 'approved' | 'revoked'
  online?: boolean
  approved_at: number
  last_seen?: number
  connection_mode?: 'pull' | 'listen' | string
  requested_mode?: 'auto' | 'pull' | 'listen' | string
  forced_mode?: 'auto' | 'pull' | 'listen' | string
  listener_port?: number
  probe_sent?: number
  probe_received?: number
  probe_loss_percent?: number
  probe_min_latency_ms?: number
  probe_max_latency_ms?: number
  probe_avg_latency_ms?: number
  /** Offline cause from server: user_exit | update | admin | unexpected | reconnecting */
  session_end_reason?: string
  session_end_label?: string
  session_end_at?: number
  session_end_detail?: string
}

type ConfigForm = {
  config_id: string
  issued_at: number
  heartbeat_interval_seconds: number
  heartbeat_timeout_seconds: number
  message_ttl_hours: number
  ack_timeout_seconds: number
  max_speech_depth: number
  max_repeat_expansion: number
  default_display_position: string
  display_duration_ratio: number
  listener_probe_interval_seconds: number
  listener_probe_duration_hours: number
  listener_probe_reset_days: number
  listener_loss_threshold: number
  listener_idle_timeout_seconds: number
  immediate: boolean
}

type ServerLogEntry = {
  time?: string
  rawTime?: string
  level?: string
  msg?: string
  fields?: string
}
type UpdateRecord = {
  update_id: string
  seq?: number
  component?: string
  components?: string[]
  version?: string
  platform?: string
  client_version?: string
  updater_version?: string
  sha256: string
  status: string
  status_detail?: string
  force?: boolean
  created_at: number
}

const toast = useToast()
const confirm = useConfirm()
const settings = useSettingsStore()
const active = ref('messages')
const isMobile = ref(false)
const navigationVisible = ref(true)
const sidebarTransitionEnabled = ref(false)
const sidebarBreakpointChanging = ref(false)
let mediaQuery: MediaQueryList | undefined
let onMediaQueryChange: ((event: MediaQueryListEvent) => void) | undefined
let onHashChange: (() => void) | undefined
let onBeforeUnload: ((event: BeforeUnloadEvent) => void) | undefined
let eventSource: EventSource | undefined
const messages = ref<Message[]>([])
const devices = ref<Device[]>([])
const configForm = ref<ConfigForm>({ config_id: '', issued_at: 0, heartbeat_interval_seconds: 15, heartbeat_timeout_seconds: 45, message_ttl_hours: 24, ack_timeout_seconds: 300, max_speech_depth: 8, max_repeat_expansion: 100, default_display_position: settings.defaultDisplayPosition, display_duration_ratio: settings.defaultDisplayDurationRatio, listener_probe_interval_seconds: 60, listener_probe_duration_hours: 12, listener_probe_reset_days: 31, listener_loss_threshold: 10, listener_idle_timeout_seconds: 60, immediate: false })
const dialogVisible = ref(false)
const messagePreviewVisible = ref(false)
const previewMessage = ref<Message | null>(null)
const deviceDialogVisible = ref(false)
const editDeviceDialogVisible = ref(false)
const approveDeviceDialogVisible = ref(false)
const deviceLogsDialogVisible = ref(false)
const editingDevice = ref<Device | null>(null)
const approvingDevice = ref<Device | null>(null)
const deviceLogsDevice = ref<Device | null>(null)
const deviceLogEntries = ref<ServerLogEntry[]>([])
const deviceLogsLoading = ref(false)
const deviceLogsPage = ref(1)
const deviceLogsTotal = ref(0)
const deviceLogsJumpPage = ref<number | null>(1)
const deviceLogsTotalPages = computed(() => Math.max(1, Math.ceil(deviceLogsTotal.value / settings.pageSize)))
const editingLabel = ref('')
const editingForcedMode = ref('auto')
const approvingLabel = ref('')
const approvingForcedMode = ref('auto')
const content = ref('')
const priority = ref(1024)
const targetAll = ref(true)
const selectedTargets = ref<string[]>([])
const ttsEnabled = ref(true)
const useDefaultMessagePosition = ref(true)
const messageDisplayPosition = ref(settings.defaultDisplayPosition)
const useDefaultMessageDuration = ref(true)
const messageDisplayRatio = ref(settings.defaultDisplayDurationRatio)
const sending = ref(false)
const approving = ref(false)
const configSaving = ref(false)
const messagesLoading = ref(false)
const devicesLoading = ref(false)
const logsLoading = ref(false)
const logsTableReady = ref(false)
const configLoading = ref(false)
const preferencesLoading = ref(false)
const shutdownLoading = ref(false)
const refreshLoading = ref(false)
const publishingUpdate = ref(false)
const purgeExpiredLoading = ref(false)
/** message_id currently being withdrawn (disables that row's button). */
const withdrawingMessageId = ref<string | null>(null)
const updateDialogVisible = ref(false)
const updateTargetAll = ref(true)
const updateForce = ref(false)
const updateTargets = ref<string[]>([])
const updatePackages = ref<File[]>([])
const updates = ref<UpdateRecord[]>([])
const updatesLoading = ref(false)
const updateSearch = ref('')
const updateStatusFilter = ref<'' | 'verifying' | 'published' | 'failed'>('')
const updateStatusOptions = [
  { label: '校验中', value: 'verifying' },
  { label: '已发布', value: 'published' },
  { label: '校验失败', value: 'failed' }
]
const serverLog = ref('')
const serverLogEntries = ref<ServerLogEntry[]>([])
const serverLogPage = ref(1)
const serverLogTotal = ref(0)
const serverLogJumpPage = ref<number | null>(1)
const serverLogViewPage = ref(1)
const tableFirst = ref(0)
const tableJumpPage = ref<number | null>(1)
const messageSearch = ref('')
const messageStatusFilter = ref<Message['status'] | ''>('')
const messagePositionFilter = ref('')
const messageTtsFilter = ref<'all' | 'enabled' | 'disabled'>('all')
const publishedConfig = ref('')
const publishedConfigForm = ref<ConfigForm | null>(null)
const logStart = ref<Date | null>(null)
const logEnd = ref<Date | null>(null)
const logMinimumLevel = ref('')
const logSelectedLevels = ref<string[]>([])
const logStartInput = ref<Date | null>(null)
const logEndInput = ref<Date | null>(null)
const logMinimumLevelInput = ref('')
const logSelectedLevelsInput = ref<string[]>([])
const logLevels = ['DEBUG', 'INFO', 'WARN', 'ERROR']
const sections = ['messages', 'devices', 'updates', 'settings', 'server'] as const

function sectionFromHash() {
  let value = window.location.hash.replace(/^#/, '')
  // Legacy bookmark: #logs was renamed to #server.
  if (value === 'logs') value = 'server'
  return (sections.includes(value as typeof sections[number]) ? value : 'messages') as typeof sections[number]
}
const newClientId = ref('')
const deviceLabel = ref('')
const pendingCount = computed(() => messages.value.filter((item) => item.status === 'pending').length)
const serverTlsPort = ref(39002)
const serverHttpPort = ref(39003)
const sendFormValid = computed(() => content.value.trim().length > 0 && (targetAll.value || selectedTargets.value.length > 0))
const sendFormHint = computed(() => {
  if (!content.value.trim()) return '请输入消息内容。'
  if (!targetAll.value && selectedTargets.value.length === 0) return '请选择至少一个客户端，或勾选全部客户端。'
  return ''
})

/** Match client/core/delivery.py display_duration_ms (CJK 1.5 units, ASCII 1.0). */
function displayDurationMs(text: string, ratio: number, maximumMs = 120_000): number {
  if (ratio <= 0) return 0
  let units = 0
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0
    const isCjk =
      (code >= 0x3400 && code <= 0x4dbf) ||
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0xf900 && code <= 0xfaff)
    units += isCjk ? 1.5 : 1.0
  }
  return Math.min(maximumMs, Math.max(1000, Math.trunc(units * ratio * 1000)))
}

function formatDisplayDurationMs(ms: number): string {
  if (ms <= 0) return '需手动关闭'
  if (ms < 1000) return `${ms} 毫秒`
  const totalSeconds = ms / 1000
  if (totalSeconds < 60) {
    const rounded = Math.round(totalSeconds * 10) / 10
    return Number.isInteger(rounded) ? `${rounded} 秒` : `${rounded.toFixed(1)} 秒`
  }
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = Math.round(totalSeconds - minutes * 60)
  if (seconds <= 0) return `${minutes} 分`
  return `${minutes} 分 ${seconds} 秒`
}

const effectiveMessageDisplayRatio = computed(() => {
  const raw = useDefaultMessageDuration.value
    ? configForm.value.display_duration_ratio ?? settings.defaultDisplayDurationRatio
    : messageDisplayRatio.value ?? 0
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) ? n : 0
})

/** Live estimate shown while composing a broadcast. */
const estimatedDisplayDurationHint = computed(() => {
  const text = content.value.trim()
  const ratio = effectiveMessageDisplayRatio.value
  if (!text) return '输入内容后将实时估算客户端显示时长。'
  if (ratio <= 0) return '预计显示：需手动关闭（显示比率为 0）。'
  const ms = displayDurationMs(text, ratio)
  const note = ttsEnabled.value
    ? '（开启朗读时，实际窗口可能按语音时长再延长）'
    : ''
  return `预计显示约 ${formatDisplayDurationMs(ms)}（比率 ${ratio}）${note}`
})
const clientOptions = computed(() => sortDevicesStable(
  devices.value.filter((device) => !device.status || device.status === 'approved'),
  'approved_at_desc'
).map((device) => {
  const fingerprint = formatPublicKeyFingerprint(device.client_id)
  return { label: device.label ? `${device.label} · ${fingerprint}` : fingerprint, value: device.client_id }
}))
const deviceSearch = ref('')
const deviceStatusFilter = ref<'' | 'pending' | 'approved' | 'revoked'>('')
/** Connection / session-end filter: online + every offline classification. */
type DeviceConnectionFilter =
  | ''
  | 'online'
  | 'offline'
  | 'user_exit'
  | 'unexpected'
  | 'admin'
  | 'update'
  | 'reconnecting'
  | 'unknown_offline'
const deviceOnlineFilter = ref<DeviceConnectionFilter>('')
const deviceModeFilter = ref<'' | 'pull' | 'listen'>('')
/** Secondary sort within auth groups; pending is always pinned above approved/revoked. */
type DeviceSortKey = 'approved_at_desc' | 'approved_at_asc' | 'last_seen_desc' | 'last_seen_asc' | 'label_asc' | 'label_desc' | 'online_first' | 'offline_first'
const deviceSort = ref<DeviceSortKey>('approved_at_desc')
const deviceStatusOptions = [
  { label: '待批准', value: 'pending' },
  { label: '已批准', value: 'approved' },
  { label: '已撤销', value: 'revoked' }
]
const deviceOnlineOptions: { label: string; value: DeviceConnectionFilter }[] = [
  { label: '已连接', value: 'online' },
  { label: '全部未连接', value: 'offline' },
  { label: '人为终止', value: 'user_exit' },
  { label: '意外终止', value: 'unexpected' },
  { label: '管理端断开', value: 'admin' },
  { label: '更新重启中', value: 'update' },
  { label: '重连中', value: 'reconnecting' },
  { label: '未分类离线', value: 'unknown_offline' }
]
const deviceModeFilterOptions = [
  { label: '从模式', value: 'pull' },
  { label: '监听模式', value: 'listen' }
]
const deviceSortOptions: { label: string; value: DeviceSortKey }[] = [
  { label: '登记时间（新→旧）', value: 'approved_at_desc' },
  { label: '登记时间（旧→新）', value: 'approved_at_asc' },
  { label: '最近在线（新→旧）', value: 'last_seen_desc' },
  { label: '最近在线（旧→新）', value: 'last_seen_asc' },
  { label: '名称 A→Z', value: 'label_asc' },
  { label: '名称 Z→A', value: 'label_desc' },
  { label: '已连接优先', value: 'online_first' },
  { label: '未连接优先', value: 'offline_first' }
]
const displayPositionOptions = [
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
const connectionModeOptions = [
  { label: '自动侦测与回退', value: 'auto' },
  { label: '从模式（客户端主动连接）', value: 'pull' },
  { label: '监听模式（服务端主动连接）', value: 'listen' }
]
const messageStatusOptions = [
  { label: '待投递', value: 'pending' },
  { label: '已发送', value: 'sent' },
  { label: '已过期', value: 'expired' },
  { label: '已撤回', value: 'withdrawn' }
]
const messageTtsOptions = [
  { label: '全部语音', value: 'all' },
  { label: '启用 TTS', value: 'enabled' },
  { label: '未启用 TTS', value: 'disabled' }
]
const pageSizeOptions = [10, 15, 20, 25, 30, 50, 100, 200]
const backendLogPageSize = 100
const filteredUpdates = computed(() => {
  const needle = updateSearch.value.trim().toLowerCase()
  return updates.value.filter((item) => {
    if (updateStatusFilter.value && item.status !== updateStatusFilter.value) return false
    if (!needle) return true
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
  })
})
function deviceActivityTs(device: Device): number {
  if (device.last_seen && device.last_seen > 0) return device.last_seen
  return device.approved_at || 0
}

/** pending first (authorize), then approved, then revoked — always, so new clients are not buried. */
function deviceAuthRank(device: Device): number {
  const status = device.status || 'approved'
  if (status === 'pending') return 0
  if (status === 'approved') return 1
  return 2
}

function compareDevices(a: Device, b: Device, key: DeviceSortKey): number {
  const byId = () => String(a.client_id || '').localeCompare(String(b.client_id || ''))
  const byAuth = deviceAuthRank(a) - deviceAuthRank(b)
  if (byAuth !== 0) return byAuth
  switch (key) {
    case 'approved_at_asc': {
      const d = (a.approved_at || 0) - (b.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'approved_at_desc': {
      // Pending have approved_at=0; within approved group newest first.
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'last_seen_asc': {
      const d = deviceActivityTs(a) - deviceActivityTs(b)
      return d !== 0 ? d : byId()
    }
    case 'last_seen_desc': {
      const d = deviceActivityTs(b) - deviceActivityTs(a)
      return d !== 0 ? d : byId()
    }
    case 'label_asc': {
      const la = (a.label || '').trim().toLowerCase()
      const lb = (b.label || '').trim().toLowerCase()
      const d = la.localeCompare(lb, 'zh-CN')
      return d !== 0 ? d : byId()
    }
    case 'label_desc': {
      const la = (a.label || '').trim().toLowerCase()
      const lb = (b.label || '').trim().toLowerCase()
      const d = lb.localeCompare(la, 'zh-CN')
      return d !== 0 ? d : byId()
    }
    case 'online_first': {
      const oa = a.online ? 1 : 0
      const ob = b.online ? 1 : 0
      if (oa !== ob) return ob - oa
      // Keep secondary order stable (approved_at desc).
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'offline_first': {
      const oa = a.online ? 1 : 0
      const ob = b.online ? 1 : 0
      if (oa !== ob) return oa - ob
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    default:
      return byId()
  }
}

function sortDevicesStable(items: Device[], key: DeviceSortKey = 'approved_at_desc'): Device[] {
  // Pending always first for quick approve; then the chosen secondary key.
  // Heartbeats must not reshuffle: secondary keys avoid last_seen by default.
  return [...items].sort((a, b) => compareDevices(a, b, key))
}

const filteredDevices = computed(() => {
  const needle = deviceSearch.value.trim().toLowerCase()
  const filtered = devices.value.filter((device) => {
    const status = device.status || 'approved'
    if (deviceStatusFilter.value && status !== deviceStatusFilter.value) return false
    if (!deviceMatchesConnectionFilter(device, deviceOnlineFilter.value)) return false
    const mode = device.connection_mode === 'listen' ? 'listen' : 'pull'
    if (deviceModeFilter.value && mode !== deviceModeFilter.value) return false
    if (!needle) return true
    const fingerprint = formatPublicKeyFingerprint(device.client_id).toLowerCase()
    const connectionLabel = deviceConnectionLabel(device)
    const haystack = [
      device.label,
      device.client_id,
      fingerprint,
      device.client_version,
      status,
      device.connection_mode,
      device.requested_mode,
      device.forced_mode,
      device.online ? 'online connected 已连接' : 'offline 未连接',
      connectionLabel,
      device.session_end_reason,
      device.session_end_detail,
      device.session_end_label
    ].map((part) => String(part || '').toLowerCase()).join(' ')
    return haystack.includes(needle)
  })
  return sortDevicesStable(filtered, deviceSort.value)
})
const tableListTotal = computed(() => {
  if (active.value === 'devices') return filteredDevices.value.length
  if (active.value === 'updates') return filteredUpdates.value.length
  return filteredMessages.value.length
})
const tableTotalPages = computed(() => Math.max(1, Math.ceil(tableListTotal.value / settings.pageSize)))
const tablePage = computed(() => Math.min(tableTotalPages.value, Math.floor(tableFirst.value / settings.pageSize) + 1))
const pagedMessages = computed(() => filteredMessages.value.slice(tableFirst.value, tableFirst.value + settings.pageSize))
const pagedDevices = computed(() => filteredDevices.value.slice(tableFirst.value, tableFirst.value + settings.pageSize))
const pagedUpdates = computed(() => filteredUpdates.value.slice(tableFirst.value, tableFirst.value + settings.pageSize))
const serverLogTotalPages = computed(() => Math.max(1, Math.ceil(serverLogTotal.value / settings.pageSize)))
const visibleServerLogEntries = computed(() => {
  const globalFirst = (serverLogViewPage.value - 1) * settings.pageSize
  const backendFirst = (serverLogPage.value - 1) * backendLogPageSize
  const localFirst = globalFirst - backendFirst
  if (localFirst < 0 || localFirst >= serverLogEntries.value.length) return []
  return serverLogEntries.value.slice(localFirst, localFirst + settings.pageSize)
})

function configComparable(value: ConfigForm) {
  return JSON.stringify({
    heartbeat_interval_seconds: value.heartbeat_interval_seconds,
    heartbeat_timeout_seconds: value.heartbeat_timeout_seconds,
    message_ttl_hours: value.message_ttl_hours,
    ack_timeout_seconds: value.ack_timeout_seconds,
    max_speech_depth: value.max_speech_depth,
    max_repeat_expansion: value.max_repeat_expansion,
    default_display_position: value.default_display_position,
    display_duration_ratio: value.display_duration_ratio,
    listener_probe_interval_seconds: value.listener_probe_interval_seconds,
    listener_probe_duration_hours: value.listener_probe_duration_hours,
    listener_probe_reset_days: value.listener_probe_reset_days,
    listener_loss_threshold: value.listener_loss_threshold,
    listener_idle_timeout_seconds: value.listener_idle_timeout_seconds
  })
}

const configDirty = computed(() => Boolean(publishedConfig.value) && configComparable(configForm.value) !== publishedConfig.value)

const serverVersion = ref('')
const serverBuildDate = ref('')

const serverVersionLabel = computed(() => {
  if (!serverVersion.value) return '未知'
  if (!serverBuildDate.value || serverBuildDate.value === 'dev') return `${serverVersion.value} (dev)`
  return `${serverVersion.value} (${serverBuildDate.value})`
})

async function loadHealth() {
  try {
    const response = await fetch('/api/v1/health')
    if (!response.ok) return
    const data = await response.json() as {
      tls_port?: number
      http_port?: number
      version?: string
      build_date?: string
    }
    if (typeof data.tls_port === 'number' && data.tls_port > 0) serverTlsPort.value = data.tls_port
    if (typeof data.http_port === 'number' && data.http_port > 0) serverHttpPort.value = data.http_port
    if (typeof data.version === 'string' && data.version) serverVersion.value = data.version
    if (typeof data.build_date === 'string' && data.build_date) serverBuildDate.value = data.build_date
  } catch {
    // Keep defaults when health is unavailable.
  }
}

async function loadMessages() {
  messagesLoading.value = true
  try {
    const response = await fetch('/api/v1/messages')
    if (!response.ok) throw new Error('加载消息失败')
    const data = await response.json()
    messages.value = data.items ?? []
  } finally {
    messagesLoading.value = false
  }
}

const filteredMessages = computed(() => {
  const query = messageSearch.value.trim().toLowerCase()
  return messages.value.filter((message) => {
    if (messageStatusFilter.value && message.status !== messageStatusFilter.value) return false
    if (messagePositionFilter.value && message.display_position !== messagePositionFilter.value) return false
    if (messageTtsFilter.value === 'enabled' && !message.tts_enabled) return false
    if (messageTtsFilter.value === 'disabled' && message.tts_enabled) return false
    if (!query) return true
    const target = (message.target_client_ids ?? []).join(' ').toLowerCase()
    return message.content.toLowerCase().includes(query) || target.includes(query) || message.message_id.toLowerCase().includes(query)
  })
})

async function loadDevices() {
  devicesLoading.value = true
  try {
    const response = await fetch('/api/v1/devices')
    if (!response.ok) throw new Error('加载客户端失败')
    const data = await response.json()
    // Keep server order as-is; filteredDevices applies the chosen sort.
    devices.value = Array.isArray(data.items) ? data.items : []
    const maxFirst = Math.max(0, (Math.ceil(filteredDevices.value.length / Math.max(1, settings.pageSize)) - 1) * settings.pageSize)
    if (tableFirst.value > maxFirst) {
      tableFirst.value = maxFirst
      tableJumpPage.value = Math.floor(maxFirst / settings.pageSize) + 1
    }
  } finally {
    devicesLoading.value = false
  }
}

function updatePackageKey(file: File): string {
  return `${file.name}\0${file.size}\0${file.lastModified}`
}

function formatUpdatePackageSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function publishUpdate() {
  // Snapshot options once at submit so mid-upload UI edits cannot change the batch
  // or make the dialog show a different force/target than what was actually sent.
  if (publishingUpdate.value) return
  const force = updateForce.value
  const targetAll = updateTargetAll.value
  const targets = force || targetAll
    ? clientOptions.value.map((item) => item.value)
    : [...updateTargets.value]
  const packages = [...updatePackages.value]
  if (!force && targets.length === 0) {
    toast.add({ severity: 'warn', summary: '请选择至少一个已批准客户端', life: 2600 })
    return
  }
  if (force && clientOptions.value.length === 0) {
    toast.add({ severity: 'warn', summary: '强制更新需要至少一个已批准客户端', life: 2600 })
    return
  }
  if (packages.length === 0) {
    toast.add({ severity: 'warn', summary: '请选择至少一个更新包', life: 2600 })
    return
  }
  publishingUpdate.value = true
  let published = 0
  let totalSent = 0
  let totalOffline = 0
  const failures: string[] = []
  try {
    for (const file of packages) {
      try {
        const form = new FormData()
        if (force) {
          form.append('force', 'true')
        } else {
          targets.forEach((target) => form.append('target_client_ids', target))
        }
        form.append('package', file)
        const response = await fetch('/api/v1/updates/publish', {
          method: 'POST',
          body: form
        })
        const result = await response.json().catch(() => ({}))
        // 200 OK (sync) and 202 Accepted (async verify) both count as accepted upload.
        if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : '发布更新失败')
        published += 1
        totalSent += Array.isArray(result.sent) ? result.sent.length : 0
        totalOffline += Array.isArray(result.offline) ? result.offline.length : 0
      } catch (error) {
        failures.push(`${file.name}: ${error instanceof Error ? error.message : '发布失败'}`)
      }
    }
    if (published > 0) {
      const severity = failures.length || totalOffline ? 'warn' : 'success'
      const failureHint = failures.length ? `，失败 ${failures.length} 个` : ''
      const forceHint = force ? '（强制：全部客户端必须更新）' : ''
      toast.add({
        severity,
        summary: `已上传 ${published}/${packages.length} 个更新包${forceHint}${failureHint}`,
        detail: totalSent || totalOffline
          ? `推送 ${totalSent} 个在线客户端，${totalOffline} 个离线${force ? '；离线客户端上线后会自动补推' : ''}`
          : '后台校验 SHA-256，通过后自动推送；失败会在状态列显示',
        life: 4800
      })
    }
    if (failures.length > 0) {
      toast.add({
        severity: 'error',
        summary: published === 0 ? '全部更新包发布失败' : '部分更新包发布失败',
        detail: failures.slice(0, 3).join('；'),
        life: 5200
      })
    }
    if (failures.length === 0) {
      updateDialogVisible.value = false
      updateTargetAll.value = true
      updateForce.value = false
      updateTargets.value = []
      updatePackages.value = []
    } else if (published > 0) {
      // Keep failed files so the admin can retry without re-picking successes.
      const failedKeys = new Set(
        packages
          .filter((file) => failures.some((item) => item.startsWith(`${file.name}: `)))
          .map(updatePackageKey)
      )
      updatePackages.value = packages.filter((file) => failedKeys.has(updatePackageKey(file)))
    }
    await loadUpdates()
  } finally {
    publishingUpdate.value = false
  }
}

const updatePackageDragOver = ref(false)
const updatePackageInputRef = ref<HTMLInputElement | null>(null)

function isAcceptedUpdatePackage(file: File): boolean {
  const name = file.name.toLowerCase()
  return name.endsWith('.tar.zst') || name.endsWith('.tar.zstd')
}

function addUpdatePackages(files: File[] | FileList | null | undefined) {
  if (publishingUpdate.value) return
  if (!files || files.length === 0) return
  const existing = new Set(updatePackages.value.map(updatePackageKey))
  const accepted: File[] = []
  let rejected = 0
  let duplicated = 0
  for (const file of Array.from(files)) {
    if (!isAcceptedUpdatePackage(file)) {
      rejected += 1
      continue
    }
    const key = updatePackageKey(file)
    if (existing.has(key)) {
      duplicated += 1
      continue
    }
    existing.add(key)
    accepted.push(file)
  }
  if (accepted.length > 0) {
    updatePackages.value = [...updatePackages.value, ...accepted]
  }
  if (rejected > 0) {
    toast.add({ severity: 'warn', summary: `已忽略 ${rejected} 个非 .tar.zst 文件`, life: 2800 })
  }
  if (duplicated > 0) {
    toast.add({ severity: 'info', summary: `已跳过 ${duplicated} 个重复文件`, life: 2400 })
  }
}

function onUpdatePackageSelect(event: Event) {
  const input = event.target as HTMLInputElement | null
  if (publishingUpdate.value) {
    if (input) input.value = ''
    return
  }
  addUpdatePackages(input?.files)
  // Allow selecting the same file again later; list state is owned by updatePackages.
  if (input) input.value = ''
}

function openUpdatePackagePicker() {
  if (publishingUpdate.value) return
  updatePackageInputRef.value?.click()
}

function onUpdatePackageDragEnter(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  if (publishingUpdate.value) return
  updatePackageDragOver.value = true
}

function onUpdatePackageDragOver(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  if (publishingUpdate.value) {
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'none'
    return
  }
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
  updatePackageDragOver.value = true
}

function onUpdatePackageDragLeave(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  const current = event.currentTarget as HTMLElement | null
  const related = event.relatedTarget as Node | null
  // Ignore leave events that stay inside the drop zone (child nodes).
  if (current && related && current.contains(related)) return
  updatePackageDragOver.value = false
}

function onUpdatePackageDrop(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  updatePackageDragOver.value = false
  if (publishingUpdate.value) return
  addUpdatePackages(event.dataTransfer?.files)
}

function removeUpdatePackage(index: number) {
  if (publishingUpdate.value) return
  if (index < 0 || index >= updatePackages.value.length) return
  updatePackages.value = updatePackages.value.filter((_, i) => i !== index)
}

function clearUpdatePackages() {
  if (publishingUpdate.value) return
  updatePackages.value = []
}

function openUpdateDialog() {
  if (publishingUpdate.value) return
  updateTargetAll.value = true
  updateForce.value = false
  updateTargets.value = []
  updatePackages.value = []
  updatePackageDragOver.value = false
  updateDialogVisible.value = true
}

async function loadUpdates() {
  updatesLoading.value = true
  try {
    const response = await fetch('/api/v1/updates')
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(typeof body.error === 'string' ? body.error : '加载更新记录失败')
    }
    const data = await response.json()
    const items = Array.isArray(data.items) ? data.items : []
    updates.value = items
    // Keep pagination inside bounds after reload (e.g. fewer rows after withdraw).
    const maxFirst = Math.max(0, (Math.ceil(items.length / Math.max(1, settings.pageSize)) - 1) * settings.pageSize)
    if (tableFirst.value > maxFirst) {
      tableFirst.value = maxFirst
      tableJumpPage.value = Math.floor(maxFirst / settings.pageSize) + 1
    }
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '加载更新记录失败', life: 3000 })
  } finally {
    updatesLoading.value = false
  }
}

function logDateParam(value: Date | null) {
  if (!value) return ''
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function parseServerLogEntries(content: string): ServerLogEntry[] {
  return content.split(/\r?\n/).filter(Boolean).map((line: string) => {
    try {
      const parsed = JSON.parse(line) as Record<string, unknown>
      const fields = Object.entries(parsed)
        .filter(([key]) => !['time', 'level', 'msg'].includes(key))
        .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
        .join(' ')
      const time = String(parsed.time ?? '')
      return { rawTime: time, time: time ? formatLogTime(time) : '', level: String(parsed.level ?? '').toUpperCase(), msg: String(parsed.msg ?? ''), fields }
    } catch {
      return { msg: line }
    }
  })
}

async function fetchLogPage(page: number) {
  const requestedPage = Math.max(1, Math.trunc(page))
  // 日志采用两级分页：服务端固定一次取 100 条，前端再按共享页大小切片。
  const params = new URLSearchParams({ page: String(requestedPage), page_size: String(backendLogPageSize) })
  const start = logDateParam(logStart.value)
  const end = logDateParam(logEnd.value)
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  if (logMinimumLevel.value) params.set('minimum_level', logMinimumLevel.value)
  if (logSelectedLevels.value.length) params.set('levels', logSelectedLevels.value.join(','))
  const response = await fetch(`/api/v1/logs?${params.toString()}`)
  if (!response.ok) throw new Error('加载服务端日志失败')
  const data = await response.json()
  return {
    page: Number(data.page) || requestedPage,
    total: Number(data.total) || 0,
    content: String(data.content ?? ''),
    entries: parseServerLogEntries(String(data.content ?? ''))
  }
}

async function loadLogViewPage(page: number, force = false) {
  const totalPages = serverLogTotalPages.value
  const requestedPage = Math.min(totalPages, Math.max(1, Math.trunc(page) || 1))
  const globalFirst = (requestedPage - 1) * settings.pageSize
  const backendPage = Math.floor(globalFirst / backendLogPageSize) + 1
  serverLogViewPage.value = requestedPage
  serverLogJumpPage.value = requestedPage
  const backendLastPage = Math.floor((globalFirst + settings.pageSize - 1) / backendLogPageSize) + 1
  if (force || backendPage !== serverLogPage.value || backendLastPage !== serverLogPage.value || serverLogEntries.value.length === 0) {
    logsLoading.value = true
    try {
      const pages = Array.from({ length: backendLastPage - backendPage + 1 }, (_, index) => backendPage + index)
      const results = await Promise.all(pages.map((backend) => fetchLogPage(backend)))
      serverLogPage.value = backendPage
      serverLogTotal.value = results[0]?.total ?? 0
      serverLog.value = results.map((result) => result.content).filter(Boolean).join('\n')
      serverLogEntries.value = results.flatMap((result) => result.entries)
    } finally {
      logsLoading.value = false
    }
  }
}

async function loadPreferences() {
  preferencesLoading.value = true
  try {
    const response = await fetch('/api/v1/preferences')
    if (!response.ok) throw new Error('加载服务端偏好失败')
    const data = await response.json()
    if (pageSizeOptions.includes(Number(data.page_size))) settings.setPageSize(Number(data.page_size))
  } finally {
    preferencesLoading.value = false
  }
}

async function updatePageSize(value: number) {
  if (!pageSizeOptions.includes(value) || value === settings.pageSize) return
  settings.setPageSize(value)
  try {
    const response = await fetch('/api/v1/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page_size: value })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '保存分页大小失败')
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '保存分页大小失败', life: 3200 })
  }
}

function setTablePage(page: number) {
  const bounded = Math.min(tableTotalPages.value, Math.max(1, Math.trunc(page) || 1))
  tableJumpPage.value = bounded
  tableFirst.value = (bounded - 1) * settings.pageSize
}

function jumpTablePage() {
  const page = Math.min(tableTotalPages.value, Math.max(1, Math.trunc(Number(tableJumpPage.value) || 1)))
  tableJumpPage.value = page
  tableFirst.value = (page - 1) * settings.pageSize
}

async function changeTablePageSize(value: number) {
  const first = tableFirst.value
  await updatePageSize(value)
  tableFirst.value = Math.floor(first / value) * value
}

function jumpLogPage() {
  void loadLogViewPage(Number(serverLogJumpPage.value) || 1)
}

async function changeLogPageSize(value: number) {
  if (!pageSizeOptions.includes(value) || value === settings.pageSize) return
  const first = (serverLogViewPage.value - 1) * settings.pageSize
  await updatePageSize(value)
  await loadLogViewPage(Math.floor(first / value) + 1, true)
}

async function changeDeviceLogPageSize(value: number) {
  if (!deviceLogsDevice.value || !pageSizeOptions.includes(value) || value === settings.pageSize) return
  const first = (deviceLogsPage.value - 1) * settings.pageSize
  await updatePageSize(value)
  await openDeviceLogs(deviceLogsDevice.value, Math.floor(first / value) + 1)
}

const filteredServerLogEntries = computed(() => {
  // 日期、级别筛选已在服务端执行；这里仅切出当前前端页，避免分页后重复过滤造成页内缺行。
  return visibleServerLogEntries.value
})

function applyLogFilters() {
  logStart.value = logStartInput.value
  logEnd.value = logEndInput.value
  logMinimumLevel.value = logMinimumLevelInput.value
  logSelectedLevels.value = [...logSelectedLevelsInput.value]
  serverLogViewPage.value = 1
  if (active.value === 'server') void loadLogViewPage(1, true)
}

function resetLogFilters() {
  logStartInput.value = null
  logEndInput.value = null
  logMinimumLevelInput.value = ''
  logSelectedLevelsInput.value = []
  applyLogFilters()
}

function logSeverity(level?: string) {
  switch ((level ?? '').toUpperCase()) {
    case 'ERROR': return 'danger'
    case 'WARN': return 'warn'
    case 'INFO': return 'info'
    default: return 'secondary'
  }
}

async function withdrawMessage(message: Message) {
  if (withdrawingMessageId.value) return
  withdrawingMessageId.value = message.message_id
  try {
    const response = await fetch(`/api/v1/messages/${message.message_id}/withdraw`, { method: 'POST' })
    if (!response.ok) throw new Error((await response.json()).error ?? '撤回失败')
    await loadMessages()
    toast.add({ severity: 'success', summary: '消息已撤回', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '撤回失败', life: 3200 })
  } finally {
    withdrawingMessageId.value = null
  }
}

async function purgeExpiredHistory() {
  confirm.require({
    message: '将立即删除创建时间已满 185 天的消息记录，以及超过 185 天未活跃的客户端记录。此操作不可撤销，是否继续？',
    header: '清理过期数据',
    icon: 'pi pi-exclamation-triangle',
    acceptLabel: '立即清理',
    rejectLabel: '取消',
    acceptClass: 'p-button-danger',
    accept: async () => {
      purgeExpiredLoading.value = true
      try {
        const response = await fetch('/api/v1/maintenance/purge-expired', { method: 'POST' })
        if (!response.ok) throw new Error('清理失败')
        const data = await response.json()
        const messagesRemoved = Number(data.messages_removed ?? 0)
        const devicesRemoved = Number(data.devices_removed ?? 0)
        toast.add({
          severity: 'success',
          summary: '清理完成',
          detail: `已删除 ${messagesRemoved} 条消息、${devicesRemoved} 个客户端记录`,
          life: 4200
        })
        await Promise.all([loadMessages().catch(() => undefined), loadDevices().catch(() => undefined)])
      } catch (error) {
        toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '清理失败', life: 3600 })
      } finally {
        purgeExpiredLoading.value = false
      }
    }
  })
}

async function requestServerShutdown() {
  confirm.require({
    message: '确定关闭服务端并向所有客户端发送关闭事件吗？',
    header: '关闭服务端',
    icon: 'pi pi-power-off',
    acceptLabel: '关闭服务端',
    rejectLabel: '取消',
    acceptClass: 'p-button-danger',
    accept: async () => {
      shutdownLoading.value = true
      try {
        const response = await fetch('/api/v1/shutdown', { method: 'POST' })
        if (!response.ok) throw new Error((await response.json()).error ?? '关闭服务端失败')
        toast.add({ severity: 'info', summary: '已发送服务端关闭事件', life: 2600 })
      } catch (error) {
        toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '关闭服务端失败', life: 3200 })
      } finally {
        shutdownLoading.value = false
      }
    }
  })
}

async function loadConfig(force = false) {
  // SSE 配置事件可能在用户编辑期间到达；保留本地未发布草稿，避免远端刷新覆盖输入。
  if (configDirty.value && !force) return
  configLoading.value = true
  try {
    const response = await fetch('/api/v1/config')
    if (!response.ok) throw new Error('加载配置失败')
    const data = await response.json()
    configForm.value = { ...configForm.value, ...data, immediate: false }
    publishedConfigForm.value = { ...configForm.value }
    publishedConfig.value = configComparable(configForm.value)
    settings.setDisplayDefaults(configForm.value.default_display_position, configForm.value.display_duration_ratio)
    if (useDefaultMessagePosition.value) messageDisplayPosition.value = settings.defaultDisplayPosition
    if (useDefaultMessageDuration.value) messageDisplayRatio.value = settings.defaultDisplayDurationRatio
  } finally {
    configLoading.value = false
  }
}

async function saveConfig() {
  configSaving.value = true
  try {
    const response = await fetch('/api/v1/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        heartbeat_interval_seconds: configForm.value.heartbeat_interval_seconds,
        heartbeat_timeout_seconds: configForm.value.heartbeat_timeout_seconds,
        message_ttl_hours: configForm.value.message_ttl_hours,
        ack_timeout_seconds: configForm.value.ack_timeout_seconds,
        max_speech_depth: configForm.value.max_speech_depth,
        max_repeat_expansion: configForm.value.max_repeat_expansion,
        default_display_position: configForm.value.default_display_position,
        default_display_duration_ratio: configForm.value.display_duration_ratio,
        listener_probe_interval_seconds: configForm.value.listener_probe_interval_seconds,
        listener_probe_duration_hours: configForm.value.listener_probe_duration_hours,
        listener_probe_reset_days: configForm.value.listener_probe_reset_days,
        listener_loss_threshold: configForm.value.listener_loss_threshold,
        listener_idle_timeout_seconds: configForm.value.listener_idle_timeout_seconds,
        immediate: configForm.value.immediate
      })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '保存配置失败')
    const data = await response.json()
    configForm.value = { ...configForm.value, ...data.config, immediate: false }
    publishedConfigForm.value = { ...configForm.value }
    publishedConfig.value = configComparable(configForm.value)
    settings.setDisplayDefaults(configForm.value.default_display_position, configForm.value.display_duration_ratio)
    if (useDefaultMessagePosition.value) messageDisplayPosition.value = settings.defaultDisplayPosition
    if (useDefaultMessageDuration.value) messageDisplayRatio.value = settings.defaultDisplayDurationRatio
    toast.add({ severity: 'success', summary: '配置已发布', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '保存配置失败', life: 3200 })
  } finally {
    configSaving.value = false
  }
}

function discardConfigChanges() {
  if (publishedConfigForm.value) configForm.value = { ...publishedConfigForm.value }
}

async function approveDevice(event: FormSubmitEvent<Record<string, any>>) {
  if (!event.valid || !event.values.clientId?.trim()) return
  approving.value = true
  try {
    const response = await fetch('/api/v1/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: event.values.clientId.trim(), label: event.values.label?.trim() ?? '', forced_mode: approvingForcedMode.value })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '批准失败')
    deviceDialogVisible.value = false
    newClientId.value = ''
    deviceLabel.value = ''
    approvingForcedMode.value = 'auto'
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端已批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '批准失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

function openManualApproveDevice() {
  approvingForcedMode.value = 'auto'
  deviceDialogVisible.value = true
}

function openApproveDevice(device: Device) {
  approvingDevice.value = device
  approvingLabel.value = device.label ?? ''
  approvingForcedMode.value = device.forced_mode || 'auto'
  approveDeviceDialogVisible.value = true
}

async function approvePendingDevice() {
  if (!approvingDevice.value) return
  approving.value = true
  try {
    const response = await fetch('/api/v1/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: approvingDevice.value.client_id, label: approvingLabel.value.trim(), forced_mode: approvingForcedMode.value })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '批准失败')
    approveDeviceDialogVisible.value = false
    approvingDevice.value = null
    approvingLabel.value = ''
    approvingForcedMode.value = 'auto'
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端已批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '批准失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

function openEditDevice(device: Device) {
  editingDevice.value = device
  editingLabel.value = device.label ?? ''
  editingForcedMode.value = device.forced_mode || 'auto'
  editDeviceDialogVisible.value = true
}

async function renameDevice() {
  if (!editingDevice.value) return
  approving.value = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(editingDevice.value.client_id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: editingLabel.value.trim(), forced_mode: editingForcedMode.value })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '重命名失败')
    editDeviceDialogVisible.value = false
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端名称已更新', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '重命名失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

function revokeDevice(device: Device) {
  confirm.require({
    message: `确定取消批准客户端“${device.label || formatPublicKeyFingerprint(device.client_id)}”？`,
    header: '取消批准客户端',
    icon: 'pi pi-exclamation-triangle',
    rejectLabel: '取消',
    acceptLabel: '取消批准',
    acceptClass: 'p-button-danger',
    accept: () => void revokeDeviceConfirmed(device)
  })
}

async function revokeDeviceConfirmed(device: Device) {
  approving.value = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(device.client_id)}`, { method: 'DELETE' })
    if (!response.ok) throw new Error((await response.json()).error ?? '取消批准失败')
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端已取消批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '取消批准失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

function disconnectDevice(device: Device) {
  confirm.require({
    message: `确定断开客户端“${device.label || formatPublicKeyFingerprint(device.client_id)}”吗？`,
    header: '断开客户端',
    icon: 'pi pi-power-off',
    rejectLabel: '取消',
    acceptLabel: '断开连接',
    acceptClass: 'p-button-danger',
    accept: () => void disconnectDeviceConfirmed(device)
  })
}

async function disconnectDeviceConfirmed(device: Device) {
  approving.value = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(device.client_id)}/disconnect`, { method: 'POST' })
    if (!response.ok) throw new Error((await response.json()).error ?? '断开连接失败')
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端已断开连接', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '断开连接失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

async function openDeviceLogs(device: Device, page = 1) {
  deviceLogsDevice.value = device
  deviceLogEntries.value = []
  deviceLogsPage.value = Math.min(deviceLogsTotalPages.value, Math.max(1, Math.trunc(page) || 1))
  deviceLogsJumpPage.value = deviceLogsPage.value
  deviceLogsDialogVisible.value = true
  deviceLogsLoading.value = true
  try {
    const params = new URLSearchParams({ page: String(deviceLogsPage.value), page_size: String(settings.pageSize) })
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(device.client_id)}/logs?${params.toString()}`)
    if (!response.ok) throw new Error((await response.json()).error ?? '加载客户端日志失败')
    const data = await response.json()
    deviceLogEntries.value = parseServerLogEntries(String(data.content ?? ''))
    deviceLogsTotal.value = Number(data.total) || 0
    deviceLogsPage.value = Number(data.page) || deviceLogsPage.value
    deviceLogsJumpPage.value = deviceLogsPage.value
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '加载客户端日志失败', life: 3200 })
  } finally {
    deviceLogsLoading.value = false
  }
}

function jumpDeviceLogPage() {
  if (!deviceLogsDevice.value) return
  void openDeviceLogs(deviceLogsDevice.value, Number(deviceLogsJumpPage.value) || 1)
}

function changeDeviceLogPage(page: number) {
  if (deviceLogsDevice.value) void openDeviceLogs(deviceLogsDevice.value, page)
}

async function refreshActive() {
  if (active.value === 'settings' && configDirty.value) {
    confirm.require({
      message: '配置已修改但尚未保存',
      header: '未保存的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '保存',
      rejectLabel: '丢弃',
      rejectClass: 'p-button-text p-button-secondary',
      accept: async () => {
        await saveConfig()
        if (!configDirty.value) await loadConfig()
      },
      reject: () => discardConfigChanges()
    })
    return
  }
  refreshLoading.value = true
  try {
    if (active.value === 'devices') await loadDevices()
    else if (active.value === 'updates') { await loadDevices(); await loadUpdates() }
    else if (active.value === 'settings') await loadConfig()
    else if (active.value === 'server') await loadLogViewPage(serverLogViewPage.value, true)
    else await loadMessages()
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '刷新失败', life: 3200 })
  } finally {
    refreshLoading.value = false
  }
}

async function sendMessage(event: FormSubmitEvent<Record<string, any>>) {
  if (!event.valid || !event.values.content?.trim()) return
  const targetClientIDs = event.values.target_all
    ? []
    : (Array.isArray(event.values.target_client_ids) ? event.values.target_client_ids : [])
  if (!event.values.target_all && targetClientIDs.length === 0) {
    toast.add({ severity: 'warn', summary: '请选择至少一个目标客户端，或勾选全部客户端', life: 3200 })
    return
  }
  sending.value = true
  try {
    const response = await fetch('/api/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: event.values.content,
        priority: event.values.priority ?? 1024,
        target_client_ids: targetClientIDs,
        display_position: useDefaultMessagePosition.value ? null : messageDisplayPosition.value,
        display_duration_ratio: useDefaultMessageDuration.value ? null : messageDisplayRatio.value,
        speech: event.values.tts_enabled
          ? [{ type: 'text', value: event.values.content.trim() }]
          : []
      })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '发送失败')
    dialogVisible.value = false
    content.value = ''
    targetAll.value = true
    selectedTargets.value = []
    ttsEnabled.value = true
    useDefaultMessagePosition.value = true
    useDefaultMessageDuration.value = true
    await loadMessages()
    toast.add({ severity: 'success', summary: '已加入队列', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '发送失败', life: 3200 })
  } finally {
    sending.value = false
  }
}

function statusSeverity(status: Message['status']) {
  if (status === 'sent') return 'success'
  if (status === 'withdrawn') return 'danger'
  return status === 'pending' ? 'info' : 'warn'
}

function deviceStatusLabel(status?: Device['status']) {
  if (status === 'approved') return '已批准'
  if (status === 'revoked') return '已取消授权'
  return '待确认'
}

function deviceStatusSeverity(status?: Device['status']) {
  if (status === 'approved') return 'success'
  if (status === 'revoked') return 'danger'
  return 'warn'
}

function deviceConnectionLabel(device: Device) {
  if (device.online) return '已连接'
  const label = (device.session_end_label || '').trim()
  if (label) return label
  return '未连接'
}

/** Canonical offline bucket used by the connection-status filter. */
function deviceConnectionFilterKey(device: Device): DeviceConnectionFilter {
  if (device.online) return 'online'
  const reason = (device.session_end_reason || '').trim()
  if (reason === 'user_exit') return 'user_exit'
  if (reason === 'unexpected') return 'unexpected'
  if (reason === 'admin') return 'admin'
  if (reason === 'update') return 'update'
  if (reason === 'reconnecting') return 'reconnecting'
  return 'unknown_offline'
}

function deviceMatchesConnectionFilter(device: Device, filter: DeviceConnectionFilter): boolean {
  if (!filter) return true
  if (filter === 'online') return !!device.online
  if (filter === 'offline') return !device.online
  return deviceConnectionFilterKey(device) === filter
}

function deviceConnectionSeverity(device: Device) {
  if (device.online) return 'success'
  const reason = device.session_end_reason || ''
  if (reason === 'user_exit') return 'warn' // 人为终止
  if (reason === 'unexpected') return 'danger' // 意外终止
  if (reason === 'admin') return 'secondary'
  if (reason === 'update' || reason === 'reconnecting') return 'info'
  return 'secondary'
}

function deviceConnectionTitle(device: Device) {
  if (device.online) return 'TLS 会话在线'
  const parts = [deviceConnectionLabel(device)]
  if (device.session_end_detail) parts.push(device.session_end_detail)
  if (device.session_end_at) parts.push(formatDateTime(device.session_end_at))
  return parts.join(' · ')
}

function statusLabel(status: Message['status']) {
  return { pending: '待投递', sent: '已发送', expired: '已过期', withdrawn: '已撤回' }[status]
}

function updateStatusLabel(status?: string) {
  if (status === 'verifying') return '校验中'
  if (status === 'published') return '已发布'
  if (status === 'failed') return '校验失败'
  if (status === 'withdrawn') return '已撤回'
  return status || '未知'
}

function updateStatusSeverity(status?: string) {
  if (status === 'verifying') return 'warn'
  if (status === 'published') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'withdrawn') return 'secondary'
  return 'secondary'
}

function updateStatusTitle(update: UpdateRecord) {
  if (update.status === 'failed' && update.status_detail) return update.status_detail
  if (update.status === 'verifying') return '正在校验包内 files-v1 清单 SHA-256'
  return updateStatusLabel(update.status)
}

function updateComponentsLabel(update: UpdateRecord) {
  const items = (update.components?.length ? update.components : update.component ? [update.component] : [])
    .map((item) => {
      if (item === 'client') return '客户端'
      if (item === 'updater') return '更新器'
      if (item === 'bundle') return '客户端与更新器'
      return item
    })
  return items.length ? items.join(' + ') : '未知'
}

function messagePreview(value: string) {
  const characters = Array.from(value)
  return characters.length > 72 ? `${characters.slice(0, 72).join('')}…` : value
}

function messageOrdinal(message: Message) {
  // 服务端已按 created_at 保证最新在前；# 是展示序号，不复用会重启变化的 queue_seq。
  const index = messages.value.findIndex((item) => item.message_id === message.message_id)
  return index < 0 ? '—' : messages.value.length - index
}

function openMessagePreview(message: Message) {
  previewMessage.value = message
  messagePreviewVisible.value = true
}

/** Higher rank = more advanced client-side receipt (history must not hide a later displayed/spoken). */
const deliveryStatusRank: Record<string, number> = {
  pending: 0,
  sent: 1,
  expired: 1,
  withdrawn: 0,
  received: 2,
  failed: 3,
  displayed: 4,
  spoken: 5
}

function effectiveDeliveryStatus(
  current?: string | null,
  history?: string | null
): string {
  // After withdraw/expire the live row is terminal; prefer the last real receipt from history.
  if (current === 'withdrawn' || current === 'expired') {
    if (history && deliveryStatusRank[history] >= deliveryStatusRank.received) return history
    return current
  }
  const currentRank = deliveryStatusRank[current ?? ''] ?? -1
  const historyRank = deliveryStatusRank[history ?? ''] ?? -1
  if (historyRank > currentRank) return history as string
  if (current) return current
  return history || 'pending'
}

function deliveryCounts(message: Message) {
  const clients = new Set<string>([
    ...Object.keys(message.deliveries ?? {}),
    ...Object.keys(message.delivery_history ?? {})
  ])
  clients.delete('*')
  const statuses = [...clients].map((client) =>
    effectiveDeliveryStatus(message.deliveries?.[client], message.delivery_history?.[client])
  )
  const targeted = (message.target_client_ids ?? []).filter((id) => id && id !== '*')
  const total = Math.max(statuses.length, targeted.length)
  return {
    total,
    completed: statuses.filter((status) => status === 'displayed' || status === 'spoken').length,
    received: statuses.filter((status) =>
      status === 'received' || status === 'displayed' || status === 'spoken' || status === 'failed'
    ).length,
    failed: statuses.filter((status) => status === 'failed').length,
    pending: statuses.filter((status) => status === 'pending' || status === 'sent').length
  }
}

function deliveryCountsTitle(message: Message) {
  const counts = deliveryCounts(message)
  const parts = [
    `已显示完成 ${counts.completed}`,
    `已接收 ${counts.received}`,
    `目标/记录 ${counts.total}`
  ]
  if (counts.failed > 0) parts.push(`失败 ${counts.failed}`)
  if (counts.pending > 0) parts.push(`待完成 ${counts.pending}`)
  return parts.join(' · ')
}

function displayPositionLabel(value?: string | null) {
  const label = displayPositionOptions.find((option) => option.value === value)?.label
  return label ?? '未指定'
}

function displayDurationLabel(value?: number | null) {
  if (value == null) return '未指定'
  return value === 0 ? '手动关闭' : `比率 ${value}`
}

watch([messageSearch, messageStatusFilter, messagePositionFilter, messageTtsFilter], () => {
  tableFirst.value = 0
  tableJumpPage.value = 1
})

watch([updateSearch, updateStatusFilter], () => {
  tableFirst.value = 0
  tableJumpPage.value = 1
})

watch([deviceSearch, deviceStatusFilter, deviceOnlineFilter, deviceModeFilter, deviceSort], () => {
  tableFirst.value = 0
  tableJumpPage.value = 1
})

watch([active, tableTotalPages], () => {
  const page = Math.min(tableTotalPages.value, Math.floor(tableFirst.value / settings.pageSize) + 1)
  tableFirst.value = (page - 1) * settings.pageSize
  tableJumpPage.value = page
})

watch(() => settings.pageSize, () => {
  tableFirst.value = 0
  tableJumpPage.value = 1
  if (active.value === 'server') {
    serverLogViewPage.value = 1
    serverLogJumpPage.value = 1
  }
})

function formatPublicKeyFingerprint(value: string) {
  const normalized = value.trim().toLowerCase()
  if (!/^[0-9a-f]{64}$/.test(normalized)) return value
  const bytes = normalized.match(/.{2}/g)?.map((part) => parseInt(part, 16)) ?? []
  return `SHA256:${btoa(String.fromCharCode(...bytes)).replace(/=+$/, '')}`
}

function validFingerprintInput(value: string) {
  return /^(?:[0-9a-f]{64}|SHA256:[A-Za-z0-9+/]+={0,2})$/i.test(value.trim())
}

function formatDateTime(value?: number) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const formatted = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Taipei',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23'
  }).format(date)
  return formatted + '+08:00'
}

function formatLogTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const formatted = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Taipei',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23'
  }).format(date)
  return `${formatted}+08:00`
}

function selectSection(section: string, updateHash = true) {
  if (active.value === section) {
    if (isMobile.value) navigationVisible.value = false
    return
  }
  if (active.value === 'settings' && configDirty.value && !updateHash) {
    window.history.replaceState(null, '', `#${active.value}`)
  }
  const navigate = () => {
    active.value = section
    tableFirst.value = 0
    tableJumpPage.value = 1
    if (section === 'server') {
      serverLogViewPage.value = 1
      serverLogJumpPage.value = 1
      logsTableReady.value = false
    }
    if (updateHash && window.location.hash !== `#${section}`) {
      window.history.pushState(null, '', `#${section}`)
    } else if (!updateHash && window.location.hash !== `#${section}`) {
      window.history.replaceState(null, '', `#${section}`)
    }
    if (isMobile.value) navigationVisible.value = false
    if (section === 'devices') void loadDevices()
    if (section === 'updates') { void loadDevices(); void loadUpdates() }
    if (section === 'settings') void loadConfig()
    if (section === 'server') window.requestAnimationFrame(() => {
      logsTableReady.value = true
      void loadLogViewPage(1, true)
    })
  }
  if (active.value === 'settings' && configDirty.value) {
    confirm.require({
      message: '配置已修改但尚未保存',
      header: '未保存的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '保存',
      rejectLabel: '丢弃',
      rejectClass: 'p-button-text p-button-secondary',
      accept: async () => {
        await saveConfig()
        if (!configDirty.value) navigate()
      },
      reject: () => {
        discardConfigChanges()
        navigate()
      }
    })
    return
  }
  navigate()
}

const navigationItems = computed(() => [
  { key: 'messages', label: '消息队列', icon: 'pi pi-megaphone', class: active.value === 'messages' ? 'nav-active' : '', command: () => selectSection('messages') },
  { key: 'devices', label: '客户端', icon: 'pi pi-desktop', class: active.value === 'devices' ? 'nav-active' : '', command: () => selectSection('devices') },
  {
    key: 'updates',
    label: '更新发布',
    icon: 'pi pi-cloud-upload',
    beta: true,
    class: [active.value === 'updates' ? 'nav-active' : '', 'nav-beta'].filter(Boolean).join(' '),
    command: () => selectSection('updates')
  },
  { key: 'settings', label: '配置', icon: 'pi pi-sliders-h', class: active.value === 'settings' ? 'nav-active' : '', command: () => selectSection('settings') },
  { key: 'server', label: '服务端', icon: 'pi pi-server', class: active.value === 'server' ? 'nav-active' : '', command: () => selectSection('server') }
])

onMounted(() => {
  active.value = sectionFromHash()
  // Normalize legacy #logs bookmark to #server without adding history noise.
  if (window.location.hash === '#logs') {
    window.history.replaceState(null, '', '#server')
  }
  mediaQuery = window.matchMedia('(max-width: 1023px)')
  isMobile.value = mediaQuery.matches
  navigationVisible.value = !isMobile.value
  onMediaQueryChange = (event) => {
    // 断点切换时同步状态并暂时关闭过渡，避免移动端遮罩与桌面布局同时闪现。
    sidebarBreakpointChanging.value = true
    isMobile.value = event.matches
    navigationVisible.value = !event.matches
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        sidebarBreakpointChanging.value = false
      })
    })
  }
  mediaQuery.addEventListener('change', onMediaQueryChange)
  window.setTimeout(() => { sidebarTransitionEnabled.value = true }, 100)
  onHashChange = () => selectSection(sectionFromHash(), false)
  window.addEventListener('hashchange', onHashChange)
  onBeforeUnload = (event) => {
    if (!configDirty.value) return
    event.preventDefault()
    event.returnValue = ''
  }
  window.addEventListener('beforeunload', onBeforeUnload)
  loadHealth().catch(() => undefined)
  loadMessages().catch(() => toast.add({ severity: 'error', summary: '无法连接服务端', life: 3200 }))
  loadDevices().catch(() => undefined)
  // Full page reload on #updates must hydrate the list (publish only keeps in-memory until refresh).
  if (active.value === 'updates') {
    void loadUpdates()
  }
  const preferencesPromise = loadPreferences().catch(() => undefined)
  if (active.value === 'server') window.requestAnimationFrame(() => {
    logsTableReady.value = true
    void preferencesPromise.then(() => loadLogViewPage(1, true)).catch(() => undefined)
  })
  eventSource = new EventSource('/api/v1/events')
  eventSource.addEventListener('refresh', (event) => {
    try {
      const section = JSON.parse((event as MessageEvent).data).section
      if (section === 'all') {
        void Promise.all([
          loadMessages(),
          loadDevices(),
          loadConfig(),
          active.value === 'updates' ? loadUpdates() : Promise.resolve(),
          active.value === 'server' ? loadLogViewPage(serverLogViewPage.value, true) : Promise.resolve()
        ])
      } else if (section === 'messages') void loadMessages()
      else if (section === 'devices') void loadDevices()
      else if (section === 'updates') { void loadDevices(); void loadUpdates() }
      else if (section === 'config') void loadConfig()
      else if (section === 'server' && active.value === 'server') window.requestAnimationFrame(() => { void loadLogViewPage(serverLogViewPage.value, true) })
      else if (typeof section === 'string' && section.startsWith('client_logs:')) {
        const clientId = section.slice('client_logs:'.length)
        if (deviceLogsDialogVisible.value && deviceLogsDevice.value?.client_id === clientId) {
          void openDeviceLogs(deviceLogsDevice.value, deviceLogsPage.value)
        }
      }
    } catch {
      void refreshActive()
    }
  })
})

onBeforeUnmount(() => {
  if (mediaQuery && onMediaQueryChange) mediaQuery.removeEventListener('change', onMediaQueryChange)
  if (onHashChange) window.removeEventListener('hashchange', onHashChange)
  if (onBeforeUnload) window.removeEventListener('beforeunload', onBeforeUnload)
  eventSource?.close()
})
</script>

<template>
  <Toast />
  <ConfirmDialog :closable="false" :closeOnEscape="false" :dismissableMask="false" />
  <div class="shell">
    <Sidebar v-model:visible="navigationVisible" :modal="false" :dismissable="false" :closeOnEscape="false" position="left"
      class="navigation-sidebar"
      :class="{ 'sidebar-no-initial-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }"
      :showCloseIcon="false">
      <div class="navigation-brand"><span class="brand-mark">M</span><strong>lovemilk class broadcaster</strong></div>
      <PanelMenu :model="navigationItems" multiple>
        <template #item="{ item }">
          <a
            class="nav-item-link"
            :class="{ 'nav-item-link--active': item.class?.includes?.('nav-active'), 'nav-item-link--beta': item.beta }"
            @click.prevent="item.command?.({ originalEvent: $event, item })"
          >
            <span v-if="item.icon" :class="item.icon" class="nav-item-icon" />
            <span class="nav-item-label">{{ item.label }}</span>
            <span v-if="item.beta" class="nav-item-beta" title="实验性功能">Beta</span>
          </a>
        </template>
      </PanelMenu>
      <div class="navigation-status">
        <span class="online-dot" />服务端在线
        <span class="server-version" :title="serverVersionLabel">v{{ serverVersionLabel }}</span>
      </div>
    </Sidebar>

    <main class="workspace" :class="{ 'sidebar-open': navigationVisible && !isMobile, 'layout-no-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }">
      <header class="topbar">
        <div class="topbar-leading"><Button icon="pi pi-bars" text rounded @click="navigationVisible = !navigationVisible"
            :aria-label="navigationVisible ? '收起导航菜单' : '展开导航菜单'" :title="navigationVisible ? '收起导航菜单' : '展开导航菜单'" />
          <div class="topbar-title-stack">
            <span class="eyebrow">lovemilk class broadcaster</span>
            <h1 class="page-title">
              <span>{{ active === 'messages' ? '消息队列' : active === 'devices' ? '客户端' : active === 'updates' ? '更新发布' : active === 'settings' ? '配置' : '服务端' }}</span>
              <span v-if="active === 'updates'" class="beta-badge" title="实验性功能">Beta</span>
            </h1>
          </div>
        </div><div class="topbar-actions"><Button label="刷新当前页面" icon="pi pi-refresh" text aria-label="刷新当前页面数据" title="刷新当前页面数据" :loading="refreshLoading" @click="refreshActive" /><Button label="关闭服务端" icon="pi pi-power-off" severity="danger" size="small" :loading="shutdownLoading" @click="requestServerShutdown" /></div>
      </header>
      <section v-if="active === 'messages'" class="content">
        <div class="metrics">
          <Card><template #content><span>待投递</span><strong>{{ pendingCount }}</strong></template>
          </Card>
          <Card><template #content><span>消息总数</span><strong>{{ messages.length }}</strong></template></Card>
          <Card><template #content><span>服务端 C2S TLS 端口</span><strong>{{ serverTlsPort }}</strong></template></Card>
          <Card><template #content><span>服务端前端 / API 端口</span><strong>{{ serverHttpPort }}</strong></template></Card>
          <Card><template #content><span>服务端版本</span><strong>{{ serverVersionLabel }}</strong></template></Card>
        </div>
        <div class="section-head">
          <div>
            <h2>最近消息</h2>
            <p>优先级数字越小，优先程度越高，称为“高优先级”；同优先级按入队顺序处理</p>
          </div><Button label="发送消息" icon="pi pi-send" @click="dialogVisible = true" />
        </div>
        <Form class="message-filters" :initialValues="{ search: '', status: '', position: '', tts: 'all' }">
          <label>搜索<InputText v-model="messageSearch" placeholder="内容、客户端或消息 ID" /></label>
          <label>状态<Select v-model="messageStatusFilter" :options="messageStatusOptions" optionLabel="label" optionValue="value" placeholder="全部状态" showClear /></label>
          <label>显示位置<Select v-model="messagePositionFilter" :options="displayPositionOptions" optionLabel="label" optionValue="value" placeholder="全部位置" showClear /></label>
          <label>语音<Select v-model="messageTtsFilter" :options="messageTtsOptions" optionLabel="label" optionValue="value" /></label>
        </Form>
        <TablePagination :page="tablePage" :total="filteredMessages.length" :pageSize="settings.pageSize" :pageSizeOptions="pageSizeOptions" :loading="messagesLoading" @update:page="setTablePage" @update:pageSize="changeTablePageSize" />
        <DataTable :value="pagedMessages" stripedRows responsiveLayout="scroll" class="queue-table" :loading="messagesLoading">
          <template #empty>暂无消息</template>
          <Column header="#" style="width: 6rem"><template #body="slotProps">{{ messageOrdinal(slotProps.data) }}</template></Column>
          <Column field="created_at" header="发送时间" style="width: 13rem"><template #body="slotProps">{{
            formatDateTime(slotProps.data.created_at) }}</template></Column>
          <Column field="content" header="内容" style="min-width: 18rem; max-width: 32rem"><template #body="slotProps">
            <Button class="message-preview-button" text :label="messagePreview(slotProps.data.content)" @click="openMessagePreview(slotProps.data)" />
          </template></Column>
          <Column field="priority" header="优先级（数字越小优先）" style="width: 13rem" />
          <Column field="target_client_ids" header="目标" style="width: 14rem"><template #body="slotProps">{{
            slotProps.data.target_client_ids?.map(formatPublicKeyFingerprint).join(', ') || '全部客户端' }}</template></Column>
          <Column field="status" header="状态" style="width: 9rem"><template #body="slotProps">
              <Tag :value="statusLabel(slotProps.data.status)" :severity="statusSeverity(slotProps.data.status)" />
            </template>
          </Column>
          <Column header="客户端" style="width: 16rem; min-width: 14rem">
            <template #body="slotProps">
              <div
                v-for="counts in [deliveryCounts(slotProps.data)]"
                :key="slotProps.data.message_id"
                class="delivery-tags"
                :title="deliveryCountsTitle(slotProps.data)"
              >
                <Tag
                  :value="`显示 ${counts.completed}`"
                  :severity="counts.completed > 0 ? 'success' : 'secondary'"
                />
                <Tag
                  :value="`接收 ${counts.received}`"
                  :severity="counts.received > 0 ? 'info' : 'secondary'"
                />
                <Tag
                  v-if="counts.failed > 0"
                  :value="`失败 ${counts.failed}`"
                  severity="danger"
                />
                <Tag
                  :value="`共 ${counts.total}`"
                  severity="secondary"
                />
              </div>
            </template>
          </Column>
          <Column header="显示位置" style="width: 9rem"><template #body="slotProps">{{ displayPositionLabel(slotProps.data.display_position) }}</template></Column>
          <Column header="显示比率" style="width: 9rem"><template #body="slotProps">{{ displayDurationLabel(slotProps.data.display_duration_ratio) }}</template></Column>
          <Column header="TTS" style="width: 5rem"><template #body="slotProps"><Tag :value="slotProps.data.tts_enabled ? '开启' : '关闭'" :severity="slotProps.data.tts_enabled ? 'info' : 'secondary'" /></template></Column>
          <Column header="操作" style="width: 7rem"><template #body="slotProps">
              <Button
                v-if="slotProps.data.status !== 'expired' && slotProps.data.status !== 'withdrawn'"
                label="撤回"
                text
                severity="danger"
                size="small"
                :loading="withdrawingMessageId === slotProps.data.message_id"
                :disabled="withdrawingMessageId !== null && withdrawingMessageId !== slotProps.data.message_id"
                @click="withdrawMessage(slotProps.data)"
              />
            </template></Column>
        </DataTable>
      </section>
      <section v-else-if="active === 'devices'" class="content">
        <div class="section-head">
          <div>
            <h2>客户端</h2>
            <p>{{ filteredDevices.length }} / {{ devices.length }} 个客户端</p>
          </div><Button label="批准公钥" icon="pi pi-key" @click="openManualApproveDevice" />
        </div>
        <Form class="message-filters" :initialValues="{ search: '', status: '', online: '', mode: '', sort: 'approved_at_desc' }">
          <label>搜索<InputText v-model="deviceSearch" placeholder="名称、指纹、版本或客户端 ID" /></label>
          <label>授权状态
            <Select v-model="deviceStatusFilter" :options="deviceStatusOptions" optionLabel="label" optionValue="value" placeholder="全部状态" showClear />
          </label>
          <label>连接状态
            <Select
              v-model="deviceOnlineFilter"
              :options="deviceOnlineOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="全部状态"
              showClear
            />
          </label>
          <label>连接模式
            <Select v-model="deviceModeFilter" :options="deviceModeFilterOptions" optionLabel="label" optionValue="value" placeholder="全部模式" showClear />
          </label>
          <label>排序
            <Select v-model="deviceSort" :options="deviceSortOptions" optionLabel="label" optionValue="value" />
          </label>
        </Form>
        <TablePagination :page="tablePage" :total="filteredDevices.length" :pageSize="settings.pageSize" :pageSizeOptions="pageSizeOptions" :loading="devicesLoading" @update:page="setTablePage" @update:pageSize="changeTablePageSize" />
        <DataTable :value="pagedDevices" stripedRows responsiveLayout="scroll" class="queue-table" :loading="devicesLoading">
          <template #empty>{{ devicesLoading ? '加载中…' : (deviceSearch || deviceStatusFilter || deviceOnlineFilter || deviceModeFilter ? '没有符合筛选条件的客户端' : '暂无客户端') }}</template>
          <Column field="approved_at" header="时间" style="width: 13rem"><template #body="slotProps">{{
            formatDateTime(slotProps.data.approved_at) }}</template></Column>
          <Column field="label" header="名称" style="width: 14rem"><template #body="slotProps">{{ slotProps.data.label ||
            '未命名客户端' }}</template></Column>
          <Column field="client_version" header="当前版本" style="width: 9rem"><template #body="slotProps">{{ slotProps.data.client_version || '未知' }}</template></Column>
          <Column field="client_id" header="客户端唯一标识 (公钥 SHA256)"><template #body="slotProps">{{
            formatPublicKeyFingerprint(slotProps.data.client_id) }}</template></Column>
          <Column field="status" header="授权状态" style="width: 8rem"><template #body="slotProps">
              <Tag :value="deviceStatusLabel(slotProps.data.status)"
                :severity="deviceStatusSeverity(slotProps.data.status)" />
            </template></Column>
          <Column field="online" header="连接状态" style="width: 10rem"><template #body="slotProps">
              <Tag
                :value="deviceConnectionLabel(slotProps.data)"
                :severity="deviceConnectionSeverity(slotProps.data)"
                :title="deviceConnectionTitle(slotProps.data)"
              />
            </template></Column>
          <Column header="连接模式" style="width: 12rem"><template #body="slotProps">
              <Tag :value="slotProps.data.connection_mode === 'listen' ? '监听模式' : '从模式'"
                :severity="slotProps.data.connection_mode === 'listen' ? 'info' : 'secondary'" />
              <small v-if="slotProps.data.requested_mode === 'auto'" class="device-mode-note">自动侦测</small>
            </template></Column>
          <Column header="监听测试" style="width: 14rem"><template #body="slotProps">
              <span v-if="slotProps.data.probe_sent">{{ slotProps.data.probe_received ?? 0 }}/{{ slotProps.data.probe_sent }}，丢包 {{ slotProps.data.probe_loss_percent ?? 0 }}%</span>
              <span v-else>未测试</span>
            </template></Column>
          <Column field="last_seen" header="最近在线" style="width: 13rem"><template #body="slotProps">{{
            slotProps.data.last_seen ? formatDateTime(slotProps.data.last_seen) : '从未连接' }}</template>
          </Column>
          <Column header="操作" style="width: 18rem"><template #body="slotProps">
              <div class="device-actions">
                <Button v-if="slotProps.data.status !== 'approved'" :label="slotProps.data.status === 'revoked' ? '重新授权' : '批准'" icon="pi pi-check" size="small"
                  :loading="approving" @click="openApproveDevice(slotProps.data)" />
                <Button v-else label="编辑" icon="pi pi-pencil" text size="small"
                  @click="openEditDevice(slotProps.data)" />
                <Button label="日志" icon="pi pi-file" text size="small" @click="openDeviceLogs(slotProps.data)" />
                <Button v-if="slotProps.data.online" label="断开" icon="pi pi-power-off" text severity="warn" size="small"
                  :loading="approving" @click="disconnectDevice(slotProps.data)" />
                <Button v-if="slotProps.data.status === 'approved'" label="取消批准" icon="pi pi-times" text
                  severity="danger" size="small" :loading="approving" @click="revokeDevice(slotProps.data)" />
              </div>
            </template></Column>
        </DataTable>
      </section>
      <section v-else-if="active === 'updates'" class="content">
        <div class="section-head">
          <div>
            <h2 class="section-title-with-badge">发布客户端更新 <span class="beta-badge" title="实验性功能">Beta</span></h2>
            <p>上传后立即入队；后台校验 SHA-256，通过后推送在线客户端。勾选「强制」时所有已批准客户端必须更新，离线客户端上线后自动补推。失败原因显示在状态列。</p>
          </div>
          <Button label="添加更新" icon="pi pi-cloud-upload" @click="openUpdateDialog" />
        </div>
        <Form class="message-filters" :initialValues="{ search: '', status: '' }">
          <label>搜索<InputText v-model="updateSearch" placeholder="版本、组件、SHA-256 或更新 ID" /></label>
          <label>状态
            <Select
              v-model="updateStatusFilter"
              :options="updateStatusOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="全部状态"
              showClear
            />
          </label>
        </Form>
        <TablePagination
          :page="tablePage"
          :total="filteredUpdates.length"
          :pageSize="settings.pageSize"
          :pageSizeOptions="pageSizeOptions"
          :loading="updatesLoading"
          @update:page="setTablePage"
          @update:pageSize="changeTablePageSize"
        />
        <DataTable :value="pagedUpdates" :loading="updatesLoading" stripedRows responsiveLayout="scroll" class="queue-table update-table">
          <template #empty>{{ updatesLoading ? '加载中…' : (updateSearch || updateStatusFilter ? '没有符合筛选条件的更新记录' : '暂无更新记录') }}</template>
          <Column field="created_at" header="发送时间" style="width: 13rem"><template #body="slotProps">{{ formatDateTime(slotProps.data.created_at) }}</template></Column>
          <Column field="seq" header="序列" style="width: 6rem"><template #body="slotProps">{{ slotProps.data.seq || '-' }}</template></Column>
          <Column field="components" header="更新组件" style="width: 10rem"><template #body="slotProps">{{ updateComponentsLabel(slotProps.data) }}</template></Column>
          <Column field="version" header="版本" style="width: 8rem" />
          <Column field="client_version" header="客户端版本" style="width: 9rem" />
          <Column field="updater_version" header="更新器版本" style="width: 9rem" />
          <Column field="force" header="强制" style="width: 6rem"><template #body="slotProps">
            <Tag v-if="slotProps.data.force" value="强制" severity="danger" title="全部已批准客户端必须更新；离线上线后补推" />
            <span v-else class="text-muted">—</span>
          </template></Column>
          <Column field="sha256" header="SHA-256"><template #body="slotProps"><code class="update-hash" :title="slotProps.data.sha256">{{ slotProps.data.sha256 }}</code></template></Column>
          <Column field="status" header="状态" style="width: 12rem"><template #body="slotProps">
            <div class="update-status-cell">
              <Tag :value="updateStatusLabel(slotProps.data.status)" :severity="updateStatusSeverity(slotProps.data.status)" :title="updateStatusTitle(slotProps.data)" />
              <small v-if="slotProps.data.status === 'failed' && slotProps.data.status_detail" class="update-status-detail" :title="slotProps.data.status_detail">{{ slotProps.data.status_detail }}</small>
            </div>
          </template></Column>
        </DataTable>
      </section>
      <section v-else-if="active === 'settings'" class="content settings-content">
        <div class="section-head">
          <div>
            <h2>服务端配置</h2>
            <p>配置 ID：{{ configForm.config_id || '未加载' }}</p>
          </div><Button label="保存配置" icon="pi pi-save" :loading="configSaving || configLoading" @click="saveConfig" />
        </div>
        <Skeleton v-if="configLoading" height="8rem" class="api-skeleton" />
        <Form class="settings-grid" :initialValues="configForm" @submit="saveConfig">
          <Card class="setting-group"><template #title>连接</template><template #content><label>心跳间隔（秒）
                <SafeInputNumber name="heartbeat_interval_seconds" v-model="configForm.heartbeat_interval_seconds" :min="1"
                  :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>心跳超时（秒）
                <SafeInputNumber name="heartbeat_timeout_seconds" v-model="configForm.heartbeat_timeout_seconds" :min="1"
                  :max="7200" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>回执超时（秒）
                <SafeInputNumber name="ack_timeout_seconds" v-model="configForm.ack_timeout_seconds" :min="1"
                  :max="604800" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
                <small>客户端在此时间内没有完成回执时，服务端允许重新投递消息。</small>
              </label></template>
          </Card>
          <Card class="setting-group"><template #title>消息与语音</template><template #content><label>消息保留（小时）
                <SafeInputNumber name="message_ttl_hours" v-model="configForm.message_ttl_hours" :min="1" :max="168"
                  :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>语音节点最大递归解析深度
                <SafeInputNumber name="max_speech_depth" v-model="configForm.max_speech_depth" :min="1" :max="64"
                  :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>语音节点最大重复展开上限
                <SafeInputNumber name="max_repeat_expansion" v-model="configForm.max_repeat_expansion" :min="1" :max="10000"
                  :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>默认消息显示位置
                <Select name="default_display_position" v-model="configForm.default_display_position" :options="displayPositionOptions"
                  optionLabel="label" optionValue="value" />
              </label><label>默认显示比率
                <SafeInputNumber name="default_display_duration_ratio" v-model="configForm.display_duration_ratio" :min="0"
                  :max="120" :step="0.1" mode="decimal" :minFractionDigits="0" :maxFractionDigits="2" :useGrouping="false" :allowEmpty="false" :emptyValue="0" />
                <small>按字符权重计算显示时长；设为 0 时消息不会自动关闭。</small>
              </label></template>
          </Card>
          <Card class="setting-group"><template #title>监听模式探测</template><template #content>
              <label>探测间隔（秒）
                <SafeInputNumber name="listener_probe_interval_seconds" v-model="configForm.listener_probe_interval_seconds" :min="1" :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>试用时长（小时）
                <SafeInputNumber name="listener_probe_duration_hours" v-model="configForm.listener_probe_duration_hours" :min="1" :max="168" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>失败重测间隔（天）
                <SafeInputNumber name="listener_probe_reset_days" v-model="configForm.listener_probe_reset_days" :min="1" :max="365" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><label>最大丢包率（%）
                <SafeInputNumber name="listener_loss_threshold" v-model="configForm.listener_loss_threshold" :min="0" :max="100" :useGrouping="false" :allowEmpty="false" :emptyValue="0" />
              </label><label>监听空闲断开（秒）
                <SafeInputNumber name="listener_idle_timeout_seconds" v-model="configForm.listener_idle_timeout_seconds" :min="1" :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
              </label><small>自动模式在试用窗口内按间隔探测；丢包率不超过阈值才切换监听模式。</small>
            </template></Card>
          <Card class="setting-group"><template #title>发布方式</template><template #content><label class="checkbox-label">
                <Checkbox name="immediate" v-model="configForm.immediate" binary />立即向现有连接发送配置变更
              </label><small>未启用时，新配置在后续服务端发包时生效。</small></template>
          </Card>
        </Form>
      </section>
      <section v-else class="content logs-content">
        <div class="section-head">
          <div>
            <h2>服务端</h2>
            <p>维护过期数据，并查看服务端运行日志。</p>
          </div>
        </div>

        <Card class="maintenance-card server-identity-card">
          <template #title>服务端版本</template>
          <template #content>
            <dl class="server-version-meta">
              <div><dt>版本</dt><dd>{{ serverVersion || '未知' }}</dd></div>
              <div><dt>构建时间</dt><dd>{{ serverBuildDate || '未知' }}</dd></div>
            </dl>
          </template>
        </Card>

        <Card class="maintenance-card">
          <template #title>数据维护</template>
          <template #content>
            <p class="maintenance-copy">自动任务会周期性清理超过 185 天的消息与客户端记录。也可在此立即执行一次清理。</p>
            <ul class="maintenance-list">
              <li>消息：删除创建时间 ≥ 185 天的全部消息记录（含 pending / sent）</li>
              <li>客户端：删除超过 185 天未活跃的设备记录</li>
            </ul>
            <div class="maintenance-actions">
              <Button
                label="清理 ≥ 185 天的数据"
                icon="pi pi-trash"
                severity="danger"
                outlined
                :loading="purgeExpiredLoading"
                @click="purgeExpiredHistory"
              />
            </div>
          </template>
        </Card>

        <div class="section-head log-section-head">
          <div>
            <h2>服务端日志</h2>
            <p>日志内容由服务端统一使用英文记录。</p>
          </div>
          <Button label="刷新" icon="pi pi-refresh" :loading="logsLoading" @click="loadLogViewPage(serverLogViewPage, true)" />
        </div>
        <Form class="log-filters" :initialValues="{ start: null, end: null, minimumLevel: '', selectedLevels: [] }" @submit="applyLogFilters">
          <label>开始日期<DatePicker name="start" v-model="logStartInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
          <label>结束日期<DatePicker name="end" v-model="logEndInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
          <label>最低级别<Select name="minimumLevel" v-model="logMinimumLevelInput" :options="logLevels" placeholder="全部级别" showClear /></label>
          <label>指定级别<MultiSelect name="selectedLevels" v-model="logSelectedLevelsInput" :options="logLevels" placeholder="全部级别" display="chip" /></label>
          <div class="log-filter-actions"><Button type="button" label="重置" text @click="resetLogFilters" /><Button type="submit" label="应用筛选" icon="pi pi-filter" /></div>
          <small class="log-filter-hint">日期、最低级别、指定级别条件同时满足；指定的多个级别任一匹配即可。</small>
        </Form>
        <TablePagination :page="serverLogViewPage" :total="serverLogTotal" :pageSize="settings.pageSize" :pageSizeOptions="pageSizeOptions" :loading="logsLoading" @update:page="loadLogViewPage" @update:pageSize="changeLogPageSize" />
        <Skeleton v-if="!logsTableReady" height="24rem" class="api-skeleton" />
        <DataTable v-else :value="filteredServerLogEntries" stripedRows responsiveLayout="scroll" class="queue-table" :loading="logsLoading">
          <template #empty>暂无日志</template>
          <Column field="time" header="时间" style="width: 13rem" />
          <Column field="level" header="级别" style="width: 7rem"><template #body="slotProps">
            <Tag :value="slotProps.data.level || 'UNKNOWN'" :severity="logSeverity(slotProps.data.level)" />
          </template></Column>
          <Column field="msg" header="消息" />
          <Column field="fields" header="字段" />
        </DataTable>
      </section>
    </main>
  </div>

  <Dialog v-model:visible="dialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" class="app-dialog" :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
    <template #header><div class="send-dialog-header"><span>发送消息</span><small v-if="sendFormHint" class="form-hint">{{ sendFormHint }}</small></div></template>
    <Form class="form" :initialValues="{ content: '', priority: 1024, target_all: true, target_client_ids: [], tts_enabled: true }"
      @submit="sendMessage"><label>内容
        <Textarea name="content" v-model="content" rows="5" autoResize /></label>
      <label class="checkbox-label">
        <Checkbox name="tts_enabled" v-model="ttsEnabled" binary />朗读消息内容
      </label>
      <div class="form-row"><label class="checkbox-label"><Checkbox v-model="useDefaultMessagePosition" binary />使用默认消息显示位置</label>
        <Select v-model="messageDisplayPosition" :options="displayPositionOptions" optionLabel="label" optionValue="value"
          :disabled="useDefaultMessagePosition" /></div>
      <div class="form-row"><label class="checkbox-label"><Checkbox v-model="useDefaultMessageDuration" binary />使用默认显示比率</label>
        <label>单条消息显示比率<SafeInputNumber v-model="messageDisplayRatio" :min="0" :max="120" :step="0.1" mode="decimal" :minFractionDigits="0" :maxFractionDigits="2" :useGrouping="false"
            :disabled="useDefaultMessageDuration" :allowEmpty="false" :emptyValue="0" />
          <small>按字符权重计算；设为 0 时需手动关闭。</small>
          <small class="display-duration-hint">{{ estimatedDisplayDurationHint }}</small>
        </label></div>
      <div class="form-row"><label>优先级
                <SafeInputNumber name="priority" v-model="priority" :min="0" :max="65535" :useGrouping="false" showButtons buttonLayout="vertical" :allowEmpty="false" :emptyValue="0" />
        </label><div class="target-field"><span class="field-label">目标客户端</span>
          <span class="target-picker">
            <Checkbox name="target_all" v-model="targetAll" binary />全部客户端
          </span>
          <MultiSelect name="target_client_ids" v-model="selectedTargets" :options="clientOptions" optionLabel="label"
            optionValue="value" placeholder="选择客户端" display="chip" filter :disabled="targetAll" />
        </div></div>
      <div class="dialog-actions"><Button
          type="submit" label="加入队列" icon="pi pi-send" :loading="sending" :disabled="!sendFormValid" /></div>
    </Form>
  </Dialog>
  <Dialog
    v-model:visible="updateDialogVisible"
    modal
    :closable="!publishingUpdate"
    :showHeader="true"
    :closeOnEscape="!publishingUpdate"
    :draggable="false"
    position="center"
    header="添加并推送更新"
    class="app-dialog"
    :style="{ width: 'min(44rem, calc(100vw - 2rem))' }"
  >
    <Form class="form update-publish-form" @submit="publishUpdate">
      <div class="update-package-picker">
        <span class="field-label">更新包文件</span>
        <div
          class="update-package-dropzone"
          :class="{
            'is-dragover': updatePackageDragOver && !publishingUpdate,
            'has-file': updatePackages.length > 0,
            'is-disabled': publishingUpdate
          }"
          role="button"
          :tabindex="publishingUpdate ? -1 : 0"
          :aria-disabled="publishingUpdate"
          @dragenter="!publishingUpdate && onUpdatePackageDragEnter($event)"
          @dragover="!publishingUpdate && onUpdatePackageDragOver($event)"
          @dragleave="!publishingUpdate && onUpdatePackageDragLeave($event)"
          @drop="!publishingUpdate && onUpdatePackageDrop($event)"
        >
          <i class="pi pi-cloud-upload update-package-dropzone-icon" aria-hidden="true" />
          <div class="update-package-dropzone-copy">
            <strong>{{ publishingUpdate ? '正在上传…' : (updatePackages.length > 0 ? `已选择 ${updatePackages.length} 个更新包` : '拖拽更新包到此处') }}</strong>
            <span>{{ publishingUpdate ? '上传完成前不可修改文件、强制选项或目标客户端' : (updatePackages.length > 0 ? '可继续拖入或点下方按钮追加更多文件' : '仅支持 .tar.zst 更新包（绿色安装 .zip 不可上传），可多选或继续追加') }}</span>
          </div>
          <div class="update-package-dropzone-actions" @click.stop>
            <input
              ref="updatePackageInputRef"
              class="update-package-input"
              type="file"
              accept=".tar.zst,application/zstd"
              multiple
              :disabled="publishingUpdate"
              @change="onUpdatePackageSelect"
            />
            <Button type="button" label="选择文件" icon="pi pi-folder-open" :disabled="publishingUpdate" @click="openUpdatePackagePicker" />
            <Button v-if="updatePackages.length > 0" type="button" label="全部清除" text severity="secondary" :disabled="publishingUpdate" @click="clearUpdatePackages" />
          </div>
        </div>
        <ul v-if="updatePackages.length > 0" class="update-package-list">
          <li v-for="(file, index) in updatePackages" :key="updatePackageKey(file)" class="update-package-item">
            <div class="update-package-item-meta">
              <strong class="update-package-name" :title="file.name">{{ file.name }}</strong>
              <span>{{ formatUpdatePackageSize(file.size) }}</span>
            </div>
            <Button type="button" icon="pi pi-times" text rounded severity="secondary" :aria-label="`移除 ${file.name}`" :disabled="publishingUpdate" @click="removeUpdatePackage(index)" />
          </li>
        </ul>
        <small>可一次选择或多个追加更新包；组件、版本、平台和 SHA-256 将由服务端从包内 metadata.json 自动识别并校验。</small>
      </div>
      <div class="target-field"><span class="field-label">推送范围</span>
        <span class="target-picker" :class="{ 'is-disabled': publishingUpdate }">
          <Checkbox name="force" v-model="updateForce" binary :disabled="publishingUpdate" />强制更新（所有已批准客户端必须更新）
        </span>
        <span class="target-picker" :class="{ 'is-disabled': updateForce || publishingUpdate }">
          <Checkbox name="target_all" v-model="updateTargetAll" binary :disabled="updateForce || publishingUpdate" />全部客户端
        </span>
        <MultiSelect
          name="target_client_ids"
          v-model="updateTargets"
          :options="clientOptions"
          optionLabel="label"
          optionValue="value"
          filter
          display="chip"
          placeholder="选择已批准客户端"
          :loading="devicesLoading"
          :disabled="publishingUpdate || updateForce || updateTargetAll"
        />
      </div>
      <small v-if="publishingUpdate">正在按提交时的强制/目标设置上传，请勿关闭对话框。</small>
      <small v-else-if="updateForce">强制更新会推送给当前全部已批准客户端；离线客户端上线后自动补推，直至完成该序列号更新。</small>
      <small v-else>客户端列表中的当前版本由最近一次握手上报；离线目标会保留为未送达记录（非强制时不自动补推）。</small>
      <div class="dialog-actions">
        <Button
          type="submit"
          :label="publishingUpdate ? '上传中…' : (updateForce ? '强制推送更新' : '添加并推送更新')"
          icon="pi pi-cloud-upload"
          :loading="publishingUpdate"
          :disabled="publishingUpdate || (!updateForce && !updateTargetAll && updateTargets.length === 0) || clientOptions.length === 0 || updatePackages.length === 0"
        />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="messagePreviewVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="消息内容"
    class="app-dialog" :style="{ width: 'min(46rem, calc(100vw - 2rem))' }">
    <div class="message-preview-content">{{ previewMessage?.content || '' }}</div>
  </Dialog>
  <Dialog v-model:visible="deviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="批准客户端公钥"
    class="app-dialog" :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
    <Form class="form" :initialValues="{ clientId: '', label: '' }" @submit="approveDevice"><label>客户端唯一标识 (公钥 SHA256)
        <InputText name="clientId" v-model="newClientId" placeholder="SHA256:Base64 或 64 位十六进制字符串" />
      </label><label>名称
        <InputText name="label" v-model="deviceLabel" placeholder="给客户端 (公钥) 命名" />
      </label>
      <label>连接模式
        <Select name="forced_mode" v-model="approvingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions"><Button
          type="submit" label="批准" icon="pi pi-check" :loading="approving"
          :disabled="!validFingerprintInput(newClientId)" />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="editDeviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="编辑客户端"
    class="app-dialog" :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="renameDevice">
      <label>名称
        <InputText v-model="editingLabel" placeholder="客户端名称" />
      </label>
      <label>连接模式
        <Select v-model="editingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions">
        <Button type="submit" label="保存" icon="pi pi-save" :loading="approving" />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="approveDeviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="批准客户端"
    class="app-dialog" :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="approvePendingDevice">
      <label>客户端唯一标识 (公钥 SHA256)
        <InputText :modelValue="approvingDevice ? formatPublicKeyFingerprint(approvingDevice.client_id) : ''" readonly />
      </label>
      <label>名称
        <InputText v-model="approvingLabel" placeholder="给客户端命名（可选）" autofocus />
      </label>
      <label>连接模式
        <Select v-model="approvingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions">
        <Button type="submit" label="批准" icon="pi pi-check" :loading="approving" />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="deviceLogsDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center"
    :header="`${deviceLogsDevice?.label || (deviceLogsDevice ? formatPublicKeyFingerprint(deviceLogsDevice.client_id) : '')} 的客户端本地日志`"
    class="app-dialog" :style="{ width: 'min(70rem, calc(100vw - 2rem))' }">
    <TablePagination :page="deviceLogsPage" :total="deviceLogsTotal" :pageSize="settings.pageSize" :pageSizeOptions="pageSizeOptions" :loading="deviceLogsLoading" @update:page="changeDeviceLogPage" @update:pageSize="changeDeviceLogPageSize" />
    <div>
      <Button label="刷新" icon="pi pi-refresh" text :loading="deviceLogsLoading" @click="deviceLogsDevice && openDeviceLogs(deviceLogsDevice, deviceLogsPage)" />
    </div>
    <Skeleton v-if="deviceLogsLoading" height="20rem" />
    <DataTable v-else :value="deviceLogEntries" stripedRows responsiveLayout="scroll" class="queue-table">
      <template #empty>暂无客户端日志</template>
      <Column field="time" header="时间" style="width: 13rem" />
      <Column field="level" header="级别" style="width: 7rem"><template #body="slotProps">
        <Tag :value="slotProps.data.level || 'UNKNOWN'" :severity="logSeverity(slotProps.data.level)" />
      </template></Column>
      <Column field="msg" header="消息" />
      <Column field="fields" header="字段" />
    </DataTable>
  </Dialog>
</template>
