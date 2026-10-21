import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Search, SlidersHorizontal, MessageSquare, MessageSquareOff } from 'lucide-react'

export type CommentSort = 'like' | 'new' | 'old'
/** 立场筛选：赞同 / 中立 / 反对（相对发帖人核心观点），空串 = 不筛选 */
export type StanceFilter = '' | 'agree' | 'neutral' | 'oppose'

export interface StanceStats {
  agree: number
  neutral: number
  oppose: number
  total: number
  /** 内容赞同度档位 key，如 rating.agreeVery */
  agreement: string
}

/** 兼容旧引用：情感筛选已整体切换为立场筛选 */
export type SentimentFilter = StanceFilter
export type SentimentStats = StanceStats

export type HasRepliesFilter = 'all' | 'with' | 'without'

/** 内容赞同度档位配色：高赞同→青绿，分化→橙，高反对→粉红 */
const AGREEMENT_STYLE: Record<string, string> = {
  'rating.agreeOverwhelming': 'bg-cyber-neon-green/20 text-cyber-neon-green border-cyber-neon-green/40',
  'rating.agreeVery': 'bg-cyber-neon-green/15 text-cyber-neon-green border-cyber-neon-green/30',
  'rating.agreeMostly': 'bg-cyber-neon-cyan/15 text-cyber-neon-cyan border-cyber-neon-cyan/30',
  'rating.agreeMixed': 'bg-cyber-neon-orange/15 text-cyber-neon-orange border-cyber-neon-orange/30',
  'rating.opposeMostly': 'bg-cyber-neon-pink/15 text-cyber-neon-pink border-cyber-neon-pink/30',
  'rating.opposeVery': 'bg-cyber-neon-pink/20 text-cyber-neon-pink border-cyber-neon-pink/40',
  'rating.opposeOverwhelming': 'bg-cyber-neon-pink/25 text-cyber-neon-pink border-cyber-neon-pink/50',
}

const STANCE_OPTIONS: { value: StanceFilter; key: string; active: string }[] = [
  { value: '', key: 'cmt.filterAll', active: 'bg-cyber-neon-cyan/20 text-cyber-neon-cyan' },
  { value: 'agree', key: 'cmt.stanceAgree', active: 'bg-cyber-neon-green/20 text-cyber-neon-green' },
  { value: 'neutral', key: 'cmt.stanceNeutral', active: 'bg-cyber-border-subtle text-cyber-text-primary' },
  { value: 'oppose', key: 'cmt.stanceOppose', active: 'bg-cyber-neon-pink/20 text-cyber-neon-pink' },
]

const SORT_OPTIONS: { value: CommentSort; key: string }[] = [
  { value: 'like', key: 'cmt.sortHot' },
  { value: 'new', key: 'cmt.sortNew' },
  { value: 'old', key: 'cmt.sortOld' },
]

const REPLIES_OPTIONS: { value: HasRepliesFilter; key: string; icon: typeof MessageSquare }[] = [
  { value: 'all', key: 'cmt.repliesAll', icon: SlidersHorizontal },
  { value: 'with', key: 'cmt.repliesWith', icon: MessageSquare },
  { value: 'without', key: 'cmt.repliesWithout', icon: MessageSquareOff },
]

function fmtCount(n: number): string {
  return n.toLocaleString('en-US')
}

/** 态度标签统计项（库表驱动，后端 tag_stats 下发） */
export interface TagStat {
  code: string
  name: string
  color: string | null
  /** 归属极性：agree / neutral / oppose（负面表态都是 oppose 的子集） */
  polarity: string | null
  count: number
}

/**
 * 评论区头部：统计行 + 筛选工具（参考 Steam 商店评论区布局）。
 *
 * 统计行格式（对齐 Steam）：
 *   正在显示与上方筛选条件匹配的 N 条评论（多半赞同）
 * 立场由后端 tools/stance.py 判定：先提取发帖人核心观点，再对每条评论判
 * 赞同 / 中立 / 反对；赞同度档位沿用 Steam 中文区的七档文案。
 */
