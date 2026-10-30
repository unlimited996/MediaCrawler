/**
 * 访客浏览页（#/guest）
 *
 * 独立路由、可整页分享的只读视图：只有「筛选 + 浏览 + 导出」能力，
 * 不含任何管理操作（删除/关注/编辑/备份入口都不出现）。
 * 详情页以只读模式复用 PlatformStoryDetail。
 */
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle2, FileDown, Heart, Loader2, LogIn, MessageSquare, Search,
} from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ThemeToggle } from '@/components/layout/ThemeToggle'
import { LanguageSwitch } from '@/components/layout/LanguageSwitch'
import { storyApi, StoryRow } from '@/lib/api'
import type { RouteState } from '@/lib/useHashRoute'
import { usePlatforms } from '@/hooks/useCrawler'
import { getPlatformTheme } from '@/components/data/platform/platformTheme'
import { PlatformAvatar, pickCoverMedia, MediaImage } from '@/components/data/platform/shared'
import { PlatformStoryDetail } from '@/components/data/platform/PlatformDetail'

const PAGE_SIZE = 24
const EXPORT_LIMIT = 200 // 后端一次最多导出 200 条

type TimeRange = 'all' | 'today' | 'week' | 'month'

export function GuestView({
  onEnterFull,
  storyId,
  navigate,
}: {
  onEnterFull?: () => void
  /** 当前打开的帖子详情 ID（由 hash 路由驱动，刷新可恢复） */
  storyId?: number | null
  /** 路由跳转：打开/关闭详情时写入地址栏 */
  navigate?: (next: RouteState) => void
}) {
  const { t } = useTranslation('data')
  const [platform, setPlatform] = useState('')
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [sort, setSort] = useState('publish')
  const [range, setRange] = useState<TimeRange>('all')
  const [page, setPage] = useState(1)
  // 详情由 hash 路由驱动：刷新 #/guest/story/{id} 可直接回到该帖子
  const detailId = storyId ?? null
  const openStory = (id: number) => navigate?.({ view: 'guest', platform, storyId: id })
  const closeStory = () => navigate?.({ view: 'guest', platform })

  const platformsQ = usePlatforms()

  const tsRange = useMemo(() => {
    const now = Date.now()
    if (range === 'today') return { start_ts: now - 86400_000 }
    if (range === 'week') return { start_ts: now - 7 * 86400_000 }
    if (range === 'month') return { start_ts: now - 30 * 86400_000 }
    return {}
  }, [range])

  const listQ = useQuery({
    queryKey: ['guestStories', platform, keyword, sort, range, page],
    queryFn: async () => (await storyApi.list({
      platform, keyword, sort, page, page_size: PAGE_SIZE, ...tsRange,
    })).data,
  })

  const items: StoryRow[] = listQ.data?.items || []
  const total = listQ.data?.total || 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  /** 导出当前筛选结果：先按同一筛选取回全部 id，再走后端 PDF 导出 */
  const [exporting, setExporting] = useState(false)
  const handleExport = async () => {
    setExporting(true)
    try {
      const { data } = await storyApi.list({
        platform, keyword, sort, page: 1, page_size: EXPORT_LIMIT, ...tsRange,
      })
      const ids = (data.items || []).map((i) => i.id)
      if (!ids.length) {
        toast.info('当前筛选没有可导出的内容')
        return
      }
      const capped = ids.length > EXPORT_LIMIT
      const res = await storyApi.exportSelected(ids.slice(0, EXPORT_LIMIT), true)
      toast.success(
        `已生成 PDF（${res.data.record_count} 条）` +
        (capped ? `，超出 ${EXPORT_LIMIT} 条上限已截断，可缩小筛选范围后分批导出` : '')
      )
      window.open(res.data.download_url, '_blank')
    } catch (e: any) {
      toast.error(`导出失败：${e?.response?.data?.detail || e?.message || ''}`)
    } finally {
      setExporting(false)
    }
  }

  if (detailId !== null) {
    return (
      <div className="flex h-full flex-col gap-3 overflow-hidden">
        <PlatformStoryDetail
          storyId={detailId}
          onBack={closeStory}
          readonly
        />
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-hidden">
      {/* 顶部：访客模式说明 + 进入完整版 */}
      <div className="flex flex-shrink-0 flex-wrap items-center gap-2 rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel px-3 py-2">
        <span className="font-mono text-xs font-bold text-cyber-text-primary">
          {t('guest.title', { defaultValue: '访客浏览' })}
        </span>
        <span className="text-[11px] text-cyber-text-muted">
          {t('guest.hint', { defaultValue: '只读视图：支持筛选、浏览与导出，不含管理操作' })}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <LanguageSwitch />
          <Button
            size="sm"
            variant="outline"
            className="h-8 font-mono text-xs"
            onClick={handleExport}
            disabled={exporting || total === 0}
            title={t('guest.exportHint', { defaultValue: '按当前筛选导出 PDF（一次最多 200 条）' })}
          >
            {exporting ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <FileDown className="mr-1 h-3.5 w-3.5" />
            )}
            {t('guest.export', { defaultValue: '导出当前结果' })}
          </Button>
          {onEnterFull && (
            <Button size="sm" variant="ghost" className="h-8 font-mono text-xs"
                    onClick={onEnterFull}
                    title={t('guest.enterFull', { defaultValue: '进入完整版' })}>
              <LogIn className="mr-1 h-3.5 w-3.5" />
              {t('guest.enterFull', { defaultValue: '进入完整版' })}
            </Button>
          )}
        </div>
      </div>

      {/* 筛选栏：平台 / 关键词 / 排序 / 时间范围 */}
      <div id="guest-filters" className="flex flex-shrink-0 flex-wrap items-center gap-2">
        <div className="flex flex-wrap items-center gap-1">
          <FilterChip active={platform === ''} onClick={() => { setPlatform(''); setPage(1) }}>
            {t('manage.allPlatforms', { defaultValue: '全部' })}
          </FilterChip>
          {(platformsQ.data || []).map((p: any) => {
            // 后端下发英文平台名，优先用平台主题里的中文名（未知平台才退回）
            const theme = getPlatformTheme(p.value)
            const label = theme.label === p.value ? p.label : theme.label
            return (
              <FilterChip
                key={p.value}
                active={platform === p.value}
                theme={theme}
                onClick={() => { setPlatform(p.value); setPage(1) }}
              >
                {label}
              </FilterChip>
            )
          })}
        </div>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-cyber-text-muted" />
          <Input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { setKeyword(searchInput.trim()); setPage(1) }
            }}
            placeholder={t('manage.searchPh', { defaultValue: '搜索标题 / 正文 / 作者' })}
            className="h-8 w-56 pl-7 font-mono text-xs"
          />
        </div>
        <select
          value={sort}
          onChange={(e) => { setSort(e.target.value); setPage(1) }}
          className="h-8 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-2 font-mono text-[11px] text-cyber-text-primary"
        >
          <option value="publish">{t('manage.sortPublish', { defaultValue: '按发布时间' })}</option>
          <option value="heat">{t('manage.sortHeat', { defaultValue: '按热度' })}</option>
        </select>
        <select
          value={range}
          onChange={(e) => { setRange(e.target.value as TimeRange); setPage(1) }}
          className="h-8 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-2 font-mono text-[11px] text-cyber-text-primary"
        >
          <option value="all">{t('manage.rangeAll', { defaultValue: '全部时间' })}</option>
          <option value="today">{t('manage.rangeToday', { defaultValue: '今天' })}</option>
          <option value="week">{t('manage.rangeWeek', { defaultValue: '近 7 天' })}</option>
          <option value="month">{t('manage.rangeMonth', { defaultValue: '近 30 天' })}</option>
        </select>
        <span className="ml-auto font-mono text-[11px] text-cyber-text-muted">
          {t('manage.total', { defaultValue: '共' })} {total} {t('manage.items', { defaultValue: '条' })}
        </span>
      </div>

      {/* 列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {listQ.isLoading ? (
          <div className="py-16 text-center font-mono text-cyber-text-muted animate-pulse">
            {t('manage.loading', { defaultValue: '加载中…' })}
          </div>
        ) : items.length === 0 ? (
          <div className="py-16 text-center font-mono text-cyber-text-muted">
            {t('manage.empty', { defaultValue: '没有符合条件的内容' })}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 pb-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {items.map((it) => (
              <GuestStoryCard key={it.id} it={it} onOpen={() => openStory(it.id)} />
            ))}
          </div>
        )}
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex flex-shrink-0 items-center justify-end gap-2 font-mono text-[11px] text-cyber-text-muted">
          <Button size="sm" variant="outline" className="h-7 text-xs"
                  disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            {t('manage.prev', { defaultValue: '上一页' })}
          </Button>
          <span>{page} / {totalPages}</span>
          <Button size="sm" variant="outline" className="h-7 text-xs"
                  disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            {t('manage.next', { defaultValue: '下一页' })}
          </Button>
        </div>
      )}
    </div>
  )
}

/** 筛选胶囊（平台 / 全部） */
function FilterChip({ active, theme, onClick, children }: {
  active: boolean
  theme?: ReturnType<typeof getPlatformTheme>
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-2.5 py-1 font-mono text-[11px] transition-all ${
        active
          ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
          : 'border-cyber-border-subtle text-cyber-text-secondary hover:border-cyber-neon-cyan/40 hover:text-cyber-text-primary'
      }`}
      style={active && theme ? { color: theme.accent, borderColor: theme.accent } : undefined}
    >
      {children}
    </button>
  )
}

/**
 * 访客卡片：封面 3:4 + 标题两行 + 作者/互动数据。
 * 与管理页卡片一致，但不带勾选、删除等任何交互。
 */
function GuestStoryCard({ it, onOpen }: { it: StoryRow; onOpen: () => void }) {
  const { t } = useTranslation('data')
  const theme = getPlatformTheme(it.platform)
  const [broken, setBroken] = useState(false)
  // 封面候选源：本地副本 → 平台远程 → S3 备份，过期/防盗链时自动切下一个
  const cover = pickCoverMedia(it)
  const excerpt = (it.excerpt || it.summary || '').slice(0, 50)
  const showTextCover = !cover || broken

  return (
    <div
      onClick={onOpen}
      className="group flex cursor-pointer flex-col overflow-hidden rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel transition-all hover:border-cyber-neon-cyan/40 hover:shadow-md"
    >
      <div className="relative w-full flex-shrink-0 overflow-hidden bg-cyber-bg-tertiary aspect-[3/4]">
        {!showTextCover ? (
          <MediaImage
            media={cover}
            zoomable={false}
            className="h-full w-full object-cover"
            onFailed={() => setBroken(true)}
          />
        ) : excerpt ? (
          <div className="flex h-full w-full items-start overflow-hidden p-2.5 text-[11px] leading-5 text-cyber-text-secondary"
               style={{ background: theme.accentSoft }}>
            <span className="line-clamp-6">{excerpt}</span>
          </div>
        ) : (
          <div className="flex h-full w-full items-center justify-center text-2xl font-bold"
               style={{ color: theme.accent, background: theme.accentSoft }}>
            {(it.title || '?').trim().charAt(0) || '?'}
          </div>
        )}
        {/* 数据备份标识（信息展示，不可操作） */}
        {it.backup_complete && (
          <div
            className="absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-black/55 text-emerald-300 backdrop-blur-sm"
            title={`${t('guest.backupDone', { defaultValue: '图片/视频已全部备份至 S3' })}（${it.backup_uploaded}/${it.backup_total}）`}
          >
            <CheckCircle2 className="h-3.5 w-3.5" />
          </div>
        )}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1 p-2">
        <h3 className="line-clamp-2 text-xs font-normal leading-5 text-cyber-text-primary" title={it.title}>
          {it.title}
        </h3>
        <div className="mt-auto flex items-center gap-1.5 pt-0.5">
          <PlatformAvatar name={it.author || '-'} avatar={it.author_avatar} theme={theme} size={18} />
          <span className="min-w-0 flex-1 truncate text-[11px] text-cyber-text-secondary" title={it.author || '-'}>
            {it.author || '-'}
          </span>
          <span className="flex flex-shrink-0 items-center gap-0.5 text-[11px] text-cyber-text-muted">
            <Heart className="h-3 w-3" />
            {fmtNum(it.like_count)}
          </span>
          <span className="hidden flex-shrink-0 items-center gap-0.5 text-[11px] text-cyber-text-muted sm:flex">
            <MessageSquare className="h-3 w-3" />
            {fmtNum(it.comment_count)}
          </span>
        </div>
      </div>
    </div>
  )
}

function fmtNum(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)}w`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n || 0)
}
