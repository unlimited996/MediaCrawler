import { createContext, useContext, useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, Loader2, ThumbsUp, ThumbsDown, Minus } from 'lucide-react'
import type { ComponentType, CSSProperties } from 'react'
import { storyApi } from '@/lib/api'
import type { PlatformTheme } from './platformTheme'
import { useImageViewer } from './ImageViewer'

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
 * 头像永远裂。服务端 /library/api/assets/avatar-proxy 带头部 cookie 转发（实测 200），
 * 所以这里统一改写；已经是本地地址的（/api/... 或 data:）不重复包。
 */
export function toAvatarSrc(avatar?: string | null): string {
  if (!avatar) return ''
  if (/^https?:\/\//i.test(avatar)) {
    return `/library/api/assets/avatar-proxy?url=${encodeURIComponent(avatar)}`
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
  // 头像加载失败（CDN 403 / 链接失效）时切换到字母头像，而不是留一块空白
  const [broken, setBroken] = useState(false)

  // 有真实头像 URL 就直接渲染图片（走本地代理绕防盗链，加载失败再回落字母头像）
  if (src && !broken && /^(https?:|\/|data:)/i.test(src)) {
    return (
      <img
        src={src}
        alt=""
        loading="lazy"
        className={`flex-shrink-0 bg-cyber-bg-tertiary object-cover ${
          square ? 'rounded-md' : 'rounded-full'
        } ${className}`}
        style={{ width: size, height: size }}
        onError={() => setBroken(true)}
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

export interface MediaItem {
  /** 首选地址（= candidates[0]），保持旧调用方兼容 */
  url: string
  local?: boolean
  source?: string
  /** 候选源，按优先级去重试：本地 → 平台远程 → S3 备份 */
  candidates: string[]
}

/** 单条媒体的候选源（去重去空，保持优先级顺序） */
function mediaCandidates(m: any): string[] {
  // 本地 → S3 备份 → 免签名镜像 → 原外链（xhscdn 签名过期即 403）
  const raw = [m?.local_url, m?.backup_url, m?.mirror_url, m?.url]
  const out: string[] = []
  for (const u of raw) {
    if (typeof u === 'string' && u && !out.includes(u)) out.push(u)
  }
  return out
}

/** 媒体列表（过滤掉空项）。
 *
 * 访问链（P4，与后端约定）：本地缓存 → 平台远程 → S3 备份。
 * 小红书等 CDN 的链接带时间戳签名，过期即 403（实测老笔记无一例外），
 * 所以 candidates 给了完整候选链，由 MediaImage 逐个重试，避免留一张破图。
 */
export function pickMedia(d: any): MediaItem[] {
  return ((d?.media || []) as any[])
    .filter((m) => m && (typeof m.local_url === 'string' ? m.local_url
                        : (typeof m.url === 'string' && m.url) || m.backup_url))
    .map((m) => {
      const candidates = mediaCandidates(m)
      return {
        url: candidates[0] || '',
        candidates,
        local: !!m.local_url,
        // 实际取图来源：本地 / 平台外链 / S3 备份
        source: (m.local_url ? 'local' : (m.url ? 'remote' : 's3')) as string,
      }
    })
}

/**
 * 封面媒体：列表卡片 / 竖版封面用。
 * 候选源顺序同样遵循 local → 远程 → S3，另外把首图的候选链接在后面兜底。
 */
export function pickCoverMedia(d: any, media?: MediaItem[]): MediaItem | null {
  const candidates = mediaCandidates({
    local_url: d?.cover_local_url,
    url: d?.cover_url,
    backup_url: d?.cover_backup_url,
  })
  for (const c of media?.[0]?.candidates || []) {
    if (!candidates.includes(c)) candidates.push(c)
  }
  if (candidates.length === 0) return null
  return { url: candidates[0], candidates, local: !!d?.cover_local_url, source: 'cover' }
}

/**
 * 图片按候选源依次重试：本地副本 → 平台远程外链 → S3 备份。
 *
 * 背景：小红书图床是带时间签名的临时 CDN，浏览器直连老链接必 403；
 * 之前前端只认单一 url，失败后就留一张破图（「图片显示不出来」的老问题）。
 * 这里加载失败就换下一个候选源，全部失败才回调 onFailed 交给上层退化展示。
 */
export function MediaImage({
  media, className = '', alt = '', style, onFailed, zoomable = true, gallery, galleryIndex,
}: {
  media: MediaItem | null | undefined
  className?: string
  alt?: string
  style?: CSSProperties
  /** 所有候选源都失败时回调一次（上层可切文字封面/占位） */
  onFailed?: () => void
  /** 点击图片打开看图弹窗（可缩放/翻页）；false 表示不可点（如列表卡片封面，需整卡跳详情） */
  zoomable?: boolean
  /** 一组图的候选源列表；传入后弹窗内可左右翻页，缺省仅当前这一张 */
  gallery?: string[][]
  /** 在 gallery 中的起始下标 */
  galleryIndex?: number
}) {
  const candidates = media?.candidates || []
  const [idx, setIdx] = useState(0)
  const key = candidates[0] || ''
  const viewer = useImageViewer()

  // 换数据源（重开详情/刷新列表）时从第一个候选重新开始
  useEffect(() => { setIdx(0) }, [key])

  if (candidates.length === 0) return null
  const current = candidates[Math.min(idx, candidates.length - 1)]

  const handleClick = () => {
    if (!zoomable || !viewer) return
    viewer.open(gallery && gallery.length ? gallery : [candidates], galleryIndex ?? 0)
  }

  return (
    <img
      src={current}
      alt={alt}
      loading="lazy"
      // xhscdn 免签名镜像有防盗链：带本站 Referer 会 403，必须不发 Referer
      referrerPolicy="no-referrer"
      onClick={handleClick}
      className={`${className} ${zoomable ? 'cursor-zoom-in' : ''}`}
      style={style}
      onError={() => {
        setIdx((i) => (i + 1 < candidates.length ? i + 1 : i))
        if (idx + 1 >= candidates.length) onFailed?.()
      }}
    />
  )
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
import { RichText, renderEmoji, useEmojiMap } from './EmojiText'
export { RichText, renderEmoji, useEmojiMap }

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
// 表态编辑上下文
//
// 详情页统一提供「改判这条评论的态度」回调，11 个平台布局组件不必层层传 props。
// ---------------------------------------------------------------------------

export interface StanceEditState {
  /** 打开改判选择器；传入评论对象 */
  open: (comment: any) => void
}

export const StanceEditContext = createContext<StanceEditState | null>(null)

/** 布局组件内取改判入口；没有提供时为 undefined（徽标不可点） */
export function useStanceEdit(): StanceEditState | null {
  return useContext(StanceEditContext)
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
 * 评论态度徽标（库表驱动）。
 *
 * 优先展示命中的态度标签（阴阳 / 无语 / 质疑 / 嘲讽 / 观望 / 神评 …），
  标签名与颜色都来自标签库；评论还没按标签重判过时回落三态徽标。
 * 判不出来（stance 与标签都为空）时不渲染，避免出现误导性的空标签。
 * `onEdit` 传入后徽标可点击，弹出标签选择器改判（人工修正自动判定）。
 * `dark` 用于抖音这类深色舞台，换成半透明白底以免徽标和深色背景糊在一起。
 */
export function StanceBadge({
  stance, t, size = 'sm', dark = false, className = '',
  tagCode, tagName, tagColor, onEdit, comment,
}: {
  stance?: StanceValue
  t: (key: string, opts?: any) => string
  size?: 'xs' | 'sm'
  dark?: boolean
  className?: string
  /** 命中的态度标签 code（库表驱动） */
  tagCode?: string | null
  /** 标签名（后端已带，减少前端查表） */
  tagName?: string | null
  /** 标签颜色 */
  tagColor?: string | null
  /** 传入则可点击编辑表态 */
  onEdit?: () => void
  /** 评论对象：配合上下文自动获得改判入口 */
  comment?: any
}) {
  const edit = useStanceEdit()
  const editHandler = onEdit ?? (edit && comment ? () => edit.open(comment) : undefined)
  const key = String(stance || '').toLowerCase()
  const meta = STANCE_META[key]

  // 有标签 → 用标签名 + 标签色
  if (tagCode) {
    const label = tagName || tagCode
    const color = tagColor || '#94a3b8'
    const tone = dark
      ? 'border-white/20 bg-white/10'
      : ''
    return (
      <span
        role={editHandler ? 'button' : undefined}
        onClick={editHandler ? (e) => { e.stopPropagation(); editHandler() } : undefined}
        className={`inline-flex max-w-[7.5rem] items-center gap-0.5 rounded border px-1 ${
          size === 'xs' ? 'py-0 text-[10px]' : 'py-[1px] text-[11px]'
        } font-mono leading-4 ${tone} ${className} ${
          editHandler ? 'cursor-pointer hover:brightness-125' : ''
        }`}
        style={dark ? undefined : {
          borderColor: `${color}55`,
          background: `${color}1a`,
          color,
        }}
        title={`${label}${editHandler ? '（点击改判）' : ''}`}
      >
        <span className="truncate">{label}</span>
        {editHandler && <ChevronDown className="h-2.5 w-2.5 flex-shrink-0 opacity-70" />}
      </span>
    )
  }

  if (!meta) return null
  const Icon = meta.icon
  const tone = dark && key !== 'neutral'
    ? 'border-white/20 bg-white/10 text-white/80'
    : meta.cls
  return (
    <span
      role={editHandler ? 'button' : undefined}
      onClick={editHandler ? (e) => { e.stopPropagation(); editHandler() } : undefined}
      className={`inline-flex items-center gap-0.5 rounded border px-1 ${
        size === 'xs' ? 'py-0 text-[10px]' : 'py-[1px] text-[11px]'
      } font-mono leading-4 ${tone} ${className} ${
        editHandler ? 'cursor-pointer hover:brightness-125' : ''
      }`}
      title={`${t(`manage.cmt.${meta.key}`, { defaultValue: key })}${
        editHandler ? '（点击改判）' : ''}`}
    >
      <Icon className={size === 'xs' ? 'h-2.5 w-2.5' : 'h-3 w-3'} />
      {t(`manage.cmt.${meta.key}`, { defaultValue: key })}
      {editHandler && <ChevronDown className="h-2.5 w-2.5 flex-shrink-0 opacity-70" />}
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
                {/* 说话人 + 对话目标。顺序与原站一致：「<作者> 回复 @<被回复者>：内容」，
                    作者名必须在前。子评论之间也会互相回复，reply_to 由后端按 parent_id
                    映射得出，准确指向被回复的那条评论作者，而非一律指向主评论作者。 */}
                <span className={mutedClassName}>
                  {/* 微博等平台历史上用 replyToPrefix 表「回复 @」；已有精确
                      reply_to 时不再叠加，避免「回复 @ 甲 回复 @乙」重复 */}
                  {!r.reply_to && replyToPrefix ? `${replyToPrefix} ` : ''}{r.author || '-'}
                </span>
                {r.reply_to ? (
                  <>
                    <span className={mutedClassName}> 回复 </span>
                    <span className="font-normal" style={{ color: accent }}>@{r.reply_to}</span>
                  </>
                ) : null}
                <span className={mutedClassName}>：</span>
                {/* 表情：子评论正文与主评论一致走 RichText，把小红书表情标记
                    （[捂脸R] / [doge] 等）渲染成 CDN 表情图片；此前用裸 <span>
                    会把标记当成原始方括号文本显示，导致「用户发的表情」不显示。 */}
                <RichText content={r.content} className={textClassName} />
                <CommentImages pictures={r.pictures} />
                {/* 每条子评论都像原站一样展示 IP 属地 + 时间 */}
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-cyber-text-muted">
                  {r.ip_location ? <span>IP属地：{r.ip_location}</span> : null}
                  <span>{fmtTs(r.publish_ts)}</span>
                </div>
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

/**
 * 评论配图（表情包 / 图片评论）。
 *
 * 后端已把每条评论的配图解析成 [{url, local_url, backup_url, source}]，
 * url 按「本地 → S3 → 平台远程」兜底。这里只负责网格渲染：单图大一点、
 * 多图走三列，点击新标签打开原图。此前前端从未渲染评论 pictures，导致表情包看不到。
 */
export function CommentImages({
  pictures, className = '',
}: {
  pictures?: any[] | null
  className?: string
}) {
  if (!pictures || pictures.length === 0) return null
  const single = pictures.length === 1
  return (
    <div
      className={`mt-1.5 grid gap-1.5 ${single ? 'max-w-[220px] grid-cols-1' : 'max-w-[340px] grid-cols-3'} ${className}`}
    >
      {pictures.map((p: any, i: number) => (
        <MediaImage
          key={i}
          media={{
            url: p.url,
            candidates: p.candidates?.length ? p.candidates : mediaCandidates(p),
          }}
          className="w-full cursor-zoom-in rounded-lg border border-cyber-border-subtle object-cover"
          style={{ maxHeight: single ? 220 : 120 }}
        />
      ))}
    </div>
  )
}