export function CommentToolbar({
  filteredTotal,
  stats,
  tagStats,
  selectedTags,
  sort,
  stance,
  hasReplies,
  keyword,
  showReplies,
  onSort,
  onStance,
  onToggleTag,
  onHasReplies,
  onKeyword,
  onToggleReplies,
}: {
  filteredTotal: number
  stats: StanceStats
  /** 态度标签分布（库表驱动） */
  tagStats?: TagStat[]
  /** 已选中的标签 code（多选） */
  selectedTags?: string[]
  sort: CommentSort
  stance: StanceFilter
  hasReplies: HasRepliesFilter
  keyword: string
  /** 子评论全局展开开关（默认关闭，只显示条数） */
  showReplies: boolean
  onSort: (v: CommentSort) => void
  onStance: (v: StanceFilter) => void
  /** 当前立场下的下钻：传 code 选中该表态，传空串回到该立场全部 */
  onToggleTag?: (code: string) => void
  onHasReplies: (v: HasRepliesFilter) => void
  onKeyword: (v: string) => void
  onToggleReplies: () => void
}) {
  const { t } = useTranslation('data')

  /** 当前立场下的细分表态：未选立场时给出全部标签（按极性归类需要分组，
   *  这里直接全列，数量本来就不多） */
  const subTags = useMemo(() => {
    const all = tagStats || []
    // 未选立场：只列有数据的表态，避免一行塞满 10 个空标签
    if (!stance) return all.filter((g) => (g.count || 0) > 0)
    // 已选立场：列该极性下的全部表态（含 0 条，便于知道有哪些细分维度）
    return all.filter((g) => (g.polarity || 'neutral') === stance)
  }, [tagStats, stance])

  const ratingKey = stats.agreement || ''
  const ratingText = ratingKey ? t(`manage.cmt.rating.${ratingKey.split('.').pop()}`) : ''
  // 赞同度分母 = 已表态评论（赞同 + 反对），中立不参与
  const decided = stats.agree + stats.oppose
  const pct = decided > 0 ? Math.round((stats.agree / decided) * 100) : 0

  return (
    <div className="mb-3 space-y-2">
      {/* 统计行：匹配数量 + 内容赞同度 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-mono text-cyber-text-secondary">
          {t('manage.cmt.showingMatching', {
            n: fmtCount(filteredTotal),
          })}
        </span>
        {ratingText && (
          <span
            className={`rounded border px-2 py-0.5 text-[11px] font-mono font-bold ${
              AGREEMENT_STYLE[ratingKey] || 'border-cyber-border-subtle text-cyber-text-secondary'
            }`}
            title={t('manage.cmt.agreeHint', {
              agree: stats.agree,
              total: stats.total,
            })}
          >
            {ratingText}
          </span>
        )}
        {stats.total > 0 && (
          <span className="text-[10px] font-mono text-cyber-text-muted">
            {t('manage.cmt.stanceBreakdown', {
              agree: stats.agree,
              neutral: stats.neutral,
              oppose: stats.oppose,
            })}
          </span>
        )}
        {decided > 0 && (
          <span className="hidden text-[10px] font-mono text-cyber-text-muted sm:inline">
            {t('manage.cmt.agreeRatio', { pct, decided })}
          </span>
        )}
      </div>

      {/* 筛选工具行 */}
      <div className="flex flex-wrap items-center gap-2">
        {/* 排序 */}
        <div className="flex items-center gap-1">
          <span className="text-[10px] font-mono text-cyber-text-muted">{t('manage.cmt.sort')}</span>
          <select
            value={sort}
            onChange={(e) => onSort(e.target.value as CommentSort)}
            className="h-7 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 text-[11px] font-mono text-cyber-text-primary hover:border-cyber-border-DEFAULT"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {t(o.key, { defaultValue: o.value })}
              </option>
            ))}
          </select>
        </div>

        {/* 立场分段筛选（赞同 / 中立 / 反对）+ 该立场下的细分表态（下钻） */}
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[10px] font-mono text-cyber-text-muted">{t('manage.cmt.stance')}</span>
          <div className="flex overflow-hidden rounded border border-cyber-border-subtle">
            {STANCE_OPTIONS.map((o) => (
              <button
                key={o.value || 'all'}
                type="button"
                onClick={() => onStance(o.value)}
                className={`px-2 py-1 text-[11px] font-mono transition-all ${
                  stance === o.value
                    ? o.active
                    : 'text-cyber-text-muted hover:text-cyber-text-primary'
                }`}
              >
                {t(`manage.cmt.${o.key}`, { defaultValue: o.value || 'all' })}
              </button>
            ))}
          </div>
          {/* 细分表态：选中某立场后只列该极性下的标签（阴阳/质疑/嘲讽…都是反对的子集） */}
          {subTags.length > 0 && (
            <div className="flex flex-wrap items-center gap-1">
              <span className="text-[10px] font-mono text-cyber-text-muted">›</span>
              <button
                type="button"
                onClick={() => onToggleTag?.('')}
                className={`rounded border px-1.5 py-[3px] text-[11px] font-mono transition-all ${
                  !(selectedTags || []).length
                    ? 'border-cyber-border-DEFAULT bg-cyber-bg-tertiary text-cyber-text-primary'
                    : 'border-cyber-border-subtle text-cyber-text-muted hover:text-cyber-text-primary'
                }`}
                title={t('manage.cmt.tagAllHint', { defaultValue: '该立场下全部表态' })}
              >
                {t('manage.cmt.filterAll', { defaultValue: '全部' })}
              </button>
              {subTags.map((g) => {
                const on = (selectedTags || []).includes(g.code)
                const color = g.color || '#94a3b8'
                return (
                  <button
                    key={g.code}
                    type="button"
                    onClick={() => onToggleTag?.(g.code)}
                    title={`${g.name} · ${g.count} 条`}
                    className={`flex items-center gap-1 rounded border px-1.5 py-[3px] text-[11px] font-mono transition-all ${
                      on ? '' : 'opacity-75 hover:opacity-100'
                    }`}
                    style={{
                      borderColor: on ? color : `${color}44`,
                      background: on ? `${color}26` : 'transparent',
                      color: on ? color : undefined,
                    }}
                  >
                    <span>{g.name}</span>
                    <span className="text-[10px] opacity-70">{g.count}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {/* 是否含回复 */}
        <div className="flex items-center gap-1">
          <span className="text-[10px] font-mono text-cyber-text-muted">{t('manage.cmt.replies', { defaultValue: '回复' })}</span>
          <div className="flex overflow-hidden rounded border border-cyber-border-subtle">
            {REPLIES_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => onHasReplies(o.value)}
                title={t(`manage.cmt.${o.key}`, { defaultValue: '' })}
                className={`px-1.5 py-1 transition-all ${
                  hasReplies === o.value
                    ? 'bg-cyber-neon-cyan/20 text-cyber-neon-cyan'
                    : 'text-cyber-text-muted hover:text-cyber-text-primary'
                }`}
              >
                <o.icon className="h-3 w-3" />
              </button>
            ))}
          </div>
        </div>

        {/* 子评论展开开关：默认收起（只显示条数），打开后各条评论的回复全部展开 */}
        <div className="flex items-center gap-1">
          <span className="text-[10px] font-mono text-cyber-text-muted">
            {t('manage.cmt.subReplies', { defaultValue: '子评论' })}
          </span>
          <button
            type="button"
            onClick={onToggleReplies}
            className={`flex h-7 items-center gap-1 rounded border px-1.5 text-[11px] font-mono transition-all ${
              showReplies
                ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
                : 'border-cyber-border-subtle text-cyber-text-muted hover:text-cyber-text-primary'
            }`}
            title={t('manage.repliesToggleHint', {
              defaultValue: '子评论默认收起，打开后全部展开',
            })}
          >
            <MessageSquare className="h-3 w-3" />
            {showReplies
              ? t('manage.cmt.subRepliesOn', { defaultValue: '展开' })
              : t('manage.cmt.subRepliesOff', { defaultValue: '收起' })}
          </button>
        </div>

        {/* 关键词搜索（客户端过滤） */}
        <div className="relative ml-auto">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-cyber-text-muted" />
          <input
            value={keyword}
            onChange={(e) => onKeyword(e.target.value)}
            placeholder={t('manage.cmt.searchPlaceholder', { defaultValue: '搜索评论内容' })}
            className="h-7 w-44 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary pl-7 pr-2 text-[11px] font-mono text-cyber-text-primary placeholder:text-cyber-text-muted hover:border-cyber-border-DEFAULT focus-visible:border-cyber-neon-cyan/50"
          />
        </div>
      </div>
    </div>
  )
}
