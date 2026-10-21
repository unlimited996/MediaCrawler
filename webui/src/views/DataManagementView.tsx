import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  Database, Search, FileDown, Loader2,
  Heart, MessageSquare, Tag as TagIcon, Eye, Trash2,
} from 'lucide-react'
import { storyApi } from '@/lib/api'
import { usePlatforms } from '@/hooks/useCrawler'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { toast } from 'sonner'
import { PlatformStoryDetail } from '@/components/data/platform/PlatformDetail'
import { getPlatformTheme } from '@/components/data/platform/platformTheme'
import { splitTopics } from '@/components/data/platform/shared'

type TimeRange = 'all' | 'today' | 'week' | 'month'

const DAY_MS = 86_400_000

function fmtTs(ts?: number | null): string {
  if (!ts) return '-'
  const v = ts > 10_000_000_000 ? ts : ts * 1000
  const d = new Date(v)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function DataManagementView() {
  const { t } = useTranslation('data')
  const queryClient = useQueryClient()
  const [platform, setPlatform] = useState<string>('')
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [tag, setTag] = useState('')
  const [range, setRange] = useState<TimeRange>('all')
  const [sort, setSort] = useState('heat')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [detailId, setDetailId] = useState<number | null>(null)

  const pageSize = 20

  // 平台列表复用抓取页的同一个 query（key 与返回结构必须一致，
  // 否则两个视图切换时会互相覆盖缓存导致渲染崩溃）
  const { data: platforms } = usePlatforms()
  const tagsQ = useQuery({
    queryKey: ['storyTags'],
    queryFn: async () => (await storyApi.tags(30)).data.items,
  })

  const tsRange = useMemo(() => {
    const now = Date.now()
    if (range === 'today') return { start_ts: now - DAY_MS }
    if (range === 'week') return { start_ts: now - 7 * DAY_MS }
    if (range === 'month') return { start_ts: now - 30 * DAY_MS }
    return {}
  }, [range])

  const listQ = useQuery({
    queryKey: ['stories', platform, keyword, tag, range, sort, page],
    queryFn: async () => (await storyApi.list({
      platform: platform || undefined,
      keyword: keyword || undefined,
      tag: tag || undefined,
      sort, page, page_size: pageSize,
      ...tsRange,
    })).data,
  })

  const items = listQ.data?.items || []
  const total = listQ.data?.total || 0
  const allChecked = items.length > 0 && items.every((i) => selected.has(i.id))

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const exportM = useMutation({
    mutationFn: () => storyApi.exportSelected([...selected], true),
    onSuccess: (res) => {
      toast.success(`${t('manage.exportOk')}（${res.data.record_count} ${t('manage.items')}）`)
      window.open(res.data.download_url, '_blank')
    },
    onError: (e: any) => toast.error(`${t('manage.exportFail')}: ${e?.message || ''}`),
  })

  const deleteM = useMutation({
    mutationFn: () => storyApi.removeMany([...selected]),
    onSuccess: (res) => {
      const n = res.data.deleted_count ?? 0
      const c = res.data.cascade ?? {}
      const parts = [
        c.chapters ? `${c.chapters} 章节` : '',
        c.comments ? `${c.comments} 评论` : '',
        c.media ? `${c.media} 附件` : '',
        c.files ? `${c.files} 个媒体文件` : '',
        c.legacy_rows ? `${c.legacy_rows} 行原始记录` : '',
      ].filter(Boolean)
      toast.success(
        `${t('manage.deleteOk', { count: n })}${parts.length ? `（${parts.join(' / ')}）` : ''}`
      )
      const failed = res.data.failed ?? []
      if (failed.length) {
        toast.error(`${failed.length} 条删除失败：${failed[0]?.error ?? ''}`)
      }
      setSelected(new Set())
      queryClient.invalidateQueries({ queryKey: ['stories'] })
      queryClient.invalidateQueries({ queryKey: ['storyTags'] })
    },
    onError: (e: any) => toast.error(`${t('manage.deleteFail')}: ${e?.message || ''}`),
  })

  const handleBatchDelete = () => {
    const n = selected.size
    if (n === 0) return
    if (!window.confirm(t('manage.deleteConfirmBatch', { count: n }))) return
    deleteM.mutate()
  }

  // ---------- 详情页（按平台差异化渲染） ----------
  if (detailId !== null) {
    return (
      <PlatformStoryDetail
        storyId={detailId}
        onBack={() => setDetailId(null)}
      />
    )
  }

  return (
    <div className="h-full flex flex-col gap-3">
      {/* 筛选区 */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5">
          <Database className="w-4 h-4 text-cyber-neon-cyan" />
          <h2 className="text-base font-mono font-bold text-cyber-text-primary">
            {t('manage.title')}
          </h2>
        </div>

        {/* 平台 */}
        <select
          value={platform}
          onChange={(e) => { setPlatform(e.target.value); setPage(1) }}
          className="h-8 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary"
        >
          <option value="">{t('manage.allPlatforms')}</option>
          {(platforms || []).map((p: any) => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>

        {/* 时间范围 */}
        <select
          value={range}
          onChange={(e) => { setRange(e.target.value as TimeRange); setPage(1) }}
          className="h-8 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary"
        >
          <option value="all">{t('manage.timeAll')}</option>
          <option value="today">{t('manage.timeToday')}</option>
          <option value="week">{t('manage.timeWeek')}</option>
          <option value="month">{t('manage.timeMonth')}</option>
        </select>

        {/* 标签 */}
        <select
          value={tag}
          onChange={(e) => { setTag(e.target.value); setPage(1) }}
          className="h-8 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary"
        >
          <option value="">{t('manage.allTags')}</option>
          {(tagsQ.data || []).map((x) => (
            <option key={x.name} value={x.name}>{x.name}（{x.count}）</option>
          ))}
        </select>

        {/* 排序 */}
        <select
          value={sort}
          onChange={(e) => { setSort(e.target.value); setPage(1) }}
          className="h-8 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary"
        >
          <option value="heat">{t('manage.sortHeat')}</option>
          <option value="time">{t('manage.sortTime')}</option>
          <option value="like">{t('manage.sortLike')}</option>
          <option value="comment">{t('manage.sortComment')}</option>
        </select>

        {/* 关键词搜索（回车即搜） */}
        <div className="flex items-center gap-1 ml-auto">
          <input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { setKeyword(searchInput.trim()); setPage(1) }
            }}
            placeholder={t('manage.searchPlaceholder')}
            className="h-8 w-40 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary placeholder:text-cyber-text-muted"
          />
          <Button
            variant="outline" size="sm" className="h-8 font-mono"
            onClick={() => { setKeyword(searchInput.trim()); setPage(1) }}
          >
            <Search className="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>

      {/* 批量操作栏 */}
      <div className="flex items-center gap-3 flex-wrap border-y border-cyber-border-subtle py-2">
        <label className="flex items-center gap-1.5 text-xs font-mono text-cyber-text-secondary cursor-pointer">
          <Checkbox
            checked={allChecked}
            onCheckedChange={(c) => {
              if (c) setSelected(new Set(items.map((i) => i.id)))
              else setSelected(new Set())
            }}
          />
          {t('manage.selectAll')}
        </label>
        <span className="text-xs font-mono text-cyber-text-muted">
          {t('manage.selectedCount', { count: selected.size })}
          {keyword && ` · ${t('manage.keyword')}：${keyword}`}
          {platform && ` · ${t('manage.platform')}：${platform}`}
          {range !== 'all' && ` · ${range}`}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {selected.size > 0 && (
            <Button
              variant="ghost" size="sm" className="h-8 font-mono"
              onClick={() => setSelected(new Set())}
            >
              {t('manage.clear')}
            </Button>
          )}
          <Button
            variant="outline" size="sm" className="h-8 font-mono"
            disabled={selected.size === 0 || exportM.isPending}
            onClick={() => exportM.mutate()}
          >
            {exportM.isPending ? (
              <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
            ) : (
              <FileDown className="w-3.5 h-3.5 mr-1" />
            )}
            {t('manage.exportSelected')}
          </Button>
          <Button
            variant="outline" size="sm"
            className="h-8 font-mono border-cyber-neon-pink/50 text-cyber-neon-pink hover:bg-cyber-neon-pink/10 hover:text-cyber-neon-pink"
            disabled={selected.size === 0 || deleteM.isPending}
            onClick={handleBatchDelete}
          >
            {deleteM.isPending ? (
              <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
            ) : (
              <Trash2 className="w-3.5 h-3.5 mr-1" />
            )}
            {deleteM.isPending ? t('manage.deleting') : t('manage.deleteSelected')}
          </Button>
        </div>
      </div>

      {/* 列表 */}
      <div className="flex-1 overflow-y-auto min-h-0 space-y-1">
        {listQ.isLoading ? (
          <div className="text-center py-10 text-cyber-text-muted font-mono animate-pulse">
            {t('manage.loading')}
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-10 text-cyber-text-muted font-mono">
            {t('manage.empty')}
          </div>
        ) : (
          items.map((it) => (
            <div
              key={it.id}
              className="flex items-start gap-3 px-3 py-2 rounded-md hover:bg-cyber-bg-tertiary/60 transition-colors"
            >
              <Checkbox
                checked={selected.has(it.id)}
                onCheckedChange={() => toggle(it.id)}
                className="mt-1"
              />
              <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span
                  className="rounded-full px-2 py-0.5 text-[10px] font-mono font-medium"
                  style={{
                    color: getPlatformTheme(it.platform).accent,
                    background: getPlatformTheme(it.platform).accentSoft,
                  }}
                >
                  {getPlatformTheme(it.platform).label}
                </span>
                {/* 标题里的小红书话题（#论文排版[话题]#）不进标题栏 */}
                <span className="text-sm text-cyber-text-primary truncate" title={it.title}>
                  {splitTopics(it.title).clean || it.title}
                </span>
              </div>
                <div className="flex items-center gap-3 mt-1 text-[11px] font-mono text-cyber-text-muted flex-wrap">
                  <span>{fmtTs(it.publish_ts)}</span>
                  <span className="flex items-center gap-1">
                    <Heart className="w-3 h-3" />{it.like_count}
                  </span>
                  <span className="flex items-center gap-1">
                    <MessageSquare className="w-3 h-3" />{it.comment_count}
                  </span>
                  {it.tags.slice(0, 3).map((x) => (
                    <span key={x} className="flex items-center gap-0.5">
                      <TagIcon className="w-3 h-3" />{x}
                    </span>
                  ))}
                </div>
              </div>
              <Button
                variant="ghost" size="sm" className="h-7 px-2 font-mono flex-shrink-0"
                onClick={() => setDetailId(it.id)}
              >
                <Eye className="w-3 h-3 mr-1" />
                {t('manage.detail')}
              </Button>
            </div>
          ))
        )}
      </div>

      {/* 分页 */}
      {total > pageSize && (
        <div className="flex items-center justify-center gap-2 pt-1">
          <Button
            variant="outline" size="sm" className="h-7 font-mono"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            {t('manage.prev')}
          </Button>
          <span className="text-xs font-mono text-cyber-text-muted">
            {page} / {Math.max(1, Math.ceil(total / pageSize))} · {t('manage.totalCount', { count: total })}
          </span>
          <Button
            variant="outline" size="sm" className="h-7 font-mono"
            disabled={page * pageSize >= total}
            onClick={() => setPage((p) => p + 1)}
          >
            {t('manage.next')}
          </Button>
        </div>
      )}
    </div>
  )
}
