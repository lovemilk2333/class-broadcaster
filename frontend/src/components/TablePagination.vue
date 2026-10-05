<script setup lang="ts">
import { ref, watch } from 'vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import SafeInputNumber from './SafeInputNumber.vue'

const props = defineProps<{
  page: number
  total: number
  pageSize: number
  pageSizeOptions: number[]
  loading?: boolean
}>()
const emit = defineEmits<{
  (event: 'update:page', page: number): void
  (event: 'update:pageSize', size: number): void
}>()

const jumpPage = ref<number | null>(props.page)
const totalPages = () => Math.max(1, Math.ceil(props.total / Math.max(1, props.pageSize)))
watch(() => props.page, (page) => { jumpPage.value = page })

function go(page: number) {
  const n = typeof page === 'number' && Number.isFinite(page) ? page : 1
  emit('update:page', Math.min(totalPages(), Math.max(1, Math.trunc(n) || 1)))
}

function jump() {
  const raw = jumpPage.value
  const n = typeof raw === 'number' && Number.isFinite(raw) ? raw : 1
  go(n)
}
</script>

<template>
  <div class="table-pagination">
    <div class="table-pagination-navigation">
      <Button icon="pi pi-angle-double-left" text aria-label="第一页" :disabled="loading || page <= 1" @click="go(1)" />
      <Button icon="pi pi-angle-left" text aria-label="上一页" :disabled="loading || page <= 1" @click="go(page - 1)" />
      <span class="table-pagination-page">第 {{ page }} / {{ totalPages() }} 页</span>
      <Button icon="pi pi-angle-right" text aria-label="下一页" :disabled="loading || page >= totalPages()" @click="go(page + 1)" />
      <Button icon="pi pi-angle-double-right" text aria-label="最后一页" :disabled="loading || page >= totalPages()" @click="go(totalPages())" />
    </div>
    <div class="table-pagination-jump">
      <span>跳转到</span>
      <SafeInputNumber v-model="jumpPage" inputClass="table-pagination-input" :min="1" :max="totalPages()" :useGrouping="false" :disabled="loading" :allowEmpty="true" @keydown.enter="jump" />
      <Button label="跳转" size="small" :disabled="loading" @click="jump" />
    </div>
    <Select :modelValue="pageSize" :options="pageSizeOptions" aria-label="每页条数" :disabled="loading" @update:modelValue="emit('update:pageSize', Number($event))" />
    <span class="table-pagination-total">共 {{ total }} 条</span>
  </div>
</template>
