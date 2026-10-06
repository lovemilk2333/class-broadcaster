<script setup lang="ts">
import { onMounted, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import DatePicker from 'primevue/datepicker'
import { Form } from '@primevue/forms'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import TablePagination from '../components/TablePagination.vue'
import { useAdminStore } from '../stores/admin'
import { useSettingsStore } from '../stores/settings'
import { logLevels, logSeverity, pageSizeOptions } from '../utils/labels'

const admin = useAdminStore()
const settings = useSettingsStore()
const confirm = useConfirm()

function applyLogFilters() {
  admin.applyLogFilters()
  void admin.loadLogViewPage(1, true)
}

function resetLogFilters() {
  admin.resetLogFilters()
  void admin.loadLogViewPage(1, true)
}

function purgeExpiredHistory() {
  confirm.require({
    message: '将立即删除创建时间已满 185 天的消息记录，以及超过 185 天未活跃的客户端记录。此操作不可撤销，是否继续？',
    header: '清理过期数据',
    icon: 'pi pi-exclamation-triangle',
    acceptLabel: '立即清理',
    rejectLabel: '取消',
    acceptClass: 'p-button-danger',
    accept: () => void admin.purgeExpiredHistory()
  })
}

watch(() => settings.pageSize, () => {
  admin.serverLogViewPage = 1
  admin.serverLogJumpPage = 1
})

onMounted(() => {
  admin.logsTableReady = false
  window.requestAnimationFrame(() => {
    admin.logsTableReady = true
    void admin.loadLogViewPage(1, true)
  })
})
</script>

<template>
  <section class="content logs-content">
    <div class="section-head">
      <div>
        <h2>服务端</h2>
        <p>维护过期数据，并查看服务端运行日志。</p>
      </div>
    </div>

    <Card class="maintenance-card server-identity-card">
      <template #title>服务端版本</template>
      <template #content>
        <dl class="server-version-meta">
          <div><dt>版本</dt><dd>{{ admin.serverVersion || '未知' }}</dd></div>
          <div><dt>构建时间</dt><dd>{{ admin.serverBuildDate || '未知' }}</dd></div>
        </dl>
      </template>
    </Card>

    <Card class="maintenance-card">
      <template #title>数据维护</template>
      <template #content>
        <p class="maintenance-copy">自动任务会周期性清理超过 185 天的消息与客户端记录。也可在此立即执行一次清理。</p>
        <ul class="maintenance-list">
          <li>消息：删除创建时间 ≥ 185 天的全部消息记录（含 pending / sent）</li>
          <li>客户端：删除超过 185 天未活跃的设备记录</li>
        </ul>
        <div class="maintenance-actions">
          <Button
            label="清理 ≥ 185 天的数据"
            icon="pi pi-trash"
            severity="danger"
            outlined
            :loading="admin.purgeExpiredLoading"
            @click="purgeExpiredHistory"
          />
        </div>
      </template>
    </Card>

    <div class="section-head log-section-head">
      <div>
        <h2>服务端日志</h2>
        <p>日志内容由服务端统一使用英文记录。</p>
      </div>
      <Button label="刷新" icon="pi pi-refresh" :loading="admin.logsLoading" @click="admin.loadLogViewPage(admin.serverLogViewPage, true)" />
    </div>
    <Form class="log-filters" :initialValues="{ start: null, end: null, minimumLevel: '', selectedLevels: [] }" @submit="applyLogFilters">
      <label>开始日期<DatePicker name="start" v-model="admin.logStartInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
      <label>结束日期<DatePicker name="end" v-model="admin.logEndInput" dateFormat="yy-mm-dd" showIcon showButtonBar /></label>
      <label>最低级别<Select name="minimumLevel" v-model="admin.logMinimumLevelInput" :options="logLevels" placeholder="全部级别" showClear /></label>
      <label>指定级别<MultiSelect name="selectedLevels" v-model="admin.logSelectedLevelsInput" :options="logLevels" placeholder="全部级别" display="chip" /></label>
      <div class="log-filter-actions">
        <Button type="button" label="重置" text @click="resetLogFilters" />
        <Button type="submit" label="应用筛选" icon="pi pi-filter" />
      </div>
      <small class="log-filter-hint">日期、最低级别、指定级别条件同时满足；指定的多个级别任一匹配即可。</small>
    </Form>
    <TablePagination
      :page="admin.serverLogViewPage"
      :total="admin.serverLogTotal"
      :pageSize="settings.pageSize"
      :pageSizeOptions="pageSizeOptions"
      :loading="admin.logsLoading"
      @update:page="admin.loadLogViewPage"
      @update:pageSize="admin.changeLogPageSize"
    />
    <Skeleton v-if="!admin.logsTableReady" height="24rem" class="api-skeleton" />
    <DataTable v-else :value="admin.visibleServerLogEntries" stripedRows responsiveLayout="scroll" class="queue-table" :loading="admin.logsLoading">
      <template #empty>暂无日志</template>
      <Column field="time" header="时间" style="width: 13rem" />
      <Column field="level" header="级别" style="width: 7rem"><template #body="slotProps">
        <Tag :value="slotProps.data.level || 'UNKNOWN'" :severity="logSeverity(slotProps.data.level)" />
      </template></Column>
      <Column field="msg" header="消息" />
      <Column field="fields" header="字段" />
    </DataTable>
  </section>
</template>
