/** Format an Ed25519 public-key hex (or pass-through) as `SHA256:…` fingerprint. */
export function formatPublicKeyFingerprint(value: string) {
  const normalized = value.trim().toLowerCase()
  if (!/^[0-9a-f]{64}$/.test(normalized)) return value
  const bytes = normalized.match(/.{2}/g)?.map((part) => parseInt(part, 16)) ?? []
  return `SHA256:${btoa(String.fromCharCode(...bytes)).replace(/=+$/, '')}`
}

export function validFingerprintInput(value: string) {
  return /^(?:[0-9a-f]{64}|SHA256:[A-Za-z0-9+/]+={0,2})$/i.test(value.trim())
}

export function formatDateTime(value?: number) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const formatted = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Taipei',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23'
  }).format(date)
  return formatted + '+08:00'
}

export function formatLogTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const formatted = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Taipei',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23'
  }).format(date)
  return `${formatted}+08:00`
}

export function formatUpdatePackageSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Match client/core/delivery.py display_duration_ms (CJK 1.5 units, ASCII 1.0). */
export function displayDurationMs(text: string, ratio: number, maximumMs = 120_000): number {
  if (ratio <= 0) return 0
  let units = 0
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0
    const isCjk =
      (code >= 0x3400 && code <= 0x4dbf) ||
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0xf900 && code <= 0xfaff)
    units += isCjk ? 1.5 : 1.0
  }
  return Math.min(maximumMs, Math.max(1000, Math.trunc(units * ratio * 1000)))
}

export function formatDisplayDurationMs(ms: number): string {
  if (ms <= 0) return '需手动关闭'
  if (ms < 1000) return `${ms} 毫秒`
  const totalSeconds = ms / 1000
  if (totalSeconds < 60) {
    const rounded = Math.round(totalSeconds * 10) / 10
    return Number.isInteger(rounded) ? `${rounded} 秒` : `${rounded.toFixed(1)} 秒`
  }
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = Math.round(totalSeconds - minutes * 60)
  if (seconds <= 0) return `${minutes} 分`
  return `${minutes} 分 ${seconds} 秒`
}

export function messagePreview(value: string) {
  const characters = Array.from(value)
  return characters.length > 72 ? `${characters.slice(0, 72).join('')}…` : value
}

export function logDateParam(value: Date | null) {
  if (!value) return ''
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}
