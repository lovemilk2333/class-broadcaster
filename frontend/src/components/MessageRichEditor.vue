<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'
import { fileToMessageImageDataUri, sanitizeMessageHtml } from '../utils/richtext'

const props = defineProps<{
  modelValue: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const toast = useToast()
const editorRef = ref<HTMLDivElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const applyingExternal = ref(false)
/** After toolbar format at EOF, keep caret outside style wrappers across sanitize rewrites. */
const preferUnstyledTail = ref(false)
let syncTimer: ReturnType<typeof setTimeout> | null = null

function focusEditor() {
  const el = editorRef.value
  if (!el) return
  el.focus()
}

function currentSelectionInside(): boolean {
  const el = editorRef.value
  const sel = window.getSelection()
  if (!el || !sel || sel.rangeCount === 0) return false
  const node = sel.anchorNode
  return !!(node && el.contains(node))
}

function restoreCaretToEnd() {
  const el = editorRef.value
  if (!el) return
  const range = document.createRange()
  range.selectNodeContents(el)
  range.collapse(false)
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
}

const STYLE_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'SPAN'])

function isStyleElement(node: Node | null): node is HTMLElement {
  return !!(node && node.nodeType === Node.ELEMENT_NODE && STYLE_TAGS.has((node as HTMLElement).tagName))
}

function caretAtEndOf(node: Node, offset: number): boolean {
  if (node.nodeType === Node.TEXT_NODE) {
    return offset >= (node.textContent || '').length
  }
  return offset >= node.childNodes.length
}

/**
 * Place caret in an unstyled text node after the last style wrapper.
 * Uses a regular space (not ZWSP) so sanitizeMessageHtml keeps it — ZWSP was
 * stripped on sync and the caret snapped back inside `<b>`/`<i>`.
 */
function placeCaretAfterTrailingStyles() {
  const el = editorRef.value
  const sel = window.getSelection()
  if (!el || !sel) return

  let lastStyle: HTMLElement | null = null
  for (let i = el.childNodes.length - 1; i >= 0; i--) {
    const child = el.childNodes[i]
    if (isStyleElement(child)) {
      lastStyle = child
      break
    }
    if (child.nodeType === Node.TEXT_NODE) {
      const text = child.textContent || ''
      if (text.replace(/\u200b/g, '').trim() === '') continue
      // Trailing plain text already exists — put caret at its end.
      const range = document.createRange()
      range.setStart(child, text.length)
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
      preferUnstyledTail.value = true
      return
    }
    if (child.nodeType === Node.ELEMENT_NODE) {
      // Non-style element (e.g. br/img) at end — caret after it is fine.
      const range = document.createRange()
      range.setStartAfter(child)
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
      preferUnstyledTail.value = true
      return
    }
  }
  if (!lastStyle) {
    restoreCaretToEnd()
    preferUnstyledTail.value = true
    return
  }

  let tail = lastStyle.nextSibling
  if (!(tail && tail.nodeType === Node.TEXT_NODE)) {
    tail = document.createTextNode('\u00a0') // nbsp survives sanitize better than ZWSP
    if (lastStyle.nextSibling) el.insertBefore(tail, lastStyle.nextSibling)
    else el.appendChild(tail)
  }
  const range = document.createRange()
  range.setStart(tail, (tail.textContent || '').length)
  range.collapse(true)
  sel.removeAllRanges()
  sel.addRange(range)
  preferUnstyledTail.value = true
}

/**
 * After formatting a range that ends at EOF, browsers leave the caret inside
 * the new style wrapper. Move it out so newly typed text is unstyled.
 */
function exitTrailingStyleWrappers() {
  const el = editorRef.value
  const sel = window.getSelection()
  if (!el || !sel || sel.rangeCount === 0 || !sel.isCollapsed) return
  const anchor = sel.anchorNode
  if (!anchor || !el.contains(anchor)) return
  if (!caretAtEndOf(anchor, sel.anchorOffset)) return

  let walk: Node | null = anchor.nodeType === Node.TEXT_NODE ? anchor.parentNode : anchor
  let insideStyle = false
  while (walk && walk !== el) {
    if (isStyleElement(walk)) {
      insideStyle = true
      break
    }
    walk = walk.parentNode
  }
  if (!insideStyle) {
    preferUnstyledTail.value = true
    return
  }
  placeCaretAfterTrailingStyles()
}

function exec(command: string, value?: string) {
  focusEditor()
  if (!currentSelectionInside()) restoreCaretToEnd()
  try {
    document.execCommand('styleWithCSS', false, 'false')
  } catch {
    /* older browsers */
  }
  const selBefore = window.getSelection()
  const wasRange = !!(selBefore && selBefore.rangeCount > 0 && !selBefore.isCollapsed)
  document.execCommand(command, false, value)
  if (wasRange && (command === 'bold' || command === 'italic' || command === 'underline')) {
    exitTrailingStyleWrappers()
  }
  queueSync()
}

