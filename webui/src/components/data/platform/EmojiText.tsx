import { useEffect, useState, type ReactNode } from 'react'

/**
 * 小红书表情渲染。
 *
 * 平台评论里的表情是纯文本标记（如 [笑哭R]、[萌萌哒R]、[doge]），
 * 官方前端会用图片替换。映射表来自小红书 web 端 localStorage 的
 * redmoji 数据（166 条，抓取时提取），CDN（picasso-static）无防盗链。
 * 映射表放在 public/emoji/xhs.json，运行时懒加载。
 */

let emojiMap: Record<string, string> | null = null
let loadingPromise: Promise<Record<string, string>> | null = null

function loadMap(): Promise<Record<string, string>> {
  if (emojiMap) return Promise.resolve(emojiMap)
  if (loadingPromise) return loadingPromise
  loadingPromise = fetch(`${import.meta.env.BASE_URL}emoji/xhs.json`)
    .then((r) => r.json())
    .then((m) => {
      emojiMap = m as Record<string, string>
      return emojiMap
    })
    .catch(() => {
      loadingPromise = null
      return {} as Record<string, string>
    })
  return loadingPromise
}

/** 确保映射表已加载（组件内调用） */
export function useEmojiMap(): Record<string, string> | null {
  const [map, setMap] = useState<Record<string, string> | null>(emojiMap)
  useEffect(() => {
    if (emojiMap) return
    let alive = true
    loadMap().then((m) => { if (alive) setMap(m) })
    return () => { alive = false }
  }, [])
  return map
}

// [xxxR] / [xxxH] / [doge] 等：方括号包裹、2-8 个字符
const EMOJI_RE = /\[([^\[\]\s]{1,10}?)\]/g

/**
 * 把文本里的 [表情名] 替换成 <img>。未命中的方括号片段原样保留
 * （避免把普通文本如 [注] 也吃掉）。
 */
export function renderEmoji(text: string, map: Record<string, string> | null): ReactNode[] {
  if (!text) return []
  if (!map) return [text]
  const out: ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null
  EMOJI_RE.lastIndex = 0
  let key = 0
  while ((match = EMOJI_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      out.push(text.slice(lastIndex, match.index))
    }
    const token = match[0]
    const url = map[token]
    if (url) {
      out.push(
        <img
          key={`e${key++}`}
          src={url}
          alt={token}
          title={token}
          loading="lazy"
          className="inline-block h-[1.15em] w-[1.15em] align-text-bottom"
        />,
      )
    } else {
      out.push(token)
    }
    lastIndex = match.index + token.length
  }
  if (lastIndex < text.length) out.push(text.slice(lastIndex))
  return out
}

/**
 * 评论/正文内容组件：自动渲染小红书表情。
 * 用法：<RichText content={c.content} />
 */
export function RichText({ content, className }: { content?: string; className?: string }) {
  const map = useEmojiMap()
  if (!content) return null
  // break-anywhere：评论/正文里的超长链接、无空格英文长串也能断行，
  // 避免把评论卡片撑出横向溢出（外层 overflow-hidden 会把内容直接裁掉）
  return <span className={`break-anywhere ${className || ''}`}>{renderEmoji(content, map)}</span>
}
