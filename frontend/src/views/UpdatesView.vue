<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import { Form } from '@primevue/forms'
import MultiSelect from 'primevue/multiselect'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import FilterableDataTable, { type FiltersModel, type FilterFieldDef } from '../components/FilterableDataTable.vue'
import { useAdminStore } from '../stores/admin'
import { useSettingsStore } from '../stores/settings'
import type { UpdateRecord } from '../types'
import { CustomFilterMatchMode, FilterMatchMode, ensureCustomFiltersRegistered, textFilter, emptyFilter } from '../utils/filters'
import { formatDateTime, formatUpdatePackageSize } from '../utils/format'
import {
  pageSizeOptions,
  updateComponentsLabel,
  updateStatusLabel,
  updateStatusOptions,
  updateStatusSeverity,
  updateStatusTitle
} from '../utils/labels'

ensureCustomFiltersRegistered()

const admin = useAdminStore()
const settings = useSettingsStore()
const toast = useToast()

const page = ref(1)
const updateDialogVisible = ref(false)
const updateTargetAll = ref(true)
const updateForce = ref(false)
const updateTargets = ref<string[]>([])
const updatePackages = ref<File[]>([])
const updatePackageDragOver = ref(false)
const updatePackageInputRef = ref<HTMLInputElement | null>(null)

const filters = ref<FiltersModel>({
  global: textFilter('', CustomFilterMatchMode.UPDATE_SEARCH),
  status: emptyFilter(FilterMatchMode.EQUALS)
})

const filterFields: FilterFieldDef[] = [
  { key: 'global', label: '搜索', type: 'text', placeholder: '版本、组件、SHA-256 或更新 ID', matchMode: CustomFilterMatchMode.UPDATE_SEARCH },
  { key: 'status', label: '状态', type: 'select', options: updateStatusOptions, placeholder: '全部状态', showClear: true, matchMode: FilterMatchMode.EQUALS }
]

const hasActiveFilters = computed(() => {
  const f = filters.value
  return Boolean((typeof f.global?.value === 'string' && f.global.value.trim()) || f.status?.value)
})

function customFilter(row: UpdateRecord, model: FiltersModel): boolean {
  if (model.status?.value && row.status !== model.status.value) return false
  const needle = String(model.global?.value ?? '').trim().toLowerCase()
  if (!needle) return true
  const haystack = [
    row.update_id,
    row.version,
    row.client_version,
    row.updater_version,
    row.sha256,
    row.component,
    ...(row.components ?? []),
    row.status,
    row.status_detail,
    row.force ? 'force 强制' : ''
  ].map((part) => String(part || '').toLowerCase()).join(' ')
  return haystack.includes(needle)
}

async function changePageSize(value: number) {
  await admin.updatePageSize(value)
  page.value = 1
}

function updatePackageKey(file: File): string {
  return `${file.name}\0${file.size}\0${file.lastModified}`
}

function isAcceptedUpdatePackage(file: File): boolean {
  const name = file.name.toLowerCase()
  return name.endsWith('.tar.zst') || name.endsWith('.tar.zstd')
}

function addUpdatePackages(files: File[] | FileList | null | undefined) {
  if (admin.publishingUpdate) return
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
  if (accepted.length > 0) updatePackages.value = [...updatePackages.value, ...accepted]
  if (rejected > 0) toast.add({ severity: 'warn', summary: `已忽略 ${rejected} 个非 .tar.zst 文件`, life: 2800 })
  if (duplicated > 0) toast.add({ severity: 'info', summary: `已跳过 ${duplicated} 个重复文件`, life: 2400 })
}

function onUpdatePackageSelect(event: Event) {
  const input = event.target as HTMLInputElement | null
  if (admin.publishingUpdate) {
    if (input) input.value = ''
    return
  }
  addUpdatePackages(input?.files)
  if (input) input.value = ''
}

function openUpdatePackagePicker() {
  if (admin.publishingUpdate) return
  updatePackageInputRef.value?.click()
}

function onUpdatePackageDragEnter(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  if (admin.publishingUpdate) return
  updatePackageDragOver.value = true
}

