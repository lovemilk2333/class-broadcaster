<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import { Form } from '@primevue/forms'
import type { FormSubmitEvent } from '@primevue/forms'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import DataTable from 'primevue/datatable'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import FilterableDataTable, { type FiltersModel, type FilterFieldDef } from '../components/FilterableDataTable.vue'
import TablePagination from '../components/TablePagination.vue'
import { useAdminStore } from '../stores/admin'
import { useSettingsStore } from '../stores/settings'
import type { Device, DeviceConnectionFilter, DeviceSortKey } from '../types'
import { CustomFilterMatchMode, FilterMatchMode, ensureCustomFiltersRegistered, textFilter, emptyFilter } from '../utils/filters'
import { sortDevicesStable } from '../utils/devices'
import { formatDateTime, formatPublicKeyFingerprint, validFingerprintInput } from '../utils/format'
import {
  connectionModeOptions,
  deviceConnectionLabel,
  deviceConnectionSeverity,
  deviceConnectionTitle,
  deviceMatchesConnectionFilter,
  deviceModeFilterOptions,
  deviceOnlineOptions,
  deviceSortOptions,
  deviceStatusLabel,
  deviceStatusOptions,
  deviceStatusSeverity,
  logSeverity,
  pageSizeOptions
} from '../utils/labels'

ensureCustomFiltersRegistered()

const admin = useAdminStore()
const settings = useSettingsStore()
const toast = useToast()
const confirm = useConfirm()

const page = ref(1)
const filteredCount = ref(0)
const deviceDialogVisible = ref(false)
const editDeviceDialogVisible = ref(false)
const approveDeviceDialogVisible = ref(false)
const editingDevice = ref<Device | null>(null)
const approvingDevice = ref<Device | null>(null)
const editingLabel = ref('')
const editingForcedMode = ref('auto')
const approvingLabel = ref('')
const approvingForcedMode = ref('auto')
const newClientId = ref('')
const deviceLabel = ref('')

const filters = ref<FiltersModel>({
  global: textFilter('', CustomFilterMatchMode.DEVICE_SEARCH),
  status: emptyFilter(FilterMatchMode.EQUALS),
  connection: emptyFilter(CustomFilterMatchMode.DEVICE_CONNECTION),
  connection_mode: emptyFilter(CustomFilterMatchMode.DEVICE_MODE),
  sort: { value: 'approved_at_desc' as DeviceSortKey, matchMode: FilterMatchMode.EQUALS }
})

const filterFields = computed<FilterFieldDef[]>(() => [
  { key: 'global', label: '搜索', type: 'text', placeholder: '名称、指纹、版本或客户端 ID', matchMode: CustomFilterMatchMode.DEVICE_SEARCH },
  { key: 'status', label: '授权状态', type: 'select', options: deviceStatusOptions, placeholder: '全部状态', showClear: true, matchMode: FilterMatchMode.EQUALS },
  { key: 'connection', label: '连接状态', type: 'select', options: deviceOnlineOptions, placeholder: '全部状态', showClear: true, matchMode: CustomFilterMatchMode.DEVICE_CONNECTION },
  { key: 'connection_mode', label: '连接模式', type: 'select', options: deviceModeFilterOptions, placeholder: '全部模式', showClear: true, matchMode: CustomFilterMatchMode.DEVICE_MODE },
  { key: 'sort', label: '排序', type: 'select', options: deviceSortOptions, matchMode: FilterMatchMode.EQUALS, required: true, showClear: false }
])

const hasActiveFilters = computed(() => {
  const f = filters.value
  return Boolean(
    (typeof f.global?.value === 'string' && f.global.value.trim()) ||
    f.status?.value ||
    f.connection?.value ||
    f.connection_mode?.value
  )
})

function customFilter(row: Device, model: FiltersModel): boolean {
  const status = row.status || 'approved'
  if (model.status?.value && status !== model.status.value) return false
  if (!deviceMatchesConnectionFilter(row, (model.connection?.value as DeviceConnectionFilter) || '')) return false
  const mode = row.connection_mode === 'listen' ? 'listen' : 'pull'
  if (model.connection_mode?.value && mode !== model.connection_mode.value) return false
  const needle = String(model.global?.value ?? '').trim().toLowerCase()
  if (needle) {
    const fingerprint = formatPublicKeyFingerprint(row.client_id).toLowerCase()
    const connectionLabel = deviceConnectionLabel(row)
    const haystack = [
      row.label,
      row.client_id,
      fingerprint,
      row.client_version,
      status,
      row.connection_mode,
      row.requested_mode,
      row.forced_mode,
      row.online ? 'online connected 已连接' : 'offline 未连接',
      connectionLabel,
      row.session_end_reason,
      row.session_end_detail,
      row.session_end_label
    ].map((part) => String(part || '').toLowerCase()).join(' ')
    if (!haystack.includes(needle)) return false
  }
  return true
}

