<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import { Form } from '@primevue/forms'
import type { FormSubmitEvent } from '@primevue/forms'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import FilterableDataTable, { type FiltersModel, type FilterFieldDef } from '../components/FilterableDataTable.vue'
import SafeInputNumber from '../components/SafeInputNumber.vue'
import { useAdminStore } from '../stores/admin'
import { useSettingsStore } from '../stores/settings'
import type { Message } from '../types'
import { CustomFilterMatchMode, FilterMatchMode, ensureCustomFiltersRegistered, textFilter, emptyFilter } from '../utils/filters'
import { displayDurationMs, formatDateTime, formatDisplayDurationMs, formatPublicKeyFingerprint, messagePreview } from '../utils/format'
import {
  displayDurationLabel,
  displayPositionLabel,
  displayPositionOptions,
  messageStatusOptions,
  messageTtsOptions,
  pageSizeOptions,
  statusLabel,
  statusSeverity
} from '../utils/labels'
import { deliveryCounts, deliveryCountsTitle } from '../utils/messages'

ensureCustomFiltersRegistered()

const admin = useAdminStore()
const settings = useSettingsStore()
const toast = useToast()

const page = ref(1)
const dialogVisible = ref(false)
const messagePreviewVisible = ref(false)
const previewMessage = ref<Message | null>(null)

const content = ref('')
const priority = ref(1024)
const targetAll = ref(true)
const selectedTargets = ref<string[]>([])
const ttsEnabled = ref(true)
const useDefaultMessagePosition = ref(true)
const messageDisplayPosition = ref(settings.defaultDisplayPosition)
const useDefaultMessageDuration = ref(true)
const messageDisplayRatio = ref(settings.defaultDisplayDurationRatio)

const filters = ref<FiltersModel>({
  global: textFilter('', CustomFilterMatchMode.MESSAGE_SEARCH),
  status: emptyFilter(FilterMatchMode.EQUALS),
  display_position: emptyFilter(FilterMatchMode.EQUALS),
  tts_enabled: { value: 'all', matchMode: CustomFilterMatchMode.MESSAGE_TTS }
})

const filterFields: FilterFieldDef[] = [
  { key: 'global', label: '搜索', type: 'text', placeholder: '内容、客户端或消息 ID', matchMode: CustomFilterMatchMode.MESSAGE_SEARCH },
  { key: 'status', label: '状态', type: 'select', options: messageStatusOptions, placeholder: '全部状态', showClear: true, matchMode: FilterMatchMode.EQUALS },
  { key: 'display_position', label: '显示位置', type: 'select', options: displayPositionOptions, placeholder: '全部位置', showClear: true, matchMode: FilterMatchMode.EQUALS },
  { key: 'tts_enabled', label: '语音', type: 'select', options: messageTtsOptions, matchMode: CustomFilterMatchMode.MESSAGE_TTS, required: true }
]

const sendFormValid = computed(() => content.value.trim().length > 0 && (targetAll.value || selectedTargets.value.length > 0))
const sendFormHint = computed(() => {
  if (!content.value.trim()) return '请输入消息内容。'
  if (!targetAll.value && selectedTargets.value.length === 0) return '请选择至少一个客户端，或勾选全部客户端。'
  return ''
})

const effectiveMessageDisplayRatio = computed(() => {
  const raw = useDefaultMessageDuration.value
    ? admin.configForm.display_duration_ratio ?? settings.defaultDisplayDurationRatio
    : messageDisplayRatio.value ?? 0
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) ? n : 0
})

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

function customFilter(row: Message, model: FiltersModel): boolean {
  const search = model.global?.value
  if (search && String(search).trim()) {
    const needle = String(search).trim().toLowerCase()
    const target = (row.target_client_ids ?? []).join(' ').toLowerCase()
    if (!row.content.toLowerCase().includes(needle) && !target.includes(needle) && !row.message_id.toLowerCase().includes(needle)) {
      return false
    }
  }
  if (model.status?.value && row.status !== model.status.value) return false
  if (model.display_position?.value && row.display_position !== model.display_position.value) return false
  const tts = model.tts_enabled?.value
  if (tts === 'enabled' && !row.tts_enabled) return false
  if (tts === 'disabled' && row.tts_enabled) return false
  return true
}

watch(() => settings.defaultDisplayPosition, (value) => {
  if (useDefaultMessagePosition.value) messageDisplayPosition.value = value
})
watch(() => settings.defaultDisplayDurationRatio, (value) => {
  if (useDefaultMessageDuration.value) messageDisplayRatio.value = value
})

async function changePageSize(value: number) {
  await admin.updatePageSize(value)
  page.value = 1
}

function openMessagePreview(message: Message) {
  previewMessage.value = message
  messagePreviewVisible.value = true
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
  admin.sending = true
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
    await admin.loadMessages()
    toast.add({ severity: 'success', summary: '已加入队列', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '发送失败', life: 3200 })
  } finally {
    admin.sending = false
  }
}
</script>

