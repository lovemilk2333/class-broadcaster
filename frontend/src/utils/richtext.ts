/** Whitelist HTML helpers for message compose / preview (mirrors client/core/richtext.py). */

const ALLOWED_TAGS = new Set(['B', 'I', 'U', 'SPAN', 'BR', 'IMG', 'DIV', 'P'])
const SPAN_STYLE_PROPS = new Set(['font-family', 'font-size', 'color', 'font-weight', 'font-style'])
const DATA_IMAGE_RE = /^data:image\/[a-z0-9.+-]+;base64,/i
const FONT_WEIGHT_RE = /^(?:normal|bold|bolder|lighter|[1-9]00)$/i
const FONT_STYLE_RE = /^(?:normal|italic|oblique)$/i

function sanitizeSpanStyle(style: string): string {
  const kept: string[] = []
  for (const part of String(style || '').split(';')) {
    const colon = part.indexOf(':')
    if (colon <= 0) continue
    const name = part.slice(0, colon).trim().toLowerCase()
    let value = part.slice(colon + 1).trim()
    if (!name || !value) continue
    if (!SPAN_STYLE_PROPS.has(name)) continue
    const lowered = value.toLowerCase()
    if (lowered.includes('url(') || lowered.includes('expression(') || lowered.includes('javascript:')) continue
    // Browsers often emit <span style="font-weight: bold"> from execCommand('bold')
    // instead of <b>; keep those so the compose surface preview stays accurate.
    if (name === 'font-weight' && !FONT_WEIGHT_RE.test(value)) continue
    if (name === 'font-style' && !FONT_STYLE_RE.test(value)) continue
    kept.push(`${name}: ${value}`)
  }
  return kept.join('; ')
}

function imgSrcAllowed(src: string): boolean {
  const raw = String(src || '').trim()
  return DATA_IMAGE_RE.test(raw) && raw.includes(',')
}

/**
 * Sanitize compose HTML down to the client whitelist.
 * Contenteditable may emit `<div>` / `<p>` — those become `<br>` separators + inner HTML.
 */
export function sanitizeMessageHtml(input: string): string {
  const raw = String(input || '')
  if (!raw.trim()) return ''
  if (typeof DOMParser === 'undefined') return raw
  const doc = new DOMParser().parseFromString(`<div id="mkcb-root">${raw}</div>`, 'text/html')
  const root = doc.getElementById('mkcb-root')
  if (!root) return ''

  const walk = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) {
      return escapeText(node.textContent || '')
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return ''
    const el = node as HTMLElement
    const tag = el.tagName.toUpperCase()

    if (tag === 'SCRIPT' || tag === 'IFRAME' || tag === 'OBJECT' || tag === 'EMBED' || tag === 'STYLE' || tag === 'LINK' || tag === 'META') {
      return ''
    }
    if (tag === 'BR') return '<br />'
    if (tag === 'IMG') {
      const src = el.getAttribute('src') || ''
      if (!imgSrcAllowed(src)) return ''
      const alt = el.getAttribute('alt') || ''
      const width = (el.getAttribute('width') || '').trim()
      const height = (el.getAttribute('height') || '').trim()
      const parts = [`src="${escapeAttr(src)}"`, `alt="${escapeAttr(alt)}"`]
      if (/^\d{1,4}$/.test(width)) parts.push(`width="${width}"`)
      if (/^\d{1,4}$/.test(height)) parts.push(`height="${height}"`)
      return `<img ${parts.join(' ')} />`
    }
    if (tag === 'B' || tag === 'STRONG') {
      return `<b>${Array.from(el.childNodes).map(walk).join('')}</b>`
    }
    if (tag === 'I' || tag === 'EM') {
      return `<i>${Array.from(el.childNodes).map(walk).join('')}</i>`
    }
    if (tag === 'U') {
      return `<u>${Array.from(el.childNodes).map(walk).join('')}</u>`
    }
    if (tag === 'SPAN') {
      // Prefer semantic tags when the span is only a bold/italic wrapper.
      const style = sanitizeSpanStyle(el.getAttribute('style') || '')
      const inner = Array.from(el.childNodes).map(walk).join('')
      const weight = /(?:^|;\s*)font-weight:\s*(bold|bolder|[5-9]00)/i.exec(style)
      const italic = /(?:^|;\s*)font-style:\s*(italic|oblique)/i.test(style)
      const other = style
        .split(';')
        .map((p) => p.trim())
        .filter((p) => p && !/^font-weight:/i.test(p) && !/^font-style:/i.test(p))
        .join('; ')
      if (!other && (weight || italic)) {
        let wrapped = inner
        if (weight) wrapped = `<b>${wrapped}</b>`
        if (italic) wrapped = `<i>${wrapped}</i>`
        return wrapped
      }
      return style ? `<span style="${escapeAttr(style)}">${inner}</span>` : `<span>${inner}</span>`
    }
    // Block containers from contenteditable → line breaks + children.
    if (tag === 'DIV' || tag === 'P' || tag === 'LI') {
      const inner = Array.from(el.childNodes).map(walk).join('')
      return inner ? `${inner}<br />` : '<br />'
    }
    if (!ALLOWED_TAGS.has(tag) && tag !== 'STRONG' && tag !== 'EM') {
      return Array.from(el.childNodes).map(walk).join('')
    }
    return Array.from(el.childNodes).map(walk).join('')
  }

  let out = Array.from(root.childNodes).map(walk).join('')
  // Trim trailing <br /> noise from final empty block.
  out = out.replace(/(?:<br\s*\/?>\s*)+$/i, '')
  // Drop zero-width / BOM caret anchors; keep regular / NBSP tail text so the
  // editor can park the caret outside trailing <b>/<i> after toolbar format.
  out = out.replace(/[\u200b\uFEFF]/g, '')
  return out
}

