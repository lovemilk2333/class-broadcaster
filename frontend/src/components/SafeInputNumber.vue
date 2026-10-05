<script setup lang="ts">
/**
 * PrimeVue InputNumber wrapper that never writes NaN into the bound model.
 * Invalid / empty intermediate input becomes null (blank) or a configured fallback.
 */
import { computed, ref, useAttrs, watch } from 'vue'
import InputNumber from 'primevue/inputnumber'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    modelValue?: number | null
    min?: number
    max?: number
    step?: number
    mode?: 'decimal' | 'currency'
    minFractionDigits?: number
    maxFractionDigits?: number
    useGrouping?: boolean
    showButtons?: boolean
    buttonLayout?: 'stacked' | 'horizontal' | 'vertical'
    disabled?: boolean
    name?: string
    inputClass?: string | object | (string | object)[]
    allowEmpty?: boolean
    /** When empty/invalid and allowEmpty is false, fall back to this (default: min or 0). */
    emptyValue?: number
  }>(),
  {
    useGrouping: false,
    allowEmpty: true,
    mode: 'decimal'
  }
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: number | null): void
}>()

const attrs = useAttrs()
const lastValid = ref<number | null>(sanitize(props.modelValue))

function sanitize(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'object' && value !== null && 'value' in (value as object)) {
    return sanitize((value as { value: unknown }).value)
  }
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return null
  let out = n
  if (typeof props.min === 'number' && out < props.min) out = props.min
  if (typeof props.max === 'number' && out > props.max) out = props.max
  return out
}

function fallbackValue(): number {
  if (typeof props.emptyValue === 'number' && Number.isFinite(props.emptyValue)) return props.emptyValue
  if (typeof props.min === 'number' && Number.isFinite(props.min)) return props.min
  if (lastValid.value !== null) return lastValid.value
  return 0
}

const displayValue = computed({
  get(): number | null {
    return sanitize(props.modelValue)
  },
  set(value: number | null | undefined) {
    commit(value)
  }
})

function commit(value: unknown) {
  const cleaned = sanitize(value)
  if (cleaned === null) {
    if (props.allowEmpty) {
      emit('update:modelValue', null)
      return
    }
    const restore = fallbackValue()
    lastValid.value = restore
    emit('update:modelValue', restore)
    return
  }
  lastValid.value = cleaned
  emit('update:modelValue', cleaned)
}

watch(
  () => props.modelValue,
  (value) => {
    if (typeof value === 'number' && Number.isNaN(value)) {
      const restore = fallbackValue()
      lastValid.value = restore
      emit('update:modelValue', restore)
      return
    }
    const cleaned = sanitize(value)
    if (cleaned !== null) lastValid.value = cleaned
  }
)

/** PrimeVue 4 InputNumber @update:modelValue may emit null/undefined/NaN on bad keystrokes. */
function onModelUpdate(value: number | null | undefined) {
  commit(value)
}

function onBlur() {
  if (props.allowEmpty) return
  if (sanitize(props.modelValue) === null) {
    const restore = fallbackValue()
    lastValid.value = restore
    emit('update:modelValue', restore)
  }
}
</script>

<template>
  <InputNumber
    :modelValue="displayValue"
    v-bind="attrs"
    :name="name"
    :min="min"
    :max="max"
    :step="step"
    :mode="mode"
    :minFractionDigits="minFractionDigits"
    :maxFractionDigits="maxFractionDigits"
    :useGrouping="useGrouping"
    :showButtons="showButtons"
    :buttonLayout="buttonLayout"
    :disabled="disabled"
    :inputClass="inputClass"
    :allowEmpty="allowEmpty"
    @update:modelValue="onModelUpdate"
    @blur="onBlur"
  />
</template>
