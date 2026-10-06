export type Message = {
  message_id: string
  queue_seq: number
  priority: number
  content: string
  target_client_ids: string[]
  status: 'pending' | 'sent' | 'expired' | 'withdrawn'
  created_at: number
  expires_at: number
  display_position?: string | null
  display_duration_ratio?: number | null
  tts_enabled?: boolean
  deliveries?: Record<string, 'pending' | 'sent' | 'received' | 'displayed' | 'spoken' | 'failed' | 'withdrawn'>
  delivery_history?: Record<string, 'received' | 'displayed' | 'spoken' | 'failed'>
}

export type Device = {
  client_id: string
  label: string
  client_version?: string
  status?: 'pending' | 'approved' | 'revoked'
  online?: boolean
  approved_at: number
  last_seen?: number
  connection_mode?: 'pull' | 'listen' | string
  requested_mode?: 'auto' | 'pull' | 'listen' | string
  forced_mode?: 'auto' | 'pull' | 'listen' | string
  listener_port?: number
  probe_sent?: number
  probe_received?: number
  probe_loss_percent?: number
  probe_min_latency_ms?: number
  probe_max_latency_ms?: number
  probe_avg_latency_ms?: number
  /** Offline cause from server: user_exit | update | admin | unexpected | reconnecting */
  session_end_reason?: string
  session_end_label?: string
  session_end_at?: number
  session_end_detail?: string
}

export type ConfigForm = {
  config_id: string
  issued_at: number
  heartbeat_interval_seconds: number
  heartbeat_timeout_seconds: number
  message_ttl_hours: number
  ack_timeout_seconds: number
  max_speech_depth: number
  max_repeat_expansion: number
  default_display_position: string
  display_duration_ratio: number
  listener_probe_interval_seconds: number
  listener_probe_duration_hours: number
  listener_probe_reset_days: number
  listener_loss_threshold: number
  listener_idle_timeout_seconds: number
  immediate: boolean
}

export type ServerLogEntry = {
  time?: string
  rawTime?: string
  level?: string
  msg?: string
  fields?: string
}

export type UpdateRecord = {
  update_id: string
  seq?: number
  component?: string
  components?: string[]
  version?: string
  platform?: string
  client_version?: string
  updater_version?: string
  sha256: string
  status: string
  status_detail?: string
  force?: boolean
  created_at: number
}

/** Connection / session-end filter: online + every offline classification. */
export type DeviceConnectionFilter =
  | ''
  | 'online'
  | 'offline'
  | 'user_exit'
  | 'unexpected'
  | 'admin'
  | 'update'
  | 'reconnecting'
  | 'unknown_offline'

/** Secondary sort within auth groups; pending is always pinned above approved/revoked. */
export type DeviceSortKey =
  | 'approved_at_desc'
  | 'approved_at_asc'
  | 'last_seen_desc'
  | 'last_seen_asc'
  | 'label_asc'
  | 'label_desc'
  | 'online_first'
  | 'offline_first'

export type SectionKey = 'messages' | 'devices' | 'updates' | 'settings' | 'server'

export type SelectOption<T extends string = string> = { label: string; value: T }
