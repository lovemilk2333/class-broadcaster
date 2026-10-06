<script setup lang="ts" generic="T extends Record<string, any>">
/**
 * DataTable + TablePagination with a toolbar that drives PrimeVue FilterService filters.
 *
 * Filter model shape (PrimeVue DataTable-compatible):
 *   { global?: { value, matchMode }, [field]: { value, matchMode } }
 *
 * Custom match modes can be registered via FilterService.register before use
 * (see utils/filters.ts). Toolbar fields are declared via `filterFields`.
 */
import { computed, watch } from 'vue'
import { FilterMatchMode, FilterService } from '@primevue/core/api'
import DataTable from 'primevue/datatable'
import { Form } from '@primevue/forms'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import TablePagination from './TablePagination.vue'

export type FilterMeta = {
  value: unknown
  matchMode?: string
}

export type FiltersModel = Record<string, FilterMeta>

export type FilterFieldDef = {
  /** Key in the filters model (use `global` for the search box). */
  key: string
  label: string
  type?: 'text' | 'select'
  placeholder?: string
  showClear?: boolean
  options?: { label: string; value: unknown }[]
  optionLabel?: string
  optionValue?: string
  /** Match mode written into filters[key] when the control changes. */
  matchMode?: string
  /** Disable clearing / treat as always-present (e.g. sort selects). */
  required?: boolean
}

const props = withDefaults(
  defineProps<{
    value: T[]
    loading?: boolean
    page: number
    pageSize: number
    pageSizeOptions: number[]
    /** When omitted, total is derived from filtered rows. */
    total?: number
    filters: FiltersModel
    filterFields?: FilterFieldDef[]
    /** Fields used by the `global` filter (FilterMatchMode.CONTAINS by default). */
    globalFilterFields?: string[]
    /** Optional custom predicate; when set, bypasses FilterService for row matching. */
    customFilter?: (row: T, filters: FiltersModel) => boolean
    /** Optional post-filter transform (e.g. device sort). */
    transformFiltered?: (rows: T[]) => T[]
    emptyMessage?: string
    tableClass?: string
  }>(),
  {
    loading: false,
    filterFields: () => [],
    globalFilterFields: () => [],
    emptyMessage: '暂无数据',
    tableClass: 'queue-table'
  }
)

const emit = defineEmits<{
  (event: 'update:page', page: number): void
  (event: 'update:pageSize', size: number): void
  (event: 'update:filters', filters: FiltersModel): void
  (event: 'filtered-count', count: number): void
}>()

function resolveField(row: T, field: string): unknown {
  if (!field.includes('.')) return (row as Record<string, unknown>)[field]
  return field.split('.').reduce<unknown>((acc, part) => {
    if (acc == null || typeof acc !== 'object') return undefined
    return (acc as Record<string, unknown>)[part]
  }, row)
}

function isFilterActive(meta: FilterMeta | undefined): boolean {
  if (!meta) return false
  const value = meta.value
  if (value === null || value === undefined || value === '') return false
  if (Array.isArray(value) && value.length === 0) return false
  return true
}

function rowMatchesFilters(row: T): boolean {
  if (props.customFilter) return props.customFilter(row, props.filters)

  const active = Object.entries(props.filters).filter(([, meta]) => isFilterActive(meta))
  if (active.length === 0) return true

  for (const [field, meta] of active) {
    const matchMode = meta.matchMode || FilterMatchMode.CONTAINS
    // PrimeVue typings declare `filters` as a nested interface; runtime object holds the matchers.
    const constraint = (FilterService as unknown as { filters: Record<string, Function> }).filters[matchMode]
    if (typeof constraint !== 'function') continue

    if (field === 'global') {
      const fields = props.globalFilterFields
      if (!fields.length) continue
      const hit = fields.some((globalField) => {
        const data = resolveField(row, globalField)
        return constraint(data, meta.value, undefined)
      })
      if (!hit) return false
      continue
    }

    const data = resolveField(row, field)
    if (!constraint(data, meta.value, undefined)) return false
  }
  return true
}

const filteredRows = computed(() => {
  let rows = props.value.filter((row) => rowMatchesFilters(row))
  if (props.transformFiltered) rows = props.transformFiltered(rows)
  return rows
})

const filteredTotal = computed(() => props.total ?? filteredRows.value.length)

const totalPages = computed(() => Math.max(1, Math.ceil(filteredTotal.value / Math.max(1, props.pageSize))))

const safePage = computed(() => Math.min(totalPages.value, Math.max(1, props.page)))

const pagedRows = computed(() => {
  const first = (safePage.value - 1) * props.pageSize
  return filteredRows.value.slice(first, first + props.pageSize)
})

watch(
  filteredTotal,
  (count) => emit('filtered-count', count),
  { immediate: true }
)

watch(
  () => [filteredTotal.value, props.pageSize, props.page] as const,
  () => {
    if (props.page !== safePage.value) emit('update:page', safePage.value)
  }
)

watch(
  () => {
    // Ignore keys marked required (e.g. device sort) so changing sort alone
    // does not force a jump back to page 1 unless other filters change.
    const snapshot: Record<string, unknown> = {}
    for (const [key, meta] of Object.entries(props.filters)) {
      const field = props.filterFields.find((item) => item.key === key)
      if (field?.required) continue
      snapshot[key] = meta?.value ?? null
    }
    return JSON.stringify(snapshot)
  },
  () => {
    if (props.page !== 1) emit('update:page', 1)
  }
)

function setFilterValue(key: string, value: unknown, matchMode?: string) {
  const next: FiltersModel = { ...props.filters }
  const existing = next[key]
  next[key] = {
    value,
    matchMode: matchMode || existing?.matchMode || (key === 'global' ? FilterMatchMode.CONTAINS : FilterMatchMode.EQUALS)
  }
  emit('update:filters', next)
}

function filterControlValue(key: string) {
  return props.filters[key]?.value ?? (key === 'global' ? '' : null)
}
</script>

<template>
  <div class="filterable-data-table">
    <Form
      v-if="filterFields.length || $slots.toolbar"
      class="table-filters message-filters"
      :initialValues="{}"
    >
      <label v-for="field in filterFields" :key="field.key">
        {{ field.label }}
        <InputText
          v-if="field.type !== 'select'"
          :modelValue="String(filterControlValue(field.key) ?? '')"
          :placeholder="field.placeholder"
          @update:modelValue="setFilterValue(field.key, $event, field.matchMode)"
        />
        <Select
          v-else
          :modelValue="filterControlValue(field.key)"
          :options="field.options"
          :optionLabel="field.optionLabel || 'label'"
          :optionValue="field.optionValue || 'value'"
          :placeholder="field.placeholder"
          :showClear="field.showClear !== false && !field.required"
          @update:modelValue="setFilterValue(field.key, $event ?? (field.required ? filterControlValue(field.key) : ''), field.matchMode)"
        />
      </label>
      <slot name="toolbar" />
    </Form>

    <TablePagination
      :page="safePage"
      :total="filteredTotal"
      :pageSize="pageSize"
      :pageSizeOptions="pageSizeOptions"
      :loading="loading"
      @update:page="emit('update:page', $event)"
      @update:pageSize="emit('update:pageSize', $event)"
    />

    <DataTable
      :value="pagedRows"
      stripedRows
      responsiveLayout="scroll"
      :class="tableClass"
      :loading="loading"
    >
      <template #empty>
        <slot name="empty">{{ emptyMessage }}</slot>
      </template>
      <slot />
    </DataTable>
  </div>
</template>
