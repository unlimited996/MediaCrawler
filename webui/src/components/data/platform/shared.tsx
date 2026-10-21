import { createContext, useContext, useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, Loader2, ThumbsUp, ThumbsDown, Minus } from 'lucide-react'
import type { ComponentType } from 'react'
import { storyApi } from '@/lib/api'
import type { PlatformTheme } from './platformTheme'

/** 统一的时间格式化（兼容秒/毫秒时间戳） */
export function fmtTs(ts?: number | null): string {
  if (!ts) return '-'
  const v = ts > 10_000_000_000 ? ts : ts * 1000
  const d = new Date(v)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 大数字格式化为「1.2万」 */
export function fmtNum(n?: number | string | null): string {
  if (n === null || n === undefined || n === '') return '0'
  const v = typeof n === 'string' ? Number(n) : n
  if (!Number.isFinite(v)) return String(n)
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(1)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)}万`
  return String(v)
}

export interface AvatarProps {
  name?: string | null
  theme: PlatformTheme
  size?: number
  /** 圆角方形头像（B站/抖音风格），默认圆形 */
  square?: boolean
  className?: string
  /** 平台返回的作者头像 URL；有则显示图片，无则退化为字母渐变头像 */
  avatar?: string | null
}

/**
 * 平台头像 URL 改写成本地代理地址。
 *
 * 头像 CDN（xhscdn 等）和图片 CDN 一样有防盗链，浏览器 <img> 直连吃 403，
 * 头像永远裂。服务端 /api/data/avatar-proxy 带头部 cookie 转发（实测 200），
 * 所以这里统一改写；已经是本地地址的（/api/... 或 data:）不重复包。
 */
export function toAvatarSrc(avatar?: string | null): string {
  if (!avatar) return ''
  if (/^https?:\/\//i.test(avatar)) {
    return `/api/data/avatar-proxy?url=${encodeURIComponent(avatar)}`
  }
  return avatar
}

/** 兜底主题：布局没传 theme 时（如直接复用 CommentReplies）避免 undefined 崩溃 */
const FALLBACK_THEME: PlatformTheme = {
  key: 'default', label: '内容', accent: '#06B6D4',
  accentSoft: 'rgba(6,182,212,0.1)', layout: 'default',
}

/** 平台色渐变头像：后端不下发头像地址时用作者名首字生成 */
export function PlatformAvatar({
  name, theme, size = 40, square = false, className = '', avatar,
}: AvatarProps) {
  const ch = (name || '?').trim().charAt(0) || '?'
  const src = toAvatarSrc(avatar)

  // 有真实头像 URL 就直接渲染图片（走本地代理绕防盗链，加载失败再回落字母头像）
  if (src && /^(https?:|\/|data:)/i.test(src)) {
    return (
      <img
        src={src}
        alt=""
        loading="lazy"
        className={`flex-shrink-0 bg-cyber-bg-tertiary object-cover ${
          square ? 'rounded-md' : 'rounded-full'
        } ${className}`}
        style={{ width: size, height: size }}
        onError={(e) => {
          // 图裂了隐藏自身，由父级背景露出（不阻塞列表渲染）
          ;(e.target as HTMLImageElement).style.visibility = 'hidden'
        }}
      />
    )
  }
  return (
    <span
      className={`inline-flex items-center justify-center flex-shrink-0 font-medium text-white select-none ${
        square ? 'rounded-md' : 'rounded-full'
      } ${className}`}
      style={{
        width: size,
        height: size,
        fontSize: Math.max(11, size * 0.42),
        background: `linear-gradient(135deg, ${theme.accent}, ${theme.accent}CC)`,
      }}
      aria-hidden
    >
      {ch}
    </span>
  )
}

/** 把 chapters 合并为正文文本；有章节标题时作为小标题拼接 */
export function buildContent(d: any): string {
  const chapters: any[] = d?.chapters || []
  if (chapters.length === 0) return d?.summary || d?.desc || ''
  return chapters
    .map((c) => (c?.title ? `${c.title}\n${c.content || ''}` : c?.content || ''))
    .filter(Boolean)
    .join('\n\n')
}

// ---------------------------------------------------------------------------
// 话题标签（小红书形如 `#论文排版[话题]#`）
//
// 平台 API 把话题直接拼在标题/正文里，导致标题栏变成一长串 `#xx[话题]#`。
// 这里把话题从文本里摘出来：标题/正文只留纯文本，话题另做彩色标签展示。
// ---------------------------------------------------------------------------

