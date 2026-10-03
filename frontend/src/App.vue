<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
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
import InputNumber from 'primevue/inputnumber'
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
}

type Device = {
  client_id: string
  label: string
  status?: 'pending' | 'approved'
  online?: boolean
  approved_at: number
  last_seen?: number
}

type ConfigForm = {
  config_id: string
  issued_at: number
  heartbeat_interval_seconds: number
  heartbeat_timeout_seconds: number
  message_ttl_hours: number
  max_speech_depth: number
  max_repeat_expansion: number
  default_display_position: string
  display_duration_ratio: number
  immediate: boolean
}

type ServerLogEntry = {
  time?: string
  rawTime?: string
  level?: string
  msg?: string
  fields?: string
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
const configForm = ref<ConfigForm>({ config_id: '', issued_at: 0, heartbeat_interval_seconds: 15, heartbeat_timeout_seconds: 45, message_ttl_hours: 24, max_speech_depth: 8, max_repeat_expansion: 100, default_display_position: settings.defaultDisplayPosition, display_duration_ratio: settings.defaultDisplayDurationRatio, immediate: false })
const dialogVisible = ref(false)
const messagePreviewVisible = ref(false)
const previewMessage = ref<Message | null>(null)
const deviceDialogVisible = ref(false)
const editDeviceDialogVisible = ref(false)
const approveDeviceDialogVisible = ref(false)
const editingDevice = ref<Device | null>(null)
const approvingDevice = ref<Device | null>(null)
const editingLabel = ref('')
const approvingLabel = ref('')
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
const configLoading = ref(false)
const preferencesLoading = ref(false)
const shutdownLoading = ref(false)
const refreshLoading = ref(false)
const serverLog = ref('')
const serverLogEntries = ref<ServerLogEntry[]>([])
const serverLogPage = ref(1)
const serverLogFirst = ref(0)
const serverLogTotal = ref(0)
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
const sections = ['messages', 'devices', 'settings', 'logs'] as const

function sectionFromHash() {
  const value = window.location.hash.replace(/^#/, '')
  return (sections.includes(value as typeof sections[number]) ? value : 'messages') as typeof sections[number]
}
const newClientId = ref('')
const deviceLabel = ref('')
const pendingCount = computed(() => messages.value.filter((item) => item.status === 'pending').length)
const sendFormValid = computed(() => content.value.trim().length > 0 && (targetAll.value || selectedTargets.value.length > 0))
const sendFormHint = computed(() => {
  if (!content.value.trim()) return '请输入消息内容。'
  if (!targetAll.value && selectedTargets.value.length === 0) return '请选择至少一个客户端，或勾选全部客户端。'
  return ''
})
const clientOptions = computed(() => devices.value
  .filter((device) => !device.status || device.status === 'approved')
  .map((device) => {
    const fingerprint = formatPublicKeyFingerprint(device.client_id)
    return { label: device.label ? `${device.label} · ${fingerprint}` : fingerprint, value: device.client_id }
  }))
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
const messageStatusOptions = [
  { label: '全部状态', value: '' },
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

function configComparable(value: ConfigForm) {
  return JSON.stringify({
    heartbeat_interval_seconds: value.heartbeat_interval_seconds,
    heartbeat_timeout_seconds: value.heartbeat_timeout_seconds,
    message_ttl_hours: value.message_ttl_hours,
    max_speech_depth: value.max_speech_depth,
    max_repeat_expansion: value.max_repeat_expansion,
    default_display_position: value.default_display_position,
    display_duration_ratio: value.display_duration_ratio
  })
}

const configDirty = computed(() => Boolean(publishedConfig.value) && configComparable(configForm.value) !== publishedConfig.value)

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
    if (messagePositionFilter.value && (message.display_position ?? configForm.value.default_display_position) !== messagePositionFilter.value) return false
    if (messageTtsFilter.value === 'enabled' && !message.tts_enabled) return false
    if (messageTtsFilter.value === 'disabled' && message.tts_enabled) return false
    if (!query) return true
    const target = (message.target_client_ids ?? []).join(' ').toLowerCase()
    return message.content.toLowerCase().includes(query) || target.includes(query) || message.message_id.toLowerCase().includes(query)
  }).sort((left, right) => right.queue_seq - left.queue_seq)
})

