<script setup lang="ts">
/**
 * Dialog that embeds the devices table filtered to a set of client IDs
 * (or all devices when targetAll / empty targets + force).
 */
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import Tag from 'primevue/tag'
import FilterableDataTable, { type FiltersModel, type FilterFieldDef } from './FilterableDataTable.vue'
import { useAdminStore } from '../stores/admin'
import { useSettingsStore } from '../stores/settings'
import type { Device, DeviceConnectionFilter, DeviceSortKey } from '../types'
import { CustomFilterMatchMode, FilterMatchMode, ensureCustomFiltersRegistered, textFilter, emptyFilter } from '../utils/filters'
import { sortDevicesStable } from '../utils/devices'
import { formatDateTime, formatPublicKeyFingerprint } from '../utils/format'
import {
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
  pageSizeOptions
} from '../utils/labels'

ensureCustomFiltersRegistered()

const props = defineProps<{
  visible: boolean
  /** When true, show every known device (message “全部客户端” / force update). */
  targetAll?: boolean
  targetClientIds?: string[]
  header?: string
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
}>()

const admin = useAdminStore()
const settings = useSettingsStore()

const page = ref(1)
const filteredCount = ref(0)

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

const targetSet = computed(() => {
  const ids = (props.targetClientIds ?? []).filter((id) => id && id !== '*')
  return new Set(ids)
})

const scopedDevices = computed(() => {
  if (props.targetAll || targetSet.value.size === 0) {
    return admin.devices
  }
  return admin.devices.filter((device) => targetSet.value.has(device.client_id))
})

const dialogHeader = computed(() => {
  if (props.header) return props.header
  if (props.targetAll || targetSet.value.size === 0) return '目标客户端（全部）'
  return `目标客户端（${targetSet.value.size}）`
})

watch(
  () => props.visible,
  (open) => {
    if (open) {
      page.value = 1
      filters.value = {
        global: textFilter('', CustomFilterMatchMode.DEVICE_SEARCH),
        status: emptyFilter(FilterMatchMode.EQUALS),
        connection: emptyFilter(CustomFilterMatchMode.DEVICE_CONNECTION),
        connection_mode: emptyFilter(CustomFilterMatchMode.DEVICE_MODE),
        sort: { value: 'approved_at_desc' as DeviceSortKey, matchMode: FilterMatchMode.EQUALS }
      }
      if (admin.devices.length === 0) void admin.loadDevices()
    }
  }
)

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

function deviceVersionLabel(device: Device): string {
  const version = String(device.client_version || '').trim()
  if (!version) return '未知'
  const build = String(device.client_build_date || '').trim()
  if (!build || build.toLowerCase() === 'dev') return `${version} (dev)`
  return `${version} (${build})`
}

async function changePageSize(value: number) {
  await admin.updatePageSize(value)
  page.value = 1
}
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    closable
    :showHeader="true"
    :closeOnEscape="true"
    :draggable="false"
    position="center"
    :header="dialogHeader"
    class="app-dialog target-clients-dialog"
    :style="{ width: 'min(72rem, calc(100vw - 2rem))' }"
    @update:visible="emit('update:visible', $event)"
  >
    <FilterableDataTable
      :value="scopedDevices"
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
        {{ admin.devicesLoading ? '加载中…' : '没有符合条件的目标客户端' }}
      </template>
      <Column field="label" header="名称" style="width: 14rem"><template #body="slotProps">{{ slotProps.data.label || '未命名客户端' }}</template></Column>
      <Column field="client_version" header="当前版本" style="width: 16rem"><template #body="slotProps">
        <span>{{ deviceVersionLabel(slotProps.data) }}</span>
      </template></Column>
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
      </template></Column>
      <Column field="last_seen" header="最近在线" style="width: 13rem"><template #body="slotProps">{{
        slotProps.data.last_seen ? formatDateTime(slotProps.data.last_seen) : '从未连接' }}</template>
      </Column>
    </FilterableDataTable>
    <template #footer>
      <Button label="关闭" text @click="emit('update:visible', false)" />
    </template>
  </Dialog>
</template>