export function htmlToPlainText(input: string): string {
  const raw = String(input || '')
  if (!raw.trim()) return ''
  if (typeof DOMParser === 'undefined') {
    return raw.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  }
  const doc = new DOMParser().parseFromString(`<div id="mkcb-root">${raw}</div>`, 'text/html')
  const root = doc.getElementById('mkcb-root')
  if (!root) return ''
  const parts: string[] = []
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push(node.textContent || '')
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return
    const el = node as HTMLElement
    const tag = el.tagName.toUpperCase()
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'IFRAME') return
    if (tag === 'BR') {
      parts.push('\n')
      return
    }
    if (tag === 'IMG') {
      const alt = el.getAttribute('alt') || ''
      if (alt) parts.push(alt)
      return
    }
    if (tag === 'DIV' || tag === 'P') {
      Array.from(el.childNodes).forEach(walk)
      parts.push('\n')
      return
    }
    Array.from(el.childNodes).forEach(walk)
  }
  Array.from(root.childNodes).forEach(walk)
  return parts.join('').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').replace(/[ \t]{2,}/g, ' ').trim()
}

export function looksLikeHtml(content: string): boolean {
  return /<\s*\/?\s*[a-zA-Z!][^>]*>/.test(String(content || ''))
}

/** Compress / resize an image File to a data URI suitable for message embed. */
export async function fileToMessageImageDataUri(
  file: File,
  options?: { maxEdge?: number; maxBytes?: number; mime?: string; quality?: number }
): Promise<{ dataUri: string; width: number; height: number }> {
  const maxEdge = options?.maxEdge ?? 960
  const maxBytes = options?.maxBytes ?? 350_000
  const preferMime = options?.mime ?? (file.type === 'image/png' ? 'image/png' : 'image/jpeg')
  const quality = options?.quality ?? 0.82

  const bitmap = await createImageBitmap(file)
  try {
    let { width, height } = bitmap
    const scale = Math.min(1, maxEdge / Math.max(width, height))
    width = Math.max(1, Math.round(width * scale))
    height = Math.max(1, Math.round(height * scale))

    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('无法创建画布')
    ctx.drawImage(bitmap, 0, 0, width, height)

    let mime = preferMime
    let q = quality
    let dataUri = canvas.toDataURL(mime, q)
    // Re-encode smaller if still huge (JPEG only; PNG stays lossless).
    if (mime === 'image/jpeg') {
      while (dataUri.length > maxBytes * 1.37 && q > 0.45) {
        q -= 0.08
        dataUri = canvas.toDataURL(mime, q)
      }
    } else if (dataUri.length > maxBytes * 1.37) {
      // PNG too large → fall back to JPEG.
      mime = 'image/jpeg'
      q = quality
      dataUri = canvas.toDataURL(mime, q)
      while (dataUri.length > maxBytes * 1.37 && q > 0.45) {
        q -= 0.08
        dataUri = canvas.toDataURL(mime, q)
      }
    }
    if (!dataUri.startsWith('data:image/')) throw new Error('图片编码失败')
    return { dataUri, width, height }
  } finally {
    bitmap.close()
  }
}

function escapeText(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function escapeAttr(value: string): string {
  return escapeText(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/`/g, '&#96;')
}
