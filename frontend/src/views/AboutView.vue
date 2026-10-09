<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import { useAdminStore } from '../stores/admin'
import licenses from '../data/third_party_licenses.json'

type LicenseEntry = {
  name: string
  license: string
  url: string
  note?: string
}

const admin = useAdminStore()
const licensesVisible = ref(false)

const title = String((licenses as { title?: string }).title || '开源软件版权声明')
const intro = String((licenses as { intro?: string }).intro || '')
const entries = computed(() => {
  const raw = (licenses as { entries?: LicenseEntry[] }).entries
  return Array.isArray(raw) ? raw : []
})
</script>

<template>
  <section class="content about-content">
    <div class="section-head">
      <div>
        <h2>关于</h2>
        <p>lovemilk class broadcaster 管理端与开源软件版权信息。</p>
      </div>
    </div>

    <Card class="about-card">
      <template #title>lovemilk class broadcaster</template>
      <template #content>
        <dl class="about-meta">
          <div>
            <dt>版本</dt>
            <dd>{{ admin.serverVersion || '未知' }}</dd>
          </div>
          <div>
            <dt>构建时间</dt>
            <dd>{{ admin.serverBuildDate || '未知' }}</dd>
          </div>
          <div>
            <dt>许可证</dt>
            <dd>BSD-3-Clause</dd>
          </div>
        </dl>
        <p class="about-copy">LAN 教室广播系统：服务端推送消息，教室客户端显示并可用离线 TTS 朗读。</p>
        <Button
          label="开源软件版权声明"
          icon="pi pi-book"
          outlined
          @click="licensesVisible = true"
        />
      </template>
    </Card>

    <Card class="about-card about-licenses-preview">
      <template #title>开源软件版权声明</template>
      <template #content>
        <p class="about-copy">{{ intro }}</p>
        <ul class="license-list">
          <li v-for="entry in entries.slice(0, 8)" :key="entry.name + entry.url">
            <strong>{{ entry.name }}</strong>
            <span>{{ entry.license }}</span>
            <a v-if="entry.url" :href="entry.url" target="_blank" rel="noopener noreferrer">{{ entry.url }}</a>
          </li>
        </ul>
        <Button
          v-if="entries.length > 8"
          label="查看全部"
          icon="pi pi-external-link"
          text
          @click="licensesVisible = true"
        />
      </template>
    </Card>

    <Dialog
      v-model:visible="licensesVisible"
      modal
      closable
      :showHeader="true"
      :closeOnEscape="true"
      :draggable="false"
      position="center"
      :header="title"
      class="app-dialog about-licenses-dialog"
      :style="{ width: 'min(40rem, calc(100vw - 2rem))' }"
    >
      <p class="about-copy">{{ intro }}</p>
      <div class="license-scroll">
        <article v-for="(entry, index) in entries" :key="entry.name + entry.url" class="license-entry">
          <h3>{{ index + 1 }}. {{ entry.name }}</h3>
          <p><span class="license-label">许可证</span>{{ entry.license }}</p>
          <p v-if="entry.url">
            <span class="license-label">地址</span>
            <a :href="entry.url" target="_blank" rel="noopener noreferrer">{{ entry.url }}</a>
          </p>
          <p v-if="entry.note"><span class="license-label">说明</span>{{ entry.note }}</p>
        </article>
      </div>
      <template #footer>
        <Button label="关闭" text @click="licensesVisible = false" />
      </template>
    </Dialog>
  </section>
</template>

<style scoped>
.about-meta {
  display: grid;
  gap: 0.75rem;
  margin: 0 0 1rem;
}

.about-meta > div {
  display: grid;
  grid-template-columns: 6rem 1fr;
  gap: 0.5rem;
  align-items: baseline;
}

.about-meta dt {
  margin: 0;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 500;
}

.about-meta dd {
  margin: 0;
}

.about-copy {
  margin: 0 0 1rem;
  color: var(--p-text-muted-color, #6b7280);
  line-height: 1.5;
}

.about-card + .about-card {
  margin-top: 1rem;
}

.license-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.75rem;
}

.license-list li {
  display: grid;
  gap: 0.15rem;
}

.license-list a {
  word-break: break-all;
  font-size: 0.875rem;
}

.license-scroll {
  max-height: min(28rem, 60vh);
  overflow: auto;
  display: grid;
  gap: 1rem;
  padding-right: 0.25rem;
}

.license-entry h3 {
  margin: 0 0 0.35rem;
  font-size: 1rem;
}

.license-entry p {
  margin: 0.2rem 0;
  line-height: 1.45;
}

.license-label {
  display: inline-block;
  min-width: 3.5rem;
  margin-right: 0.5rem;
  color: var(--p-text-muted-color, #6b7280);
}
</style>