function onUpdatePackageDragOver(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  if (admin.publishingUpdate) {
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
  if (current && related && current.contains(related)) return
  updatePackageDragOver.value = false
}

function onUpdatePackageDrop(event: DragEvent) {
  event.preventDefault()
  event.stopPropagation()
  updatePackageDragOver.value = false
  if (admin.publishingUpdate) return
  addUpdatePackages(event.dataTransfer?.files)
}

function removeUpdatePackage(index: number) {
  if (admin.publishingUpdate) return
  if (index < 0 || index >= updatePackages.value.length) return
  updatePackages.value = updatePackages.value.filter((_, i) => i !== index)
}

function clearUpdatePackages() {
  if (admin.publishingUpdate) return
  updatePackages.value = []
}

function openUpdateDialog() {
  if (admin.publishingUpdate) return
  updateTargetAll.value = true
  updateForce.value = false
  updateTargets.value = []
  updatePackages.value = []
  updatePackageDragOver.value = false
  updateDialogVisible.value = true
}

async function publishUpdate() {
  if (admin.publishingUpdate) return
  const force = updateForce.value
  const targetAll = updateTargetAll.value
  const targets = force || targetAll
    ? admin.clientOptions.map((item) => item.value)
    : [...updateTargets.value]
  const packages = [...updatePackages.value]
  if (!force && targets.length === 0) {
    toast.add({ severity: 'warn', summary: '请选择至少一个已批准客户端', life: 2600 })
    return
  }
  if (force && admin.clientOptions.length === 0) {
    toast.add({ severity: 'warn', summary: '强制更新需要至少一个已批准客户端', life: 2600 })
    return
  }
  if (packages.length === 0) {
    toast.add({ severity: 'warn', summary: '请选择至少一个更新包', life: 2600 })
    return
  }
  admin.publishingUpdate = true
  let published = 0
  let totalSent = 0
  let totalOffline = 0
  const failures: string[] = []
  try {
    for (const file of packages) {
      try {
        const form = new FormData()
        if (force) form.append('force', 'true')
        else targets.forEach((target) => form.append('target_client_ids', target))
        form.append('package', file)
        const response = await fetch('/api/v1/updates/publish', { method: 'POST', body: form })
        const result = await response.json().catch(() => ({}))
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
      const failedKeys = new Set(
        packages
          .filter((file) => failures.some((item) => item.startsWith(`${file.name}: `)))
          .map(updatePackageKey)
      )
      updatePackages.value = packages.filter((file) => failedKeys.has(updatePackageKey(file)))
    }
    await admin.loadUpdates()
  } finally {
    admin.publishingUpdate = false
  }
}
</script>

<template>
  <section class="content">
    <div class="section-head">
      <div>
        <h2 class="section-title-with-badge">发布客户端更新 <span class="beta-badge" title="实验性功能">Beta</span></h2>
        <p>上传后立即入队；后台校验 SHA-256，通过后推送在线客户端。勾选「强制」时所有已批准客户端必须更新，离线客户端上线后自动补推。失败原因显示在状态列。</p>
      </div>
      <Button label="添加更新" icon="pi pi-cloud-upload" @click="openUpdateDialog" />
    </div>

    <FilterableDataTable
      :value="admin.updates"
      :loading="admin.updatesLoading"
      :page="page"
      :pageSize="settings.pageSize"
      :pageSizeOptions="pageSizeOptions"
      v-model:filters="filters"
      :filterFields="filterFields"
      :customFilter="customFilter"
      tableClass="queue-table update-table"
      @update:page="page = $event"
      @update:pageSize="changePageSize"
    >
      <template #empty>
        {{ admin.updatesLoading ? '加载中…' : (hasActiveFilters ? '没有符合筛选条件的更新记录' : '暂无更新记录') }}
      </template>
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
    </FilterableDataTable>
  </section>

  <Dialog
    v-model:visible="updateDialogVisible"
    modal
    :closable="!admin.publishingUpdate"
    :showHeader="true"
    :closeOnEscape="!admin.publishingUpdate"
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
            'is-dragover': updatePackageDragOver && !admin.publishingUpdate,
            'has-file': updatePackages.length > 0,
            'is-disabled': admin.publishingUpdate
          }"
          role="button"
          :tabindex="admin.publishingUpdate ? -1 : 0"
          :aria-disabled="admin.publishingUpdate"
          @dragenter="!admin.publishingUpdate && onUpdatePackageDragEnter($event)"
          @dragover="!admin.publishingUpdate && onUpdatePackageDragOver($event)"
          @dragleave="!admin.publishingUpdate && onUpdatePackageDragLeave($event)"
          @drop="!admin.publishingUpdate && onUpdatePackageDrop($event)"
        >
          <i class="pi pi-cloud-upload update-package-dropzone-icon" aria-hidden="true" />
          <div class="update-package-dropzone-copy">
            <strong>{{ admin.publishingUpdate ? '正在上传…' : (updatePackages.length > 0 ? `已选择 ${updatePackages.length} 个更新包` : '拖拽更新包到此处') }}</strong>
            <span>{{ admin.publishingUpdate ? '上传完成前不可修改文件、强制选项或目标客户端' : (updatePackages.length > 0 ? '可继续拖入或点下方按钮追加更多文件' : '仅支持 .tar.zst 更新包（绿色安装 .zip 不可上传），可多选或继续追加') }}</span>
          </div>
          <div class="update-package-dropzone-actions" @click.stop>
            <input
              ref="updatePackageInputRef"
              class="update-package-input"
              type="file"
              accept=".tar.zst,application/zstd"
              multiple
              :disabled="admin.publishingUpdate"
              @change="onUpdatePackageSelect"
            />
            <Button type="button" label="选择文件" icon="pi pi-folder-open" :disabled="admin.publishingUpdate" @click="openUpdatePackagePicker" />
            <Button v-if="updatePackages.length > 0" type="button" label="全部清除" text severity="secondary" :disabled="admin.publishingUpdate" @click="clearUpdatePackages" />
          </div>
        </div>
        <ul v-if="updatePackages.length > 0" class="update-package-list">
          <li v-for="(file, index) in updatePackages" :key="updatePackageKey(file)" class="update-package-item">
            <div class="update-package-item-meta">
              <strong class="update-package-name" :title="file.name">{{ file.name }}</strong>
              <span>{{ formatUpdatePackageSize(file.size) }}</span>
            </div>
            <Button type="button" icon="pi pi-times" text rounded severity="secondary" :aria-label="`移除 ${file.name}`" :disabled="admin.publishingUpdate" @click="removeUpdatePackage(index)" />
          </li>
        </ul>
        <small>可一次选择或多个追加更新包；组件、版本、平台和 SHA-256 将由服务端从包内 metadata.json 自动识别并校验。</small>
      </div>
      <div class="target-field">
        <span class="field-label">推送范围</span>
        <span class="target-picker" :class="{ 'is-disabled': admin.publishingUpdate }">
          <Checkbox name="force" v-model="updateForce" binary :disabled="admin.publishingUpdate" />强制更新（所有已批准客户端必须更新）
        </span>
        <span class="target-picker" :class="{ 'is-disabled': updateForce || admin.publishingUpdate }">
          <Checkbox name="target_all" v-model="updateTargetAll" binary :disabled="updateForce || admin.publishingUpdate" />全部客户端
        </span>
        <MultiSelect
          name="target_client_ids"
          v-model="updateTargets"
          :options="admin.clientOptions"
          optionLabel="label"
          optionValue="value"
          filter
          display="chip"
          placeholder="选择已批准客户端"
          :loading="admin.devicesLoading"
          :disabled="admin.publishingUpdate || updateForce || updateTargetAll"
        />
      </div>
      <small v-if="admin.publishingUpdate">正在按提交时的强制/目标设置上传，请勿关闭对话框。</small>
      <small v-else-if="updateForce">强制更新会推送给当前全部已批准客户端；离线或序列号落后的客户端上线后自动补推。客户端列表中的当前版本由最近一次握手上报。</small>
      <small v-else>在线目标立即推送；发布时离线或序列号落后的目标客户端，下次连接后自动补推最高适用更新。客户端列表中的当前版本由最近一次握手上报。</small>
      <div class="dialog-actions">
        <Button
          type="submit"
          :label="admin.publishingUpdate ? '上传中…' : (updateForce ? '强制推送更新' : '添加并推送更新')"
          icon="pi pi-cloud-upload"
          :loading="admin.publishingUpdate"
          :disabled="admin.publishingUpdate || (!updateForce && !updateTargetAll && updateTargets.length === 0) || admin.clientOptions.length === 0 || updatePackages.length === 0"
        />
      </div>
    </Form>
  </Dialog>
</template>