async function loadDevices() {
  devicesLoading.value = true
  try {
    const response = await fetch('/api/v1/devices')
    if (!response.ok) throw new Error('加载客户端失败')
    const data = await response.json()
    devices.value = data.items ?? []
  } finally {
    devicesLoading.value = false
  }
}

function logDateParam(value: Date | null) {
  if (!value) return ''
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

async function loadLogs(page = serverLogPage.value) {
  logsLoading.value = true
  try {
    const requestedPage = Math.max(1, Math.trunc(page))
    const params = new URLSearchParams({ page: String(requestedPage), page_size: String(settings.pageSize) })
    const start = logDateParam(logStart.value)
    const end = logDateParam(logEnd.value)
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    if (logMinimumLevel.value) params.set('minimum_level', logMinimumLevel.value)
    if (logSelectedLevels.value.length) params.set('levels', logSelectedLevels.value.join(','))
    const response = await fetch(`/api/v1/logs?${params.toString()}`)
    if (!response.ok) throw new Error('加载服务端日志失败')
    const data = await response.json()
    serverLogPage.value = Number(data.page) || requestedPage
    serverLogFirst.value = (serverLogPage.value - 1) * settings.pageSize
    serverLogTotal.value = Number(data.total) || 0
    serverLog.value = data.content ?? ''
    serverLogEntries.value = serverLog.value.split(/\r?\n/).filter(Boolean).map((line: string) => {
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
  } finally {
    logsLoading.value = false
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

function handleTablePage(event: { rows?: number }) {
  if (event.rows) void updatePageSize(Number(event.rows))
}

function handleLogPage(event: { first?: number; rows?: number }) {
  const rows = Number(event.rows ?? settings.pageSize)
  const page = Math.floor(Number(event.first ?? 0) / Math.max(1, rows)) + 1
  if (rows !== settings.pageSize) {
    void updatePageSize(rows).then(() => loadLogs(page))
    return
  }
  void loadLogs(page)
}

const filteredServerLogEntries = computed(() => {
  const minimum = logMinimumLevel.value ? logLevels.indexOf(logMinimumLevel.value) : -1
  const selected = new Set(logSelectedLevels.value)
  const start = logStart.value ? targetDayStart(logStart.value) : -Infinity
  const end = logEnd.value ? targetDayStart(logEnd.value) + 86400000 - 1 : Infinity
  return serverLogEntries.value.filter((entry) => {
    const levelIndex = logLevels.indexOf((entry.level ?? '').toUpperCase())
    if (minimum >= 0 && (levelIndex < 0 || levelIndex < minimum)) return false
    if (selected.size && !selected.has((entry.level ?? '').toUpperCase())) return false
    if (entry.rawTime) {
      const timestamp = Date.parse(entry.rawTime)
      if (!Number.isNaN(timestamp) && (timestamp < start || timestamp > end)) return false
    }
    return true
  })
})

function targetDayStart(date: Date) {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) - 8 * 60 * 60 * 1000
}

function applyLogFilters() {
  logStart.value = logStartInput.value
  logEnd.value = logEndInput.value
  logMinimumLevel.value = logMinimumLevelInput.value
  logSelectedLevels.value = [...logSelectedLevelsInput.value]
  if (active.value === 'logs') void loadLogs(1)
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
  try {
    const response = await fetch(`/api/v1/messages/${message.message_id}/withdraw`, { method: 'POST' })
    if (!response.ok) throw new Error((await response.json()).error ?? '撤回失败')
    await loadMessages()
    toast.add({ severity: 'success', summary: '消息已撤回', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '撤回失败', life: 3200 })
  }
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

async function loadConfig() {
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
        max_speech_depth: configForm.value.max_speech_depth,
        max_repeat_expansion: configForm.value.max_repeat_expansion,
        default_display_position: configForm.value.default_display_position,
        default_display_duration_ratio: configForm.value.display_duration_ratio,
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
      body: JSON.stringify({ client_id: event.values.clientId.trim(), label: event.values.label?.trim() ?? '' })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '批准失败')
    deviceDialogVisible.value = false
    newClientId.value = ''
    deviceLabel.value = ''
    await loadDevices()
    toast.add({ severity: 'success', summary: '客户端已批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '批准失败', life: 3200 })
  } finally {
    approving.value = false
  }
}

function openApproveDevice(device: Device) {
  approvingDevice.value = device
  approvingLabel.value = device.label ?? ''
  approveDeviceDialogVisible.value = true
}

async function approvePendingDevice() {
  if (!approvingDevice.value) return
  approving.value = true
  try {
    const response = await fetch('/api/v1/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: approvingDevice.value.client_id, label: approvingLabel.value.trim() })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '批准失败')
    approveDeviceDialogVisible.value = false
    approvingDevice.value = null
    approvingLabel.value = ''
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
  editDeviceDialogVisible.value = true
}

async function renameDevice() {
  if (!editingDevice.value) return
  approving.value = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(editingDevice.value.client_id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: editingLabel.value.trim() })
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

async function refreshActive() {
  if (active.value === 'settings' && configDirty.value) {
    confirm.require({
      message: '配置已修改但尚未发布。要发布修改还是丢弃修改？',
      header: '未发布的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '发布',
      rejectLabel: '丢弃',
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
    else if (active.value === 'settings') await loadConfig()
    else if (active.value === 'logs') await loadLogs(serverLogPage.value)
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

function statusLabel(status: Message['status']) {
  return { pending: '待投递', sent: '已发送', expired: '已过期', withdrawn: '已撤回' }[status]
}

function messagePreview(value: string) {
  const characters = Array.from(value)
  return characters.length > 72 ? `${characters.slice(0, 72).join('')}…` : value
}

function openMessagePreview(message: Message) {
  previewMessage.value = message
  messagePreviewVisible.value = true
}

function deliveryCounts(message: Message) {
  const statuses = Object.values(message.deliveries ?? {})
  if (!statuses.length) return { completed: 0, received: 0 }
  return {
    completed: statuses.filter((status) => status === 'displayed' || status === 'spoken').length,
    received: statuses.filter((status) => status === 'received' || status === 'displayed' || status === 'spoken').length
  }
}

function displayPositionLabel(value?: string | null) {
  const effective = value ?? configForm.value.default_display_position
  const label = displayPositionOptions.find((option) => option.value === effective)?.label ?? '默认配置'
  return value == null ? `默认配置（${label}）` : label
}

function displayDurationLabel(value?: number | null) {
  if (value == null) {
    const defaultValue = configForm.value.display_duration_ratio
    return `默认配置（${defaultValue === 0 ? '手动关闭' : `比率 ${defaultValue}`}）`
  }
  return value === 0 ? '手动关闭' : `比率 ${value}`
}

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
    if (updateHash && window.location.hash !== `#${section}`) {
      window.history.pushState(null, '', `#${section}`)
    } else if (!updateHash && window.location.hash !== `#${section}`) {
      window.history.replaceState(null, '', `#${section}`)
    }
    if (isMobile.value) navigationVisible.value = false
    if (section === 'devices') void loadDevices()
    if (section === 'settings') void loadConfig()
    if (section === 'logs') window.requestAnimationFrame(() => { void loadLogs(1) })
  }
  if (active.value === 'settings' && configDirty.value) {
    confirm.require({
      message: '配置已修改但尚未发布。要发布修改还是丢弃修改？',
      header: '未发布的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '发布',
      rejectLabel: '丢弃',
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
  { label: '消息队列', icon: 'pi pi-megaphone', class: active.value === 'messages' ? 'nav-active' : '', command: () => selectSection('messages') },
  { label: '客户端', icon: 'pi pi-desktop', class: active.value === 'devices' ? 'nav-active' : '', command: () => selectSection('devices') },
  { label: '配置', icon: 'pi pi-sliders-h', class: active.value === 'settings' ? 'nav-active' : '', command: () => selectSection('settings') },
  { label: '服务端日志', icon: 'pi pi-file', class: active.value === 'logs' ? 'nav-active' : '', command: () => selectSection('logs') }
])

onMounted(() => {
  active.value = sectionFromHash()
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
  loadMessages().catch(() => toast.add({ severity: 'error', summary: '无法连接服务端', life: 3200 }))
  loadDevices().catch(() => undefined)
  void loadPreferences().catch(() => undefined)
  if (active.value === 'logs') window.requestAnimationFrame(() => { void loadLogs(1).catch(() => undefined) })
  eventSource = new EventSource('/api/v1/events')
  eventSource.addEventListener('refresh', (event) => {
    try {
      const section = JSON.parse((event as MessageEvent).data).section
      if (section === 'all') {
        void Promise.all([loadMessages(), loadDevices(), loadConfig(), active.value === 'logs' ? loadLogs(serverLogPage.value) : Promise.resolve()])
      } else if (section === 'messages') void loadMessages()
      else if (section === 'devices') void loadDevices()
      else if (section === 'config') void loadConfig()
      else if (section === 'logs' && active.value === 'logs') window.requestAnimationFrame(() => { void loadLogs(serverLogPage.value) })
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
  <ConfirmDialog />
  <div class="shell">
    <Sidebar v-model:visible="navigationVisible" :modal="false" :dismissable="false" :closeOnEscape="false" position="left"
      class="navigation-sidebar"
      :class="{ 'sidebar-no-initial-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }"
      :showCloseIcon="false">
      <div class="navigation-brand"><span class="brand-mark">M</span><strong>lovemilk class broadcaster</strong></div>
      <PanelMenu :model="navigationItems" multiple />
      <div class="navigation-status"><span class="online-dot" />服务端在线</div>
    </Sidebar>

    <main class="workspace" :class="{ 'sidebar-open': navigationVisible && !isMobile, 'layout-no-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }">
      <header class="topbar">
        <div class="topbar-leading"><Button icon="pi pi-bars" text rounded @click="navigationVisible = !navigationVisible"
            :aria-label="navigationVisible ? '收起导航菜单' : '展开导航菜单'" :title="navigationVisible ? '收起导航菜单' : '展开导航菜单'" />
          <div><span class="eyebrow">lovemilk class broadcaster</span>
          <h1>{{ active === 'messages' ? '消息队列' : active === 'devices' ? '客户端' : active === 'settings' ? '配置' : '服务端日志' }}</h1>
          </div>
        </div><div class="topbar-actions"><Button label="刷新当前页面" icon="pi pi-refresh" text aria-label="刷新当前页面数据" title="刷新当前页面数据" :loading="refreshLoading" @click="refreshActive" /><Button label="关闭服务端" icon="pi pi-power-off" severity="danger" size="small" :loading="shutdownLoading" @click="requestServerShutdown" /></div>
      </header>
      <section v-if="active === 'messages'" class="content">
        <div class="metrics">
          <Card><template #content><span>待投递</span><strong>{{ pendingCount }}</strong></template>
          </Card>
          <Card><template #content><span>消息总数</span><strong>{{ messages.length }}</strong></template></Card>
          <Card><template #content><span>服务端端口</span><strong>39002</strong></template></Card>
        </div>
        <div class="section-head">
          <div>
            <h2>最近消息</h2>
            <p>优先级数字越小，优先程度越高；同优先级按入队顺序处理</p>
          </div><Button label="发送消息" icon="pi pi-send" @click="dialogVisible = true" />
        </div>
        <Form class="message-filters" :initialValues="{ search: '', status: '', position: '', tts: 'all' }">
          <label>搜索<InputText v-model="messageSearch" placeholder="内容、客户端或消息 ID" /></label>
          <label>状态<Select v-model="messageStatusFilter" :options="messageStatusOptions" optionLabel="label" optionValue="value" /></label>
          <label>显示位置<Select v-model="messagePositionFilter" :options="displayPositionOptions" optionLabel="label" optionValue="value" placeholder="全部位置" showClear /></label>
          <label>语音<Select v-model="messageTtsFilter" :options="messageTtsOptions" optionLabel="label" optionValue="value" /></label>
        </Form>
        <DataTable :value="filteredMessages" stripedRows responsiveLayout="scroll" class="queue-table" sortField="queue_seq"
          :sortOrder="-1" paginator paginatorPosition="top" paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink CurrentPageReport RowsPerPageDropdown JumpToPageInput" currentPageReportTemplate="{currentPage} / {totalPages}" :rows="settings.pageSize" :rowsPerPageOptions="pageSizeOptions" :loading="messagesLoading" @page="handleTablePage">
          <template #empty>暂无消息</template>
          <Column field="queue_seq" header="#" style="width: 6rem" />
          <Column field="created_at" header="发送时间" style="width: 13rem"><template #body="slotProps">{{
            formatDateTime(slotProps.data.created_at) }}</template></Column>
          <Column field="content" header="内容" style="min-width: 18rem; max-width: 32rem"><template #body="slotProps">
            <Button class="message-preview-button" text :label="messagePreview(slotProps.data.content)" @click="openMessagePreview(slotProps.data)" />
          </template></Column>
          <Column field="priority" header="优先级（数字越小越高）" style="width: 13rem" />
          <Column field="target_client_ids" header="目标" style="width: 14rem"><template #body="slotProps">{{
            slotProps.data.target_client_ids?.map(formatPublicKeyFingerprint).join(', ') || '全部客户端' }}</template></Column>
          <Column field="status" header="状态" style="width: 9rem"><template #body="slotProps">
              <Tag :value="statusLabel(slotProps.data.status)" :severity="statusSeverity(slotProps.data.status)" />
            </template>
          </Column>
          <Column header="已显示完成" style="width: 9rem"><template #body="slotProps">{{ deliveryCounts(slotProps.data).completed }} 个客户端</template></Column>
          <Column header="已接收" style="width: 8rem"><template #body="slotProps">{{ deliveryCounts(slotProps.data).received }} 个客户端</template></Column>
          <Column header="显示位置" style="width: 9rem"><template #body="slotProps">{{ displayPositionLabel(slotProps.data.display_position) }}</template></Column>
          <Column header="显示比率" style="width: 9rem"><template #body="slotProps">{{ displayDurationLabel(slotProps.data.display_duration_ratio) }}</template></Column>
          <Column header="TTS" style="width: 5rem"><template #body="slotProps"><Tag :value="slotProps.data.tts_enabled ? '开启' : '关闭'" :severity="slotProps.data.tts_enabled ? 'info' : 'secondary'" /></template></Column>
          <Column header="操作" style="width: 7rem"><template #body="slotProps">
              <Button v-if="slotProps.data.status !== 'expired' && slotProps.data.status !== 'withdrawn'" label="撤回"
                text severity="danger" size="small" @click="withdrawMessage(slotProps.data)" />
            </template></Column>
        </DataTable>
      </section>
      <section v-else-if="active === 'devices'" class="content">
        <div class="section-head">
          <div>
            <h2>客户端</h2>
            <p>{{ devices.length }} 个客户端</p>
          </div><Button label="批准公钥" icon="pi pi-key" @click="deviceDialogVisible = true" />
        </div>
        <DataTable :value="devices" stripedRows responsiveLayout="scroll" class="queue-table" paginator paginatorPosition="top" paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink CurrentPageReport RowsPerPageDropdown JumpToPageInput" currentPageReportTemplate="{currentPage} / {totalPages}" :rows="settings.pageSize" :rowsPerPageOptions="pageSizeOptions" :loading="devicesLoading" @page="handleTablePage">
          <template #empty>暂无客户端</template>
          <Column field="approved_at" header="时间" style="width: 13rem"><template #body="slotProps">{{
            formatDateTime(slotProps.data.approved_at) }}</template></Column>
          <Column field="label" header="名称" style="width: 14rem"><template #body="slotProps">{{ slotProps.data.label ||
            '未命名客户端' }}</template></Column>
          <Column field="client_id" header="客户端唯一标识 (公钥 SHA256)"><template #body="slotProps">{{
            formatPublicKeyFingerprint(slotProps.data.client_id) }}</template></Column>
          <Column field="status" header="授权状态" style="width: 8rem"><template #body="slotProps">
              <Tag :value="slotProps.data.status === 'pending' ? '待确认' : '已批准'"
                :severity="slotProps.data.status === 'pending' ? 'warn' : 'success'" />
            </template></Column>
          <Column field="online" header="连接状态" style="width: 8rem"><template #body="slotProps">
              <Tag :value="slotProps.data.online ? '已连接' : '未连接'"
                :severity="slotProps.data.online ? 'success' : 'secondary'" />
            </template></Column>
          <Column field="last_seen" header="最近在线" style="width: 13rem"><template #body="slotProps">{{
            slotProps.data.last_seen ? formatDateTime(slotProps.data.last_seen) : '从未连接' }}</template>
          </Column>
          <Column header="操作" style="width: 8rem"><template #body="slotProps">
              <div class="device-actions">
                <Button v-if="slotProps.data.status === 'pending'" label="批准" icon="pi pi-check" size="small"
                  :loading="approving" @click="openApproveDevice(slotProps.data)" />
                <Button v-else label="重命名" icon="pi pi-pencil" text size="small"
                  @click="openEditDevice(slotProps.data)" />
                <Button v-if="slotProps.data.status !== 'pending'" label="取消批准" icon="pi pi-times" text
                  severity="danger" size="small" :loading="approving" @click="revokeDevice(slotProps.data)" />
              </div>
            </template></Column>
        </DataTable>
      </section>
      <section v-else-if="active === 'settings'" class="content settings-content">
        <div class="section-head">
          <div>
            <h2>服务端配置</h2>
            <p>配置 ID：{{ configForm.config_id || '未加载' }}</p>
          </div><Button label="发布配置" icon="pi pi-save" :loading="configSaving || configLoading" @click="saveConfig" />
        </div>
        <Skeleton v-if="configLoading" height="8rem" class="api-skeleton" />
        <Form class="settings-grid" :initialValues="configForm" @submit="saveConfig">
          <Card class="setting-group"><template #title>连接</template><template #content><label>心跳间隔（秒）
                <InputNumber name="heartbeat_interval_seconds" v-model="configForm.heartbeat_interval_seconds" :min="1"
                  :max="3600" :useGrouping="false" />
              </label><label>心跳超时（秒）
                <InputNumber name="heartbeat_timeout_seconds" v-model="configForm.heartbeat_timeout_seconds" :min="1"
                  :max="7200" :useGrouping="false" />
              </label></template>
          </Card>
          <Card class="setting-group"><template #title>消息与语音</template><template #content><label>消息保留（小时）
                <InputNumber name="message_ttl_hours" v-model="configForm.message_ttl_hours" :min="1" :max="168"
                  :useGrouping="false" />
              </label><label>语音节点最大递归解析深度
                <InputNumber name="max_speech_depth" v-model="configForm.max_speech_depth" :min="1" :max="64"
                  :useGrouping="false" />
              </label><label>语音节点最大重复展开上限
                <InputNumber name="max_repeat_expansion" v-model="configForm.max_repeat_expansion" :min="1" :max="10000"
                  :useGrouping="false" />
              </label><label>默认消息显示位置
                <Select name="default_display_position" v-model="configForm.default_display_position" :options="displayPositionOptions"
                  optionLabel="label" optionValue="value" />
              </label><label>默认显示比率
                <InputNumber name="default_display_duration_ratio" v-model="configForm.display_duration_ratio" :min="0"
                  :max="60" :step="0.1" :useGrouping="false" />
                <small>按字符权重计算显示时长；设为 0 时消息不会自动关闭。</small>
              </label></template>
          </Card>
          <Card class="setting-group"><template #title>发布方式</template><template #content><label class="checkbox-label">
                <Checkbox name="immediate" v-model="configForm.immediate" binary />立即向现有连接发送配置变更
              </label><small>未启用时，新配置在后续服务端发包时生效。</small></template>
          </Card>
        </Form>
      </section>
      <section v-else class="content logs-content">
        <div class="section-head"><div><h2>服务端日志</h2><p>日志内容由服务端统一使用英文记录。</p></div><Button label="刷新" icon="pi pi-refresh" :loading="logsLoading" @click="loadLogs(serverLogPage)" /></div>
        <Form class="log-filters" :initialValues="{ start: null, end: null, minimumLevel: '', selectedLevels: [] }" @submit="applyLogFilters">
          <label>开始日期<DatePicker name="start" v-model="logStartInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
          <label>结束日期<DatePicker name="end" v-model="logEndInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
          <label>最低级别<Select name="minimumLevel" v-model="logMinimumLevelInput" :options="logLevels" placeholder="全部级别" showClear /></label>
          <label>指定级别<MultiSelect name="selectedLevels" v-model="logSelectedLevelsInput" :options="logLevels" placeholder="全部级别" display="chip" /></label>
          <div class="log-filter-actions"><Button type="button" label="重置" text @click="resetLogFilters" /><Button type="submit" label="应用筛选" icon="pi pi-filter" /></div>
          <small class="log-filter-hint">日期、最低级别、指定级别条件同时满足；指定的多个级别任一匹配即可。</small>
        </Form>
        <DataTable :value="filteredServerLogEntries" stripedRows responsiveLayout="scroll" class="queue-table" paginator paginatorPosition="top" lazy :first="serverLogFirst" :totalRecords="serverLogTotal" currentPageReportTemplate="{currentPage} / {totalPages}" paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink CurrentPageReport RowsPerPageDropdown JumpToPageInput" :rows="settings.pageSize" :rowsPerPageOptions="pageSizeOptions" :loading="logsLoading" @page="handleLogPage">
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

  <Dialog v-model:visible="dialogVisible" modal :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
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
        <label>单条消息显示比率<InputNumber v-model="messageDisplayRatio" :min="0" :max="60" :step="0.1" :useGrouping="false"
            :disabled="useDefaultMessageDuration" /><small>按字符权重计算；设为 0 时需手动关闭。</small></label></div>
      <div class="form-row"><label>优先级
                <InputNumber name="priority" v-model="priority" :min="0" :max="65535" :useGrouping="false" showButtons buttonLayout="vertical" />
        </label><div class="target-field"><span class="field-label">目标客户端</span>
          <span class="target-picker">
            <Checkbox name="target_all" v-model="targetAll" binary />全部客户端
          </span>
          <MultiSelect name="target_client_ids" v-model="selectedTargets" :options="clientOptions" optionLabel="label"
            optionValue="value" placeholder="选择客户端" display="chip" filter :disabled="targetAll" />
        </div></div>
      <div class="dialog-actions"><Button type="button" label="取消" text @click="dialogVisible = false" /><Button
          type="submit" label="加入队列" icon="pi pi-send" :loading="sending" :disabled="!sendFormValid" /></div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="messagePreviewVisible" modal header="消息内容"
    :style="{ width: 'min(46rem, calc(100vw - 2rem))' }">
    <div class="message-preview-content">{{ previewMessage?.content || '' }}</div>
  </Dialog>
  <Dialog v-model:visible="deviceDialogVisible" modal header="批准客户端公钥"
    :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
    <Form class="form" :initialValues="{ clientId: '', label: '' }" @submit="approveDevice"><label>客户端唯一标识 (公钥 SHA256)
        <InputText name="clientId" v-model="newClientId" placeholder="SHA256:Base64 或 64 位十六进制字符串" />
      </label><label>名称
        <InputText name="label" v-model="deviceLabel" placeholder="给客户端 (公钥) 命名" />
      </label>
      <div class="dialog-actions"><Button type="button" label="取消" text @click="deviceDialogVisible = false" /><Button
          type="submit" label="批准" icon="pi pi-check" :loading="approving"
          :disabled="!validFingerprintInput(newClientId)" />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="editDeviceDialogVisible" modal header="重命名客户端"
    :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="renameDevice">
      <label>名称
        <InputText v-model="editingLabel" placeholder="客户端名称" />
      </label>
      <div class="dialog-actions">
        <Button type="button" label="取消" text @click="editDeviceDialogVisible = false" />
        <Button type="submit" label="保存" icon="pi pi-save" :loading="approving" />
      </div>
    </Form>
  </Dialog>
  <Dialog v-model:visible="approveDeviceDialogVisible" modal header="批准客户端"
    :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="approvePendingDevice">
      <label>客户端唯一标识 (公钥 SHA256)
        <InputText :modelValue="approvingDevice ? formatPublicKeyFingerprint(approvingDevice.client_id) : ''" readonly />
      </label>
      <label>名称
        <InputText v-model="approvingLabel" placeholder="给客户端命名（可选）" autofocus />
      </label>
      <div class="dialog-actions">
        <Button type="button" label="取消" text @click="approveDeviceDialogVisible = false" />
        <Button type="submit" label="批准" icon="pi pi-check" :loading="approving" />
      </div>
    </Form>
  </Dialog>
</template>