/** 带 `[话题]` 后缀的标准写法：`#论文排版[话题]#` */
const TOPIC_RE = /#([^#\]]+?)\[话题\]#/g
/** 裸话题：`#ai` / `#恋爱日常`，要求出现在行首或空白之后，避免误伤 `C#` 之类 */
const BARE_TAG_RE = /(^|[\s　])#([A-Za-z0-9_\-一-龥]{1,30})(?=\s|$)/g

export interface SplitTopicsResult {
  /** 去掉话题后的纯文本 */
  clean: string
  /** 摘出来的话题名（去重、保序） */
  topics: string[]
}

export function splitTopics(text?: string | null): SplitTopicsResult {
  if (!text) return { clean: '', topics: [] }
  const topics: string[] = []
  const seen = new Set<string>()
  const push = (name: string) => {
    const v = (name || '').trim()
    if (!v || v.length > 40 || seen.has(v)) return
    seen.add(v)
    topics.push(v)
  }

  let clean = String(text).replace(TOPIC_RE, (_m, name) => {
    push(name)
    return ' '
  })
  clean = clean.replace(BARE_TAG_RE, (_m, pre, name) => {
    push(name)
    return pre
  })

  // 摘掉话题后可能留下空洞缩进/多余空行，收拾一下
  clean = clean
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/^\s+|\s+$/g, '')
    .replace(/\n{3,}/g, '\n\n')

  return { clean, topics }
}

/**
 * 从详情对象里摘出全部话题，并返回「标题/正文已清洗」的副本。
 *
 * 覆盖 title / summary / desc / chapters[].content，再并上后端已解析的 tags。
 * 返回值的 `tags` 直接塞进 d，各布局现有的 tags 渲染块就能原样复用。
 */
export function extractTopics(d: any): { d: any; topics: string[] } {
  if (!d) return { d: {}, topics: [] }
  const topics: string[] = []
  const seen = new Set<string>()
  const collect = (list: string[]) => {
    for (const x of list || []) {
      const v = (x || '').trim()
      if (!v || seen.has(v)) continue
      seen.add(v)
      topics.push(v)
    }
  }

  const title = splitTopics(d.title)
  collect(title.topics)

  const chapters = ((d.chapters || []) as any[]).map((c: any) => {
    if (!c || typeof c.content !== 'string') return c
    const s = splitTopics(c.content)
    collect(s.topics)
    return { ...c, content: s.clean }
  })

  const summary = splitTopics(d.summary ?? d.desc)
  collect(summary.topics)

  collect((d.tags || []) as string[])

  return {
    d: { ...d, title: title.clean, summary: summary.clean, chapters, tags: topics },
    topics,
  }
}

/** 媒体列表（过滤掉空项）。
 *
 * 优先用 local_url：小红书等 CDN 有防盗链，浏览器 <img> 直连原始
 * source_url 会吃 403 裂图；本地已下载的文件走 /api/data/media/... 路由。
 * 没有本地文件时才退回原始 URL（此时大概率裂图，但总比空白好）。
 */
export function pickMedia(d: any): { url: string; local?: boolean }[] {
  return ((d?.media || []) as any[])
    .filter((m) => m && (typeof m.local_url === 'string' ? m.local_url : typeof m.url === 'string' && m.url))
    .map((m) => ({
      url: (m.local_url as string) || (m.url as string),
      local: !!m.local_url,
    }))
}

/** 首个原文外链 */
export function pickSourceUrl(d: any): string {
  const chapters: any[] = d?.chapters || []
  for (const c of chapters) {
    if (c?.source_url) return c.source_url
  }
  return d?.source_url || d?.note_url || ''
}

// 表情渲染组件从这里转发，各布局统一从 shared import
export { RichText, renderEmoji, useEmojiMap } from './EmojiText'

/**
 * 作者链接：头像 + 昵称包一层 <a>，点击跳转平台个人主页。
 * 没有 author_url 时退化为不带链接的普通渲染（不留死链）。
 */
export function AuthorLink({
  url, name, avatar, theme, size = 28, square = false, className = '',
}: {
  url?: string | null
  name?: string | null
  avatar?: string | null
  theme: PlatformTheme
  size?: number
  square?: boolean
  /** 附加样式（子评论头像等需要额外对齐时用） */
  className?: string
}) {
  const avatarEl = (
    <PlatformAvatar name={name} avatar={avatar} theme={theme} size={size} square={square} />
  )
  if (!url || !/^https?:\/\//.test(url)) {
    return <span className={`flex-shrink-0 ${className}`}>{avatarEl}</span>
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer"
       className={`flex-shrink-0 ${className}`} title={`访问 ${name || '用户'} 的主页`}>
      {avatarEl}
    </a>
  )
}