/** Keep selection when pressing toolbar buttons (avoid label/focus steal). */
function onToolbarMouseDown(event: MouseEvent) {
  event.preventDefault()
}

function onInput() {
  // User typed — if they typed into the unstyled tail, keep the preference;
  // if they moved mid-document, clear it on next selection change via blur/focus.
  queueSync()
}

function queueSync() {
  if (syncTimer) clearTimeout(syncTimer)
  syncTimer = setTimeout(() => {
    syncTimer = null
    flushToModel()
  }, 80)
}

function selectionCollapsedAtEnd(): boolean {
  const el = editorRef.value
  const sel = window.getSelection()
  if (!el || !sel || sel.rangeCount === 0 || !sel.isCollapsed) return false
  if (!el.contains(sel.anchorNode)) return false
  const probe = document.createRange()
  probe.selectNodeContents(el)
  probe.collapse(false)
  return (
    (sel.anchorNode === probe.startContainer && sel.anchorOffset === probe.startOffset) ||
    caretAtEndOf(sel.anchorNode!, sel.anchorOffset)
  )
}

function flushToModel() {
  const el = editorRef.value
  if (!el || applyingExternal.value) return
  const html = sanitizeMessageHtml(el.innerHTML)
  if (html !== props.modelValue) emit('update:modelValue', html)
}

function applyFromModel(value: string) {
  const el = editorRef.value
  if (!el) return
  const next = value || ''
  const current = sanitizeMessageHtml(el.innerHTML)
  if (current === next) return
  const focused = document.activeElement === el
  const keepUnstyled = preferUnstyledTail.value && focused
  const keepAtEnd = focused && selectionCollapsedAtEnd()
  applyingExternal.value = true
  el.innerHTML = next || ''
  applyingExternal.value = false
  if (keepUnstyled || keepAtEnd) {
    // Sanitize drops ZWSP; re-create an unstyled tail and park the caret there.
    placeCaretAfterTrailingStyles()
  }
}

watch(
  () => props.modelValue,
  (value) => {
    applyFromModel(value)
  }
)

onMounted(() => {
  applyFromModel(props.modelValue)
})

onBeforeUnmount(() => {
  if (syncTimer) clearTimeout(syncTimer)
  flushToModel()
})

function openImagePicker() {
  fileInputRef.value?.click()
}

async function onImageSelected(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0]
  if (!file) return
  if (!file.type.startsWith('image/')) {
    toast.add({ severity: 'warn', summary: '请选择图片文件', life: 2800 })
    if (input) input.value = ''
    return
  }
  try {
    const { dataUri, width, height } = await fileToMessageImageDataUri(file)
    // Cap display size for classroom boards (~half of maxEdge).
    const displayW = Math.min(width, 480)
    const displayH = Math.max(1, Math.round((height * displayW) / width))
    const tag = `<img src="${dataUri}" alt="" width="${displayW}" height="${displayH}" />`
    focusEditor()
    if (!currentSelectionInside()) restoreCaretToEnd()
    // insertHTML keeps caret; fallback appends when unsupported.
    const ok = document.execCommand('insertHTML', false, tag)
    if (!ok) {
      const el = editorRef.value
      if (el) el.innerHTML = `${el.innerHTML}${tag}`
    }
    preferUnstyledTail.value = false
    await nextTick()
    flushToModel()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: error instanceof Error ? error.message : '插入图片失败',
      life: 3200
    })
  }
  if (input) input.value = ''
}

defineExpose({ flushToModel, focusEditor })
</script>

<template>
  <div class="message-rich-editor">
    <div class="message-rich-toolbar" role="toolbar" aria-label="消息格式" @mousedown="onToolbarMouseDown">
      <Button type="button" label="粗体" icon="pi pi-bold" text size="small" @click="exec('bold')" />
      <Button type="button" label="斜体" icon="pi pi-italic" text size="small" @click="exec('italic')" />
      <Button type="button" label="下划线" icon="pi pi-underline" text size="small" @click="exec('underline')" />
      <Button type="button" label="换行" icon="pi pi-arrow-down-left" text size="small" @click="exec('insertHTML', '<br />')" />
      <Button type="button" label="插入图片" icon="pi pi-image" text size="small" @click="openImagePicker" />
      <input
        ref="fileInputRef"
        type="file"
        accept="image/*"
        style="display: none"
        @change="onImageSelected"
      />
    </div>
    <div
      ref="editorRef"
      class="message-rich-surface"
      contenteditable="true"
      role="textbox"
      aria-multiline="true"
      data-placeholder="输入消息内容…"
      @input="onInput"
      @blur="flushToModel"
      @paste="queueSync"
    />
    <small class="message-rich-hint">工具栏输出白名单 HTML（粗体/斜体/下划线/换行/图片）；TTS 自动去标签。也可粘贴已净化的 HTML。</small>
  </div>
</template>
