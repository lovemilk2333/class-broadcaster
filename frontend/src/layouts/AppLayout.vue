<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import PanelMenu from 'primevue/panelmenu'
import Sidebar from 'primevue/sidebar'
import Toast from 'primevue/toast'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { useAdminStore } from '../stores/admin'
import { sectionFromRoute, titleForSection } from '../router'
import type { SectionKey } from '../types'

const route = useRoute()
const router = useRouter()
const admin = useAdminStore()
const toast = useToast()
const confirm = useConfirm()

const isMobile = ref(false)
const navigationVisible = ref(true)
const sidebarTransitionEnabled = ref(false)
const sidebarBreakpointChanging = ref(false)
let mediaQuery: MediaQueryList | undefined
let onMediaQueryChange: ((event: MediaQueryListEvent) => void) | undefined
let onBeforeUnload: ((event: BeforeUnloadEvent) => void) | undefined
let eventSource: EventSource | undefined
/** Suppress leave-guard confirm while we intentionally revert the route. */
let revertingNavigation = false

const active = computed(() => sectionFromRoute(route))
const pageTitle = computed(() => titleForSection(active.value))

const navigationItems = computed(() => [
  { key: 'messages', label: '消息队列', icon: 'pi pi-megaphone', to: '/messages', class: active.value === 'messages' ? 'nav-active' : '' },
  { key: 'devices', label: '客户端', icon: 'pi pi-desktop', to: '/devices', class: active.value === 'devices' ? 'nav-active' : '' },
  {
    key: 'updates',
    label: '更新发布',
    icon: 'pi pi-cloud-upload',
    beta: true,
    to: '/updates',
    class: [active.value === 'updates' ? 'nav-active' : '', 'nav-beta'].filter(Boolean).join(' ')
  },
  { key: 'settings', label: '配置', icon: 'pi pi-sliders-h', to: '/settings', class: active.value === 'settings' ? 'nav-active' : '' },
  { key: 'server', label: '服务端', icon: 'pi pi-server', to: '/server', class: active.value === 'server' ? 'nav-active' : '' }
])

function navigateTo(section: SectionKey) {
  if (active.value === section) {
    if (isMobile.value) navigationVisible.value = false
    return
  }
  void router.push({ name: section })
}

async function refreshActive() {
  if (active.value === 'settings' && admin.configDirty) {
    confirm.require({
      message: '配置已修改但尚未保存',
      header: '未保存的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '保存',
      rejectLabel: '丢弃',
      rejectClass: 'p-button-text p-button-secondary',
      accept: async () => {
        await admin.saveConfig()
        if (!admin.configDirty) await admin.loadConfig()
      },
      reject: () => admin.discardConfigChanges()
    })
    return
  }
  admin.refreshLoading = true
  try {
    if (active.value === 'devices') await admin.loadDevices()
    else if (active.value === 'updates') { await admin.loadDevices(); await admin.loadUpdates() }
    else if (active.value === 'settings') await admin.loadConfig()
    else if (active.value === 'server') await admin.loadLogViewPage(admin.serverLogViewPage, true)
    else await admin.loadMessages()
  } catch (error) {
    toast.add({ severity: 'error', summary: error instanceof Error ? error.message : '刷新失败', life: 3200 })
  } finally {
    admin.refreshLoading = false
  }
}

function requestServerShutdown() {
  confirm.require({
    message: '确定关闭服务端并向所有客户端发送关闭事件吗？',
    header: '关闭服务端',
    icon: 'pi pi-power-off',
    acceptLabel: '关闭服务端',
    rejectLabel: '取消',
    acceptClass: 'p-button-danger',
    accept: () => void admin.requestServerShutdown()
  })
}

function loadSectionData(section: SectionKey) {
  if (section === 'devices') void admin.loadDevices()
  if (section === 'updates') { void admin.loadDevices(); void admin.loadUpdates() }
  if (section === 'settings') void admin.loadConfig()
  if (section === 'messages') void admin.loadMessages()
  // ServerView mounts its own log loader.
}

const removeBeforeEach = router.beforeEach((to, from, next) => {
  if (revertingNavigation) {
    revertingNavigation = false
    next()
    return
  }
  const fromSection = sectionFromRoute(from)
  const toSection = sectionFromRoute(to)
  if (fromSection === toSection) {
    next()
    return
  }
  if (fromSection === 'settings' && admin.configDirty) {
    next(false)
    confirm.require({
      message: '配置已修改但尚未保存',
      header: '未保存的配置修改',
      icon: 'pi pi-exclamation-triangle',
      acceptLabel: '保存',
      rejectLabel: '丢弃',
      rejectClass: 'p-button-text p-button-secondary',
      accept: async () => {
        await admin.saveConfig()
        if (!admin.configDirty) {
          revertingNavigation = false
          void router.push(to.fullPath)
        }
      },
      reject: () => {
        admin.discardConfigChanges()
        revertingNavigation = false
        void router.push(to.fullPath)
      }
    })
    return
  }
  next()
})

watch(
  () => active.value,
  (section) => {
    if (isMobile.value) navigationVisible.value = false
    loadSectionData(section)
  }
)