<template>
  <section class="content">
    <div class="metrics">
      <Card><template #content><span>待投递</span><strong>{{ admin.pendingCount }}</strong></template></Card>
      <Card><template #content><span>消息总数</span><strong>{{ admin.messages.length }}</strong></template></Card>
      <Card><template #content><span>服务端 C2S TLS 端口</span><strong>{{ admin.serverTlsPort }}</strong></template></Card>
      <Card><template #content><span>服务端前端 / API 端口</span><strong>{{ admin.serverHttpPort }}</strong></template></Card>
      <Card><template #content><span>服务端版本</span><strong>{{ admin.serverVersionLabel }}</strong></template></Card>
    </div>
    <div class="section-head">
      <div>
        <h2>最近消息</h2>
        <p>优先级数字越小，优先程度越高，称为“高优先级”；同优先级按入队顺序处理</p>
      </div>
      <Button label="发送消息" icon="pi pi-send" @click="dialogVisible = true" />
    </div>

    <FilterableDataTable
      :value="admin.messages"
      :loading="admin.messagesLoading"
      :page="page"
      :pageSize="settings.pageSize"
      :pageSizeOptions="pageSizeOptions"
      v-model:filters="filters"
      :filterFields="filterFields"
      :customFilter="customFilter"
      emptyMessage="暂无消息"
      @update:page="page = $event"
      @update:pageSize="changePageSize"
    >
      <Column header="#" style="width: 6rem"><template #body="slotProps">{{ admin.messageOrdinal(slotProps.data) }}</template></Column>
      <Column field="created_at" header="发送时间" style="width: 13rem"><template #body="slotProps">{{ formatDateTime(slotProps.data.created_at) }}</template></Column>
      <Column field="content" header="内容" style="min-width: 18rem; max-width: 32rem"><template #body="slotProps">
        <Button class="message-preview-button" text :label="messagePreview(slotProps.data.content)" @click="openMessagePreview(slotProps.data)" />
      </template></Column>
      <Column field="priority" header="优先级（数字越小优先）" style="width: 13rem" />
      <Column field="target_client_ids" header="目标" style="width: 14rem"><template #body="slotProps">{{
        slotProps.data.target_client_ids?.map(formatPublicKeyFingerprint).join(', ') || '全部客户端' }}</template></Column>
      <Column field="status" header="状态" style="width: 9rem"><template #body="slotProps">
        <Tag :value="statusLabel(slotProps.data.status)" :severity="statusSeverity(slotProps.data.status)" />
      </template></Column>
      <Column header="客户端" style="width: 16rem; min-width: 14rem">
        <template #body="slotProps">
          <div
            v-for="counts in [deliveryCounts(slotProps.data)]"
            :key="slotProps.data.message_id"
            class="delivery-tags"
            :title="deliveryCountsTitle(slotProps.data)"
          >
            <Tag :value="`显示 ${counts.completed}`" :severity="counts.completed > 0 ? 'success' : 'secondary'" />
            <Tag :value="`接收 ${counts.received}`" :severity="counts.received > 0 ? 'info' : 'secondary'" />
            <Tag v-if="counts.failed > 0" :value="`失败 ${counts.failed}`" severity="danger" />
            <Tag :value="`共 ${counts.total}`" severity="secondary" />
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
          :loading="admin.withdrawingMessageId === slotProps.data.message_id"
          :disabled="admin.withdrawingMessageId !== null && admin.withdrawingMessageId !== slotProps.data.message_id"
          @click="admin.withdrawMessage(slotProps.data)"
        />
      </template></Column>
    </FilterableDataTable>
  </section>

  <Dialog v-model:visible="dialogVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" class="app-dialog" :style="{ width: 'min(34rem, calc(100vw - 2rem))' }">
    <template #header><div class="send-dialog-header"><span>发送消息</span><small v-if="sendFormHint" class="form-hint">{{ sendFormHint }}</small></div></template>
    <Form class="form" :initialValues="{ content: '', priority: 1024, target_all: true, target_client_ids: [], tts_enabled: true }" @submit="sendMessage">
      <label>内容<Textarea name="content" v-model="content" rows="5" autoResize /></label>
      <label class="checkbox-label"><Checkbox name="tts_enabled" v-model="ttsEnabled" binary />朗读消息内容</label>
      <div class="form-row">
        <label class="checkbox-label"><Checkbox v-model="useDefaultMessagePosition" binary />使用默认消息显示位置</label>
        <Select v-model="messageDisplayPosition" :options="displayPositionOptions" optionLabel="label" optionValue="value" :disabled="useDefaultMessagePosition" />
      </div>
      <div class="form-row">
        <label class="checkbox-label"><Checkbox v-model="useDefaultMessageDuration" binary />使用默认显示比率</label>
        <label>单条消息显示比率
          <SafeInputNumber v-model="messageDisplayRatio" :min="0" :max="120" :step="0.1" mode="decimal" :minFractionDigits="0" :maxFractionDigits="2" :useGrouping="false" :disabled="useDefaultMessageDuration" :allowEmpty="false" :emptyValue="0" />
          <small>按字符权重计算；设为 0 时需手动关闭。</small>
          <small class="display-duration-hint">{{ estimatedDisplayDurationHint }}</small>
        </label>
      </div>
      <div class="form-row">
        <label>优先级
          <SafeInputNumber name="priority" v-model="priority" :min="0" :max="65535" :useGrouping="false" showButtons buttonLayout="vertical" :allowEmpty="false" :emptyValue="0" />
        </label>
        <div class="target-field">
          <span class="field-label">目标客户端</span>
          <span class="target-picker"><Checkbox name="target_all" v-model="targetAll" binary />全部客户端</span>
          <MultiSelect name="target_client_ids" v-model="selectedTargets" :options="admin.clientOptions" optionLabel="label" optionValue="value" placeholder="选择客户端" display="chip" filter :disabled="targetAll" />
        </div>
      </div>
      <div class="dialog-actions"><Button type="submit" label="加入队列" icon="pi pi-send" :loading="admin.sending" :disabled="!sendFormValid" /></div>
    </Form>
  </Dialog>

  <Dialog v-model:visible="messagePreviewVisible" modal closable :showHeader="true" :closeOnEscape="false" :draggable="false" position="center" header="消息内容" class="app-dialog" :style="{ width: 'min(46rem, calc(100vw - 2rem))' }">
    <div class="message-preview-content">{{ previewMessage?.content || '' }}</div>
  </Dialog>
</template>