function transformFiltered(rows: Device[]): Device[] {
  const key = (filters.value.sort?.value as DeviceSortKey) || 'approved_at_desc'
  return sortDevicesStable(rows, key)
}

async function changePageSize(value: number) {
  await admin.updatePageSize(value)
  page.value = 1
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

function openEditDevice(device: Device) {
  editingDevice.value = device
  editingLabel.value = device.label ?? ''
  editingForcedMode.value = device.forced_mode || 'auto'
  editDeviceDialogVisible.value = true
}

async function approveDevice(event: FormSubmitEvent<Record<string, any>>) {
  if (!event.valid || !event.values.clientId?.trim()) return
  admin.approving = true
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
    await admin.loadDevices()
    toast.add({ severity: 'success', summary: '客户端已批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '批准失败', life: 3200 })
  } finally {
    admin.approving = false
  }
}

async function approvePendingDevice() {
  if (!approvingDevice.value) return
  admin.approving = true
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
    await admin.loadDevices()
    toast.add({ severity: 'success', summary: '客户端已批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '批准失败', life: 3200 })
  } finally {
    admin.approving = false
  }
}

async function renameDevice() {
  if (!editingDevice.value) return
  admin.approving = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(editingDevice.value.client_id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: editingLabel.value.trim(), forced_mode: editingForcedMode.value })
    })
    if (!response.ok) throw new Error((await response.json()).error ?? '重命名失败')
    editDeviceDialogVisible.value = false
    await admin.loadDevices()
    toast.add({ severity: 'success', summary: '客户端名称已更新', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '重命名失败', life: 3200 })
  } finally {
    admin.approving = false
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
  admin.approving = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(device.client_id)}`, { method: 'DELETE' })
    if (!response.ok) throw new Error((await response.json()).error ?? '取消批准失败')
    await admin.loadDevices()
    toast.add({ severity: 'success', summary: '客户端已取消批准', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '取消批准失败', life: 3200 })
  } finally {
    admin.approving = false
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
  admin.approving = true
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(device.client_id)}/disconnect`, { method: 'POST' })
    if (!response.ok) throw new Error((await response.json()).error ?? '断开连接失败')
    await admin.loadDevices()
    toast.add({ severity: 'success', summary: '客户端已断开连接', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '断开连接失败', life: 3200 })
  } finally {
    admin.approving = false
  }
}

function changeDeviceLogPage(nextPage: number) {
  if (admin.deviceLogsDevice) void admin.openDeviceLogs(admin.deviceLogsDevice, nextPage)
}
</script>

