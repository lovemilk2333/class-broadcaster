import type { Message } from '../types'

/** Higher rank = more advanced client-side receipt (history must not hide a later displayed/spoken). */
const deliveryStatusRank: Record<string, number> = {
  pending: 0,
  sent: 1,
  expired: 1,
  withdrawn: 0,
  received: 2,
  failed: 3,
  displayed: 4,
  spoken: 5
}

export function effectiveDeliveryStatus(
  current?: string | null,
  history?: string | null
): string {
  // After withdraw/expire the live row is terminal; prefer the last real receipt from history.
  if (current === 'withdrawn' || current === 'expired') {
    if (history && deliveryStatusRank[history] >= deliveryStatusRank.received) return history
    return current
  }
  const currentRank = deliveryStatusRank[current ?? ''] ?? -1
  const historyRank = deliveryStatusRank[history ?? ''] ?? -1
  if (historyRank > currentRank) return history as string
  if (current) return current
  return history || 'pending'
}

export function deliveryCounts(message: Message) {
  const clients = new Set<string>([
    ...Object.keys(message.deliveries ?? {}),
    ...Object.keys(message.delivery_history ?? {})
  ])
  clients.delete('*')
  const statuses = [...clients].map((client) =>
    effectiveDeliveryStatus(message.deliveries?.[client], message.delivery_history?.[client])
  )
  const targeted = (message.target_client_ids ?? []).filter((id) => id && id !== '*')
  const total = Math.max(statuses.length, targeted.length)
  return {
    total,
    completed: statuses.filter((status) => status === 'displayed' || status === 'spoken').length,
    received: statuses.filter((status) =>
      status === 'received' || status === 'displayed' || status === 'spoken' || status === 'failed'
    ).length,
    failed: statuses.filter((status) => status === 'failed').length,
    pending: statuses.filter((status) => status === 'pending' || status === 'sent').length
  }
}

export function deliveryCountsTitle(message: Message) {
  const counts = deliveryCounts(message)
  const parts = [
    `已显示完成 ${counts.completed}`,
    `已接收 ${counts.received}`,
    `目标/记录 ${counts.total}`
  ]
  if (counts.failed > 0) parts.push(`失败 ${counts.failed}`)
  if (counts.pending > 0) parts.push(`待完成 ${counts.pending}`)
  return parts.join(' · ')
}