/**
 * 昵称文本链接：有主页 URL 包一层 <a>（新标签打开），没有则原样返回文本。
 * 用法：{authorAnchor(c.author_url, c.author || '-')}
 */
export function authorAnchor(url?: string | null, name?: string | null) {
  if (!url || !/^https?:\/\//.test(url)) return name || '-'
  return (
    <a href={url} target="_blank" rel="noopener noreferrer"
       className="hover:underline hover:text-cyber-neon-cyan transition-colors"
       title={`访问 ${name || '用户'} 的主页`}>
      {name || '-'}
    </a>
  )
}

/**
 * 正文底部的话题标签。
 * 用平台品牌色做底 + 描边，和标题/正文的纯文本区分开。
 */
export function TopicChips({
  topics, theme, className = '',
}: {
  topics?: string[]
  theme?: PlatformTheme
  className?: string
}) {
  const list = (topics || []).filter(Boolean)
  if (list.length === 0) return null
  const accent = theme?.accent || '#22d3ee'
  const soft = theme?.accentSoft || 'rgba(34,211,238,0.12)'
  return (
    <div className={`mt-3 flex flex-wrap gap-1.5 ${className}`}>
      {list.map((x) => (
        <span
          key={x}
          className="inline-flex items-center rounded-full border px-2 py-[1px] text-[11px] leading-5"
          style={{ color: accent, background: soft, borderColor: `${accent}44` }}
        >
          #{x}
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 立场徽标（内容赞同度）
//
// 后端 tools/stance.py 先提取发帖人核心观点，再判定每条评论相对该观点的立场：
// 赞同 agree / 中立 neutral / 反对 oppose。这里只做展示，判定逻辑不放在前端。
// ---------------------------------------------------------------------------

export type StanceValue = 'agree' | 'neutral' | 'oppose' | '' | null | undefined

interface StanceMeta {
  /** i18n key 后缀：manage.cmt.stanceAgree 等 */
  key: string
  cls: string
  icon: ComponentType<{ className?: string }>
}

export const STANCE_META: Record<string, StanceMeta> = {
  agree: {
    key: 'stanceAgree',
    cls: 'border-cyber-neon-green/30 bg-cyber-neon-green/10 text-cyber-neon-green',
    icon: ThumbsUp,
  },
  neutral: {
    key: 'stanceNeutral',
    cls: 'border-cyber-border-subtle bg-cyber-bg-tertiary/60 text-cyber-text-muted',
    icon: Minus,
  },
  oppose: {
    key: 'stanceOppose',
    cls: 'border-cyber-neon-pink/30 bg-cyber-neon-pink/10 text-cyber-neon-pink',
    icon: ThumbsDown,
  },
}

/**
 * 评论立场徽标：赞同 / 中立 / 反对。
 *
 * 判不出来（stance 为空）时不渲染，避免出现误导性的空标签。
 * `dark` 用于抖音这类深色舞台，换成半透明白底以免徽标和深色背景糊在一起。
 */
export function StanceBadge({
  stance, t, size = 'sm', dark = false, className = '',
}: {
  stance?: StanceValue
  t: (key: string, opts?: any) => string
  size?: 'xs' | 'sm'
  dark?: boolean
  className?: string
}) {
  const key = String(stance || '').toLowerCase()
  const meta = STANCE_META[key]
  if (!meta) return null
  const Icon = meta.icon
  const tone = dark && key !== 'neutral'
    ? 'border-white/20 bg-white/10 text-white/80'
    : meta.cls
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded border px-1 ${
        size === 'xs' ? 'py-0 text-[10px]' : 'py-[1px] text-[11px]'
      } font-mono leading-4 ${tone} ${className}`}
      title={t('manage.cmt.stanceHint', { defaultValue: '相对发帖人观点的立场（自动判定）' })}
    >
      <Icon className={size === 'xs' ? 'h-2.5 w-2.5' : 'h-3 w-3'} />
      {t(`manage.cmt.${meta.key}`, { defaultValue: key })}
    </span>
  )
}

/**
 * 子评论的视图状态：详情页顶部的开关 + storyId（按需拉全量回复要用）。
 * 用 context 传，省得给 8 个布局组件都加一层 props。
 */
export interface RepliesViewState {
  /** 全局开关：打开后每条评论的回复都展开 */
  showReplies: boolean
  storyId: number
}
export const RepliesContext = createContext<RepliesViewState>({ showReplies: false, storyId: 0 })

/** 单次拉取回复的分页大小（后端上限 2000） */
const REPLY_FETCH_SIZE = 500
/** 一条评论最多拉多少页回复，避免异常数据把页面拖死 */
const REPLY_MAX_PAGES = 6

/**
 * 楼中楼回复：默认收起只显示条数，点击展开全部。
 *
 * 一条主评论动辄上千条回复，全量下发 + 全量渲染会把详情页拖死，
 * 所以：列表接口只给前 20 条并附总数，展开时才按需拉全量。
 *
 * 子评论不参与「内容赞同度」：赞同度统计的口径是主评论（depth=0）对发帖人
 * 观点的立场，楼中楼多数是在跟其他评论者对话，判定基准都不一样，
 * 硬凑进分母只会稀释指标，因此这里不展示立场徽标。
 */
export function CommentReplies({
  replies, replyCount, commentId, theme, t,
  containerClassName = 'mt-2 space-y-1.5 rounded-lg bg-cyber-bg-tertiary/50 p-2.5',
  mutedClassName = 'text-cyber-text-muted',
  textClassName = 'text-cyber-text-secondary',
  replyToPrefix,
}: {
  /** 列表接口附带的前 N 条回复 */
  replies?: any[]
  /** 回复总数（后端 reply_count；缺省时按 replies 长度算） */
  replyCount?: number
  /** 主评论 id，用于展开时拉全量 */
  commentId?: number
  theme?: PlatformTheme
  t: (key: string, opts?: any) => string
  /** 回复容器样式（各平台配色不同，由布局传入） */
  containerClassName?: string
  /** 昵称样式 */
  mutedClassName?: string
  /** 回复正文样式 */
  textClassName?: string
  /** 微博等平台用「回复 @xxx：」，小红书直接「xxx：」 */
  replyToPrefix?: string
}) {
  const { showReplies, storyId } = useContext(RepliesContext)
  const [override, setOverride] = useState<boolean | null>(null)
  const [full, setFull] = useState<any[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)

  const partial = replies || []
  const total = replyCount ?? partial.length
  const list = full || partial
  const open = override ?? showReplies
  const needMore = open && full === null && total > partial.length

  // 展开且本地只有部分回复 → 按需拉全量
  useEffect(() => {
    if (!needMore || !storyId || !commentId || loading) return
    let alive = true
    setLoading(true)
    setFailed(false)
    ;(async () => {
      try {
        const acc: any[] = []
        let p = 1
        while (alive && p <= REPLY_MAX_PAGES) {
          const res = await storyApi.commentReplies(storyId, commentId, p, REPLY_FETCH_SIZE)
          const items = res.data?.items || []
          acc.push(...items)
          if (items.length === 0 || acc.length >= (res.data?.total ?? 0)) break
          p += 1
        }
        if (alive) setFull(acc)
      } catch {
        if (alive) setFailed(true)
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => { alive = false }
  }, [needMore, storyId, commentId]) // eslint-disable-line react-hooks/exhaustive-deps

  if (total === 0) return null
  const accent = theme?.accent || '#22d3ee'

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOverride(!open)}
        className="inline-flex items-center gap-1 text-[11px] font-mono transition-opacity hover:opacity-80"
        style={{ color: accent }}
      >
        {loading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : open ? (
          <ChevronUp className="h-3 w-3" />
        ) : (
          <ChevronDown className="h-3 w-3" />
        )}
        {open
          ? t('manage.collapseReplies', { defaultValue: '收起回复' })
          : t('manage.expandReplies', { count: total, defaultValue: `展开 ${total} 条回复` })}
      </button>
      {open && (
        <div className={containerClassName}>
          {list.map((r: any, i: number) => (
            <div key={r.id ?? `${r.author}-${i}`} className="flex items-start gap-1.5 text-xs leading-5">
              {/* 子评论头像：与主评论一致，走 AuthorLink 包一层主页链接，
                  点击跳转该用户主页；头像走本地代理绕 CDN 防盗链，裂图回落字母头像 */}
              <AuthorLink
                url={r.author_url}
                name={r.author || r.author_name || '-'}
                avatar={r.author_avatar || r.avatar}
                theme={theme || FALLBACK_THEME}
                size={18}
                className="mt-[3px]"
              />
              <div className="min-w-0 flex-1">
                <span className={mutedClassName}>
                  {replyToPrefix ? `${replyToPrefix} ` : ''}{r.author || '-'}：
                </span>
                <span className={textClassName}>{r.content}</span>
              </div>
            </div>
          ))}
          {failed && (
            <button
              type="button"
              onClick={() => { setFull(null); setFailed(false) }}
              className="text-[11px] font-mono text-cyber-neon-pink hover:underline"
            >
              {t('manage.loadRepliesFail', { defaultValue: '回复加载失败，点击重试' })}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
