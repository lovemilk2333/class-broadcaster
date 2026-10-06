import { createRouter, createWebHashHistory, type RouteLocationNormalized } from 'vue-router'
import type { SectionKey } from '../types'

const sectionTitles: Record<SectionKey, string> = {
  messages: '消息队列',
  devices: '客户端',
  updates: '更新发布',
  settings: '配置',
  server: '服务端'
}

export function sectionFromRoute(route: RouteLocationNormalized): SectionKey {
  const name = typeof route.name === 'string' ? route.name : ''
  if (name === 'messages' || name === 'devices' || name === 'updates' || name === 'settings' || name === 'server') {
    return name
  }
  return 'messages'
}

export function titleForSection(section: SectionKey): string {
  return sectionTitles[section]
}

/**
 * Normalize legacy hashes like `#messages` / `#logs` to vue-router hash paths (`#/messages`, `#/server`)
 * before the router boots. Safe to call once at startup.
 */
export function normalizeLegacyHash() {
  if (typeof window === 'undefined') return
  const raw = window.location.hash.replace(/^#/, '')
  if (!raw || raw.startsWith('/')) return
  const section = raw === 'logs' ? 'server' : raw
  if (['messages', 'devices', 'updates', 'settings', 'server'].includes(section)) {
    window.history.replaceState(null, '', `#/${section}`)
  }
}

normalizeLegacyHash()

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/messages' },
    { path: '/logs', redirect: '/server' },
    {
      path: '/messages',
      name: 'messages',
      component: () => import('../views/MessagesView.vue'),
      meta: { section: 'messages' as SectionKey, title: sectionTitles.messages }
    },
    {
      path: '/devices',
      name: 'devices',
      component: () => import('../views/DevicesView.vue'),
      meta: { section: 'devices' as SectionKey, title: sectionTitles.devices }
    },
    {
      path: '/updates',
      name: 'updates',
      component: () => import('../views/UpdatesView.vue'),
      meta: { section: 'updates' as SectionKey, title: sectionTitles.updates, beta: true }
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { section: 'settings' as SectionKey, title: sectionTitles.settings }
    },
    {
      path: '/server',
      name: 'server',
      component: () => import('../views/ServerView.vue'),
      meta: { section: 'server' as SectionKey, title: sectionTitles.server }
    },
    { path: '/:pathMatch(.*)*', redirect: '/messages' }
  ]
})

export default router
