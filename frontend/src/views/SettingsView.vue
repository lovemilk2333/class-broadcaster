<script setup lang="ts">
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import { Form } from '@primevue/forms'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import SafeInputNumber from '../components/SafeInputNumber.vue'
import { useAdminStore } from '../stores/admin'
import { displayPositionOptions } from '../utils/labels'

const admin = useAdminStore()
</script>

<template>
  <section class="content settings-content">
    <div class="section-head">
      <div>
        <h2>服务端配置</h2>
        <p>配置 ID：{{ admin.configForm.config_id || '未加载' }}</p>
      </div>
      <Button label="保存配置" icon="pi pi-save" :loading="admin.configSaving || admin.configLoading" @click="admin.saveConfig" />
    </div>
    <Skeleton v-if="admin.configLoading" height="8rem" class="api-skeleton" />
    <Form class="settings-grid" :initialValues="admin.configForm" @submit="admin.saveConfig">
      <Card class="setting-group">
        <template #title>连接</template>
        <template #content>
          <label>心跳间隔（秒）
            <SafeInputNumber name="heartbeat_interval_seconds" v-model="admin.configForm.heartbeat_interval_seconds" :min="1" :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>心跳超时（秒）
            <SafeInputNumber name="heartbeat_timeout_seconds" v-model="admin.configForm.heartbeat_timeout_seconds" :min="1" :max="7200" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>回执超时（秒）
            <SafeInputNumber name="ack_timeout_seconds" v-model="admin.configForm.ack_timeout_seconds" :min="1" :max="604800" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
            <small>客户端在此时间内没有完成回执时，服务端允许重新投递消息。</small>
          </label>
        </template>
      </Card>
      <Card class="setting-group">
        <template #title>消息与语音</template>
        <template #content>
          <label>消息保留（小时）
            <SafeInputNumber name="message_ttl_hours" v-model="admin.configForm.message_ttl_hours" :min="1" :max="168" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>语音节点最大递归解析深度
            <SafeInputNumber name="max_speech_depth" v-model="admin.configForm.max_speech_depth" :min="1" :max="64" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>语音节点最大重复展开上限
            <SafeInputNumber name="max_repeat_expansion" v-model="admin.configForm.max_repeat_expansion" :min="1" :max="10000" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>默认消息显示位置
            <Select name="default_display_position" v-model="admin.configForm.default_display_position" :options="displayPositionOptions" optionLabel="label" optionValue="value" />
          </label>
          <label>默认显示比率
            <SafeInputNumber name="default_display_duration_ratio" v-model="admin.configForm.display_duration_ratio" :min="0" :max="120" :step="0.1" mode="decimal" :minFractionDigits="0" :maxFractionDigits="2" :useGrouping="false" :allowEmpty="false" :emptyValue="0" />
            <small>按字符权重计算显示时长；设为 0 时消息不会自动关闭。</small>
          </label>
        </template>
      </Card>
      <Card class="setting-group">
        <template #title>监听模式探测</template>
        <template #content>
          <label>探测间隔（秒）
            <SafeInputNumber name="listener_probe_interval_seconds" v-model="admin.configForm.listener_probe_interval_seconds" :min="1" :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>试用时长（小时）
            <SafeInputNumber name="listener_probe_duration_hours" v-model="admin.configForm.listener_probe_duration_hours" :min="1" :max="168" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>失败重测间隔（天）
            <SafeInputNumber name="listener_probe_reset_days" v-model="admin.configForm.listener_probe_reset_days" :min="1" :max="365" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <label>最大丢包率（%）
            <SafeInputNumber name="listener_loss_threshold" v-model="admin.configForm.listener_loss_threshold" :min="0" :max="100" :useGrouping="false" :allowEmpty="false" :emptyValue="0" />
          </label>
          <label>监听空闲断开（秒）
            <SafeInputNumber name="listener_idle_timeout_seconds" v-model="admin.configForm.listener_idle_timeout_seconds" :min="1" :max="3600" :useGrouping="false" :allowEmpty="false" :emptyValue="1" />
          </label>
          <small>自动模式在试用窗口内按间隔探测；丢包率不超过阈值才切换监听模式。</small>
        </template>
      </Card>
      <Card class="setting-group">
        <template #title>发布方式</template>
        <template #content>
          <label class="checkbox-label">
            <Checkbox name="immediate" v-model="admin.configForm.immediate" binary />立即向现有连接发送配置变更
          </label>
          <small>未启用时，新配置在后续服务端发包时生效。</small>
        </template>
      </Card>
    </Form>
  </section>
</template>