onMounted(() => {
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

  onBeforeUnload = (event) => {
    if (!admin.configDirty) return
    event.preventDefault()
    event.returnValue = ''
  }
  window.addEventListener('beforeunload', onBeforeUnload)

  admin.loadHealth().catch(() => undefined)
  admin.loadMessages().catch(() => toast.add({ severity: 'error', summary: '无法连接服务端', life: 3200 }))
  admin.loadDevices().catch(() => undefined)
  if (active.value === 'updates') void admin.loadUpdates()
  if (active.value === 'settings') void admin.loadConfig()
  const preferencesPromise = admin.loadPreferences().catch(() => undefined)
  if (active.value === 'server') {
    void preferencesPromise
  }

  eventSource = new EventSource('/api/v1/events')
  eventSource.addEventListener('refresh', (event) => {
    try {
      const section = JSON.parse((event as MessageEvent).data).section
      if (section === 'all') {
        void Promise.all([
          admin.loadMessages(),
          admin.loadDevices(),
          admin.loadConfig(),
          active.value === 'updates' ? admin.loadUpdates() : Promise.resolve(),
          active.value === 'server' ? admin.loadLogViewPage(admin.serverLogViewPage, true) : Promise.resolve()
        ])
      } else if (section === 'messages') void admin.loadMessages()
      else if (section === 'devices') void admin.loadDevices()
      else if (section === 'updates') { void admin.loadDevices(); void admin.loadUpdates() }
      else if (section === 'config') void admin.loadConfig()
      else if (section === 'server' && active.value === 'server') window.requestAnimationFrame(() => { void admin.loadLogViewPage(admin.serverLogViewPage, true) })
      else if (typeof section === 'string' && section.startsWith('client_logs:')) {
        const clientId = section.slice('client_logs:'.length)
        if (admin.deviceLogsDialogVisible && admin.deviceLogsDevice?.client_id === clientId) {
          void admin.openDeviceLogs(admin.deviceLogsDevice, admin.deviceLogsPage)
        }
      }
    } catch {
      void refreshActive()
    }
  })
})

onBeforeUnmount(() => {
  removeBeforeEach()
  if (mediaQuery && onMediaQueryChange) mediaQuery.removeEventListener('change', onMediaQueryChange)
  if (onBeforeUnload) window.removeEventListener('beforeunload', onBeforeUnload)
  eventSource?.close()
})
</script>

<template>
  <Toast />
  <ConfirmDialog :closable="false" :closeOnEscape="false" :dismissableMask="false" />
  <div class="shell">
    <Sidebar
      v-model:visible="navigationVisible"
      :modal="false"
      :dismissable="false"
      :closeOnEscape="false"
      position="left"
      class="navigation-sidebar"
      :class="{ 'sidebar-no-initial-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }"
      :showCloseIcon="false"
    >
      <div class="navigation-brand"><span class="brand-mark">M</span><strong>lovemilk class broadcaster</strong></div>
      <PanelMenu :model="navigationItems" multiple>
        <template #item="{ item }">
          <a
            class="nav-item-link"
            :class="{ 'nav-item-link--active': item.class?.includes?.('nav-active'), 'nav-item-link--beta': item.beta }"
            :href="`#${item.to}`"
            @click.prevent="navigateTo(item.key as SectionKey)"
          >
            <span v-if="item.icon" :class="item.icon" class="nav-item-icon" />
            <span class="nav-item-label">{{ item.label }}</span>
            <span v-if="item.beta" class="nav-item-beta" title="实验性功能">Beta</span>
          </a>
        </template>
      </PanelMenu>
      <div class="navigation-status">
        <span class="online-dot" />服务端在线
        <span class="server-version" :title="admin.serverVersionLabel">v{{ admin.serverVersionLabel }}</span>
      </div>
    </Sidebar>

    <main class="workspace" :class="{ 'sidebar-open': navigationVisible && !isMobile, 'layout-no-transition': !sidebarTransitionEnabled || sidebarBreakpointChanging }">
      <header class="topbar">
        <div class="topbar-leading">
          <Button
            icon="pi pi-bars"
            text
            rounded
            @click="navigationVisible = !navigationVisible"
            :aria-label="navigationVisible ? '收起导航菜单' : '展开导航菜单'"
            :title="navigationVisible ? '收起导航菜单' : '展开导航菜单'"
          />
          <div class="topbar-title-stack">
            <span class="eyebrow">lovemilk class broadcaster</span>
            <h1 class="page-title">
              <span>{{ pageTitle }}</span>
              <span v-if="active === 'updates'" class="beta-badge" title="实验性功能">Beta</span>
            </h1>
          </div>
        </div>
        <div class="topbar-actions">
          <Button label="刷新当前页面" icon="pi pi-refresh" text aria-label="刷新当前页面数据" title="刷新当前页面数据" :loading="admin.refreshLoading" @click="refreshActive" />
          <Button label="关闭服务端" icon="pi pi-power-off" severity="danger" size="small" :loading="admin.shutdownLoading" @click="requestServerShutdown" />
        </div>
      </header>
      <RouterView />
    </main>
  </div>
</template>