<template>
  <section class="content">
    <div class="section-head">
      <div>
        <h2>客户端</h2>
        <p>{{ filteredCount }} / {{ admin.devices.length }} 个客户端</p>
      </div>
      <Button label="批准公钥" icon="pi pi-key" @click="openManualApproveDevice" />
    </div>

    <FilterableDataTable
      :value="admin.devices"
      :loading="admin.devicesLoading"
      :page="page"
      :pageSize="settings.pageSize"
      :pageSizeOptions="pageSizeOptions"
      v-model:filters="filters"
      :filterFields="filterFields"
      :customFilter="customFilter"
      :transformFiltered="transformFiltered"
      @update:page="page = $event"
      @update:pageSize="changePageSize"
      @filtered-count="filteredCount = $event"
    >
      <template #empty>
        {{ admin.devicesLoading ? '加载中…' : (hasActiveFilters ? '没有符合筛选条件的客户端' : '暂无客户端') }}
      </template>
      <Column field="approved_at" header="时间" style="width: 13rem"><template #body="slotProps">{{ formatDateTime(slotProps.data.approved_at) }}</template></Column>
      <Column field="label" header="名称" style="width: 14rem"><template #body="slotProps">{{ slotProps.data.label || '未命名客户端' }}</template></Column>
      <Column field="client_version" header="当前版本" style="width: 9rem"><template #body="slotProps">{{ slotProps.data.client_version || '未知' }}</template></Column>
      <Column field="client_id" header="客户端唯一标识 (公钥 SHA256)"><template #body="slotProps">{{ formatPublicKeyFingerprint(slotProps.data.client_id) }}</template></Column>
      <Column field="status" header="授权状态" style="width: 8rem"><template #body="slotProps">
        <Tag :value="deviceStatusLabel(slotProps.data.status)" :severity="deviceStatusSeverity(slotProps.data.status)" />
      </template></Column>
      <Column field="online" header="连接状态" style="width: 10rem"><template #body="slotProps">
        <Tag
          :value="deviceConnectionLabel(slotProps.data)"
          :severity="deviceConnectionSeverity(slotProps.data)"
          :title="deviceConnectionTitle(slotProps.data, formatDateTime)"
        />
      </template></Column>
      <Column header="连接模式" style="width: 12rem"><template #body="slotProps">
        <Tag :value="slotProps.data.connection_mode === 'listen' ? '监听模式' : '从模式'" :severity="slotProps.data.connection_mode === 'listen' ? 'info' : 'secondary'" />
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
          <Button v-if="slotProps.data.status !== 'approved'" :label="slotProps.data.status === 'revoked' ? '重新授权' : '批准'" icon="pi pi-check" size="small" :loading="admin.approving" @click="openApproveDevice(slotProps.data)" />
          <Button v-else label="编辑" icon="pi pi-pencil" text size="small" @click="openEditDevice(slotProps.data)" />
          <Button label="日志" icon="pi pi-file" text size="small" @click="admin.openDeviceLogs(slotProps.data)" />
          <Button v-if="slotProps.data.online" label="断开" icon="pi pi-power-off" text severity="warn" size="small" :loading="admin.approving" @click="disconnectDevice(slotProps.data)" />
          <Button v-if="slotProps.data.status === 'approved'" label="取消批准" icon="pi pi-times" text severity="danger" size="small" :loading="admin.approving" @click="revokeDevice(slotProps.data)" />
        </div>
      </template></Column>
    </FilterableDataTable>
  </section>

  <Dialog v-model:visible="deviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="批准客户端公钥" class="app-dialog" :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
    <Form class="form" :initialValues="{ clientId: '', label: '' }" @submit="approveDevice">
      <label>客户端唯一标识 (公钥 SHA256)
        <InputText name="clientId" v-model="newClientId" placeholder="SHA256:Base64 或 64 位十六进制字符串" />
      </label>
      <label>名称<InputText name="label" v-model="deviceLabel" placeholder="给客户端 (公钥) 命名" /></label>
      <label>连接模式
        <Select name="forced_mode" v-model="approvingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions">
        <Button type="submit" label="批准" icon="pi pi-check" :loading="admin.approving" :disabled="!validFingerprintInput(newClientId)" />
      </div>
    </Form>
  </Dialog>

  <Dialog v-model:visible="editDeviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="编辑客户端" class="app-dialog" :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="renameDevice">
      <label>名称<InputText v-model="editingLabel" placeholder="客户端名称" /></label>
      <label>连接模式
        <Select v-model="editingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions"><Button type="submit" label="保存" icon="pi pi-save" :loading="admin.approving" /></div>
    </Form>
  </Dialog>

  <Dialog v-model:visible="approveDeviceDialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="批准客户端" class="app-dialog" :style="{ width: 'min(30rem, calc(100vw - 2rem))' }">
    <Form class="form" @submit="approvePendingDevice">
      <label>客户端唯一标识 (公钥 SHA256)
        <InputText :modelValue="approvingDevice ? formatPublicKeyFingerprint(approvingDevice.client_id) : ''" readonly />
      </label>
      <label>名称<InputText v-model="approvingLabel" placeholder="给客户端命名（可选）" autofocus /></label>
      <label>连接模式
        <Select v-model="approvingForcedMode" :options="connectionModeOptions" optionLabel="label" optionValue="value" />
      </label>
      <div class="dialog-actions"><Button type="submit" label="批准" icon="pi pi-check" :loading="admin.approving" /></div>
    </Form>
  </Dialog>

  <Dialog
    v-model:visible="admin.deviceLogsDialogVisible"
    modal
    closable
    :showHeader="true"
    :closeOnEscape="false"
    :draggable="false"
    position="center"
    :header="`${admin.deviceLogsDevice?.label || (admin.deviceLogsDevice ? formatPublicKeyFingerprint(admin.deviceLogsDevice.client_id) : '')} 的客户端本地日志`"
    class="app-dialog"
    :style="{ width: 'min(70rem, calc(100vw - 2rem))' }"
  >
    <TablePagination
      :page="admin.deviceLogsPage"
      :total="admin.deviceLogsTotal"
      :pageSize="settings.pageSize"
      :pageSizeOptions="pageSizeOptions"
      :loading="admin.deviceLogsLoading"
      @update:page="changeDeviceLogPage"
      @update:pageSize="admin.changeDeviceLogPageSize"
    />
    <div>
      <Button label="刷新" icon="pi pi-refresh" text :loading="admin.deviceLogsLoading" @click="admin.deviceLogsDevice && admin.openDeviceLogs(admin.deviceLogsDevice, admin.deviceLogsPage)" />
    </div>
    <Skeleton v-if="admin.deviceLogsLoading" height="20rem" />
    <DataTable v-else :value="admin.deviceLogEntries" stripedRows responsiveLayout="scroll" class="queue-table">
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
