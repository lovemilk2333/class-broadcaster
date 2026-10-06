import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useToast } from 'primevue/usetoast'
import type { ConfigForm, Device, Message, ServerLogEntry, UpdateRecord } from '../types'
import { useSettingsStore } from './settings'
import { sortDevicesStable } from '../utils/devices'
import { formatPublicKeyFingerprint, logDateParam, formatLogTime } from '../utils/format'
import { pageSizeOptions } from '../utils/labels'

function defaultConfigForm(settings: ReturnType<typeof useSettingsStore>): ConfigForm {
  return {
    config_id: '',
    issued_at: 0,
    heartbeat_interval_seconds: 15,
    heartbeat_timeout_seconds: 45,
    message_ttl_hours: 24,
    ack_timeout_seconds: 300,
    max_speech_depth: 8,
    max_repeat_expansion: 100,
    default_display_position: settings.defaultDisplayPosition,
    display_duration_ratio: settings.defaultDisplayDurationRatio,
    listener_probe_interval_seconds: 60,
    listener_probe_duration_hours: 12,
    listener_probe_reset_days: 31,
    listener_loss_threshold: 10,
    listener_idle_timeout_seconds: 60,
    immediate: false
  }
}

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

export function parseServerLogEntries(content: string): ServerLogEntry[] {
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

export const useAdminStore = defineStore('admin', () => {
  const settings = useSettingsStore()
  const toast = useToast()

  const messages = ref<Message[]>([])
  const devices = ref<Device[]>([])
  const updates = ref<UpdateRecord[]>([])
  const configForm = ref<ConfigForm>(defaultConfigForm(settings))
  const publishedConfig = ref('')
  const publishedConfigForm = ref<ConfigForm | null>(null)

  const messagesLoading = ref(false)
  const devicesLoading = ref(false)
  const updatesLoading = ref(false)
  const configLoading = ref(false)
  const configSaving = ref(false)
  const preferencesLoading = ref(false)
  const shutdownLoading = ref(false)
  const refreshLoading = ref(false)
  const purgeExpiredLoading = ref(false)
  const withdrawingMessageId = ref<string | null>(null)
  const approving = ref(false)
  const sending = ref(false)
  const publishingUpdate = ref(false)

  const serverTlsPort = ref(39002)
  const serverHttpPort = ref(39003)
  const serverVersion = ref('')
  const serverBuildDate = ref('')

  const logsLoading = ref(false)
  const logsTableReady = ref(false)
  const serverLog = ref('')
  const serverLogEntries = ref<ServerLogEntry[]>([])
  const serverLogPage = ref(1)
  const serverLogTotal = ref(0)
  const serverLogJumpPage = ref<number | null>(1)
  const serverLogViewPage = ref(1)
  const backendLogPageSize = 100

  const logStart = ref<Date | null>(null)
  const logEnd = ref<Date | null>(null)
  const logMinimumLevel = ref('')
  const logSelectedLevels = ref<string[]>([])
  const logStartInput = ref<Date | null>(null)
  const logEndInput = ref<Date | null>(null)
  const logMinimumLevelInput = ref('')
  const logSelectedLevelsInput = ref<string[]>([])

  const deviceLogsDialogVisible = ref(false)
  const deviceLogsDevice = ref<Device | null>(null)
  const deviceLogEntries = ref<ServerLogEntry[]>([])
  const deviceLogsLoading = ref(false)
  const deviceLogsPage = ref(1)
  const deviceLogsTotal = ref(0)
  const deviceLogsJumpPage = ref<number | null>(1)

  const pendingCount = computed(() => messages.value.filter((item) => item.status === 'pending').length)

  const configDirty = computed(() => Boolean(publishedConfig.value) && configComparable(configForm.value) !== publishedConfig.value)

  const serverVersionLabel = computed(() => {
    if (!serverVersion.value) return '未知'
    if (!serverBuildDate.value || serverBuildDate.value === 'dev') return `${serverVersion.value} (dev)`
    return `${serverVersion.value} (${serverBuildDate.value})`
  })

  const clientOptions = computed(() => sortDevicesStable(
    devices.value.filter((device) => !device.status || device.status === 'approved'),
    'approved_at_desc'
  ).map((device) => {
    const fingerprint = formatPublicKeyFingerprint(device.client_id)
    return { label: device.label ? `${device.label} · ${fingerprint}` : fingerprint, value: device.client_id }
  }))

  const deviceLogsTotalPages = computed(() => Math.max(1, Math.ceil(deviceLogsTotal.value / settings.pageSize)))
  const serverLogTotalPages = computed(() => Math.max(1, Math.ceil(serverLogTotal.value / settings.pageSize)))

  const visibleServerLogEntries = computed(() => {
    const globalFirst = (serverLogViewPage.value - 1) * settings.pageSize
    const backendFirst = (serverLogPage.value - 1) * backendLogPageSize
    const localFirst = globalFirst - backendFirst
    if (localFirst < 0 || localFirst >= serverLogEntries.value.length) return []
    return serverLogEntries.value.slice(localFirst, localFirst + settings.pageSize)
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

  async function loadDevices() {
    devicesLoading.value = true
    try {
      const response = await fetch('/api/v1/devices')
      if (!response.ok) throw new Error('加载客户端失败')
      const data = await response.json()
      devices.value = Array.isArray(data.items) ? data.items : []
    } finally {
      devicesLoading.value = false
    }
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
      updates.value = Array.isArray(data.items) ? data.items : []
    } catch (error) {
      toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '加载更新记录失败', life: 3000 })
    } finally {
      updatesLoading.value = false
    }
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

  function applyLogFilters() {
    logStart.value = logStartInput.value
    logEnd.value = logEndInput.value
    logMinimumLevel.value = logMinimumLevelInput.value
    logSelectedLevels.value = [...logSelectedLevelsInput.value]
    serverLogViewPage.value = 1
  }

  function resetLogFilters() {
    logStartInput.value = null
    logEndInput.value = null
    logMinimumLevelInput.value = ''
    logSelectedLevelsInput.value = []
    applyLogFilters()
  }

  async function changeLogPageSize(value: number) {
    if (!pageSizeOptions.includes(value) || value === settings.pageSize) return
    const first = (serverLogViewPage.value - 1) * settings.pageSize
    await updatePageSize(value)
    await loadLogViewPage(Math.floor(first / value) + 1, true)
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

  async function changeDeviceLogPageSize(value: number) {
    if (!deviceLogsDevice.value || !pageSizeOptions.includes(value) || value === settings.pageSize) return
    const first = (deviceLogsPage.value - 1) * settings.pageSize
    await updatePageSize(value)
    await openDeviceLogs(deviceLogsDevice.value, Math.floor(first / value) + 1)
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

  async function requestServerShutdown() {
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

  async function purgeExpiredHistory() {
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

  function applyLogFiltersAndReload() {
    applyLogFilters()
    void loadLogViewPage(1, true)
  }

  function messageOrdinal(message: Message) {
    // 服务端已按 created_at 保证最新在前；# 是展示序号，不复用会重启变化的 queue_seq。
    const index = messages.value.findIndex((item) => item.message_id === message.message_id)
    return index < 0 ? '—' : messages.value.length - index
  }

  return {
    messages,
    devices,
    updates,
    configForm,
    publishedConfig,
    publishedConfigForm,
    messagesLoading,
    devicesLoading,
    updatesLoading,
    configLoading,
    configSaving,
    preferencesLoading,
    shutdownLoading,
    refreshLoading,
    purgeExpiredLoading,
    withdrawingMessageId,
    approving,
    sending,
    publishingUpdate,
    serverTlsPort,
    serverHttpPort,
    serverVersion,
    serverBuildDate,
    serverVersionLabel,
    logsLoading,
    logsTableReady,
    serverLog,
    serverLogEntries,
    serverLogPage,
    serverLogTotal,
    serverLogJumpPage,
    serverLogViewPage,
    backendLogPageSize,
    logStart,
    logEnd,
    logMinimumLevel,
    logSelectedLevels,
    logStartInput,
    logEndInput,
    logMinimumLevelInput,
    logSelectedLevelsInput,
    deviceLogsDialogVisible,
    deviceLogsDevice,
    deviceLogEntries,
    deviceLogsLoading,
    deviceLogsPage,
    deviceLogsTotal,
    deviceLogsJumpPage,
    deviceLogsTotalPages,
    pendingCount,
    configDirty,
    clientOptions,
    serverLogTotalPages,
    visibleServerLogEntries,
    loadHealth,
    loadMessages,
    loadDevices,
    loadUpdates,
    loadConfig,
    saveConfig,
    discardConfigChanges,
    loadPreferences,
    updatePageSize,
    loadLogViewPage,
    applyLogFilters,
    applyLogFiltersAndReload,
    resetLogFilters,
    changeLogPageSize,
    openDeviceLogs,
    changeDeviceLogPageSize,
    withdrawMessage,
    requestServerShutdown,
    purgeExpiredHistory,
    messageOrdinal
  }
})
