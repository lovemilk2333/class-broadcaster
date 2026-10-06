import type { Device, DeviceSortKey } from '../types'
import { deviceConnectionLabel } from './labels'
import { formatPublicKeyFingerprint } from './format'

function deviceActivityTs(device: Device): number {
  if (device.last_seen && device.last_seen > 0) return device.last_seen
  return device.approved_at || 0
}

/** pending first (authorize), then approved, then revoked — always, so new clients are not buried. */
function deviceAuthRank(device: Device): number {
  const status = device.status || 'approved'
  if (status === 'pending') return 0
  if (status === 'approved') return 1
  return 2
}

export function compareDevices(a: Device, b: Device, key: DeviceSortKey): number {
  const byId = () => String(a.client_id || '').localeCompare(String(b.client_id || ''))
  const byAuth = deviceAuthRank(a) - deviceAuthRank(b)
  if (byAuth !== 0) return byAuth
  switch (key) {
    case 'approved_at_asc': {
      const d = (a.approved_at || 0) - (b.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'approved_at_desc': {
      // Pending have approved_at=0; within approved group newest first.
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'last_seen_asc': {
      const d = deviceActivityTs(a) - deviceActivityTs(b)
      return d !== 0 ? d : byId()
    }
    case 'last_seen_desc': {
      const d = deviceActivityTs(b) - deviceActivityTs(a)
      return d !== 0 ? d : byId()
    }
    case 'label_asc': {
      const la = (a.label || '').trim().toLowerCase()
      const lb = (b.label || '').trim().toLowerCase()
      const d = la.localeCompare(lb, 'zh-CN')
      return d !== 0 ? d : byId()
    }
    case 'label_desc': {
      const la = (a.label || '').trim().toLowerCase()
      const lb = (b.label || '').trim().toLowerCase()
      const d = lb.localeCompare(la, 'zh-CN')
      return d !== 0 ? d : byId()
    }
    case 'online_first': {
      const oa = a.online ? 1 : 0
      const ob = b.online ? 1 : 0
      if (oa !== ob) return ob - oa
      // Keep secondary order stable (approved_at desc).
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    case 'offline_first': {
      const oa = a.online ? 1 : 0
      const ob = b.online ? 1 : 0
      if (oa !== ob) return oa - ob
      const d = (b.approved_at || 0) - (a.approved_at || 0)
      return d !== 0 ? d : byId()
    }
    default:
      return byId()
  }
}

export function sortDevicesStable(items: Device[], key: DeviceSortKey = 'approved_at_desc'): Device[] {
  // Pending always first for quick approve; then the chosen secondary key.
  // Heartbeats must not reshuffle: secondary keys avoid last_seen by default.
  return [...items].sort((a, b) => compareDevices(a, b, key))
}

export function deviceSearchHaystack(device: Device): string {
  const status = device.status || 'approved'
  const fingerprint = formatPublicKeyFingerprint(device.client_id).toLowerCase()
  const connectionLabel = deviceConnectionLabel(device)
  return [
    device.label,
    device.client_id,
    fingerprint,
    device.client_version,
    status,
    device.connection_mode,
    device.requested_mode,
    device.forced_mode,
    device.online ? 'online connected 已连接' : 'offline 未连接',
    connectionLabel,
    device.session_end_reason,
    device.session_end_detail,
    device.session_end_label
  ].map((part) => String(part || '').toLowerCase()).join(' ')
}
