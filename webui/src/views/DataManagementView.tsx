import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  Search, FileDown, Loader2, Users, X, List, LayoutGrid,
  Heart, MessageSquare, Tag as TagIcon, Eye, Trash2,
} from 'lucide-react'
import { storyApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { toast } from 'sonner'
import { PlatformStoryDetail } from '@/components/data/platform/PlatformDetail'
import { getPlatformTheme } from '@/components/data/platform/platformTheme'
import { splitTopics, PlatformAvatar, fmtNum } from '@/components/data/platform/shared'
import { PlatformBrandMark, PlatformPageHeader } from '@/components/data/platform/PlatformBrand'

type TimeRange = 'all' | 'today' | 'week' | 'month'
/** 列表 / 网格视图；选择写 localStorage，刷新后保留 */
type ViewMode = 'list' | 'grid'
const VIEW_MODE_KEY = 'mc_data_view_mode'

function loadViewMode(): ViewMode {
  try {
    return (localStorage.getItem(VIEW_MODE_KEY) as ViewMode) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
}


const DAY_MS = 86_400_000

function fmtTs(ts?: number | null): string {
  if (!ts) return '-'
  const v = ts > 10_000_000_000 ? ts : ts * 1000
  const d = new Date(v)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 发帖人昵称：列表模式固定只显示前 2 个字符，超出用省略号，hover 显示完整昵称。
 * 定宽（w-[2.6rem]）+ 不换行，保证多行列表的昵称列对齐、不会因长度不同而错位。
 */
function AuthorShortName({ name, className = '' }: { name?: string; className?: string }) {
  const full = name || '-'
  const short = full.length > 2 ? `${full.slice(0, 2)}…` : full
  return (
    <span
      title={full}
      className={`inline-block w-[2.6rem] flex-shrink-0 truncate whitespace-nowrap text-left align-middle ${className}`}
    >
      {short}
    </span>
  )
}

/**
 * 网格卡片（小红书式）：封面固定 3:4、标题最多两行、底部作者 + 互动数据。
 * 封面裂图（CDN 防盗链）时回落到标题首字色块，不留破图。
 */
function StoryCard({
  it, checked, onCheck, onOpen,
}: {
  it: any
  checked: boolean
  onCheck: () => void
  onOpen: () => void
}) {
  const theme = getPlatformTheme(it.platform)
  const [broken, setBroken] = useState(false)
  const title = splitTopics(it.title).clean || it.title || ''
  // 封面取帖子内部首张图片：本地副本优先（外链签名会过期），无本地副本用外链
  const cover = it.cover_local_url || it.cover_url || ''
  // 无图（或图挂了）时展示正文前 50 字
  const excerpt = (it.excerpt || it.summary || '').slice(0, 50)
  const showTextCover = !cover || broken

  return (
    <div
      onClick={onOpen}
      className="group flex cursor-pointer flex-col overflow-hidden rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel transition-all hover:border-cyber-neon-cyan/40 hover:shadow-md"
    >
      {/* 封面：统一 3:4，object-cover 保证不变形 */}
      <div className="relative w-full flex-shrink-0 overflow-hidden bg-cyber-bg-tertiary aspect-[3/4]">
        {!showTextCover ? (
          <img
            src={cover}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover"
            onError={() => setBroken(true)}
          />
        ) : excerpt ? (
          // 无图 / 图挂了：显示正文前 50 字
          <div
            className="flex h-full w-full items-start overflow-hidden p-2.5 text-[11px] leading-5 text-cyber-text-secondary"
            style={{ background: theme.accentSoft }}
          >
            <span className="line-clamp-6">{excerpt}</span>
          </div>
        ) : (
          <div
            className="flex h-full w-full items-center justify-center text-2xl font-bold"
            style={{ color: theme.accent, background: theme.accentSoft }}
          >
            {(title || '?').trim().charAt(0) || '?'}
          </div>
        )}
        {/* 勾选框浮在封面左上角；阻止冒泡，避免勾选时误开详情 */}
        <div className="absolute left-1.5 top-1.5" onClick={(e) => e.stopPropagation()}>
          <Checkbox
            checked={checked}
            onCheckedChange={onCheck}
            className="h-4 w-4 border-white/70 bg-black/40 data-[state=checked]:border-cyber-neon-cyan data-[state=checked]:bg-cyber-neon-cyan"
          />
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1 p-2">
        <h3
          className="line-clamp-2 text-xs font-normal leading-5 text-cyber-text-primary"
          title={title}
        >
          {title}
        </h3>
        {/* 底部：作者头像 + 昵称 + 互动数据 */}
        <div className="mt-auto flex items-center gap-1.5 pt-0.5">
          <PlatformAvatar
            name={it.author || '-'}
            avatar={it.author_avatar}
            theme={theme}
            size={18}
          />
          <span
            className="min-w-0 flex-1 truncate text-[11px] text-cyber-text-secondary"
            title={it.author || '-'}
          >
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

/**
 * 数据管理页。
 *
 * platform 由顶部导航栏的平台入口驱动（受控）；不传时退回内部 state，
 * 保证单独复用这个视图也能自己切平台。
 */
export function DataManagementView({
  platform: platformProp,
}: {
  /** 当前平台（顶部导航的平台入口传入）；'' = 全部 */
  platform?: string
} = {}) {
  const { t } = useTranslation('data')
  const queryClient = useQueryClient()
  // 平台由顶部导航的平台入口决定（页内不再放下拉，避免重复入口）
  const platform = platformProp ?? ''
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [tag, setTag] = useState('')
  const [range, setRange] = useState<TimeRange>('all')
  // 默认按发布时间倒序（最新在前）
  const [sort, setSort] = useState('publish')
  /** 发帖人筛选：多选，空数组 = 不筛选 */
  const [authorIds, setAuthorIds] = useState<number[]>([])
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [detailId, setDetailId] = useState<number | null>(null)
  /** 列表 / 网格视图，选择持久化到 localStorage */
  const [viewMode, setViewMode] = useState<ViewMode>(loadViewMode)
  useEffect(() => {
    try { localStorage.setItem(VIEW_MODE_KEY, viewMode) } catch { /* 隐私模式忽略 */ }
  }, [viewMode])

  const pageSize = 20

  // 说明：平台下拉已移除，平台列表 query 随之不再需要；
  // 若以后要恢复页内切换，复用抓取页的 usePlatforms() 即可（key 必须一致）。
  const tagsQ = useQuery({
    queryKey: ['storyTags'],
    queryFn: async () => (await storyApi.tags(30)).data.items,
  })

  // 发帖人候选：跟着平台走，作者表几千行，靠关键词/平台收敛（下拉不做全量）
  const [authorOpen, setAuthorOpen] = useState(false)
  const [authorKw, setAuthorKw] = useState('')
  const authorsQ = useQuery({
    queryKey: ['storyAuthors', platform, authorKw],
    queryFn: async () => (await storyApi.authors({
      platform: platform || undefined,
      keyword: authorKw.trim() || undefined,
      limit: 50,
    })).data.items,
    staleTime: 30_000,
  })

  // 点击面板外部收起发帖人筛选
  const authorBoxRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!authorOpen) return
    const onDown = (e: MouseEvent) => {
      if (authorBoxRef.current && !authorBoxRef.current.contains(e.target as Node)) {
        setAuthorOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [authorOpen])

  const toggleAuthor = (id: number) => {
    setAuthorIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
    setPage(1)
  }

  // 平台切换（顶部入口或页内下拉）都要回到第一页，否则会停在空页
  useEffect(() => {
    setPage(1)
    setSelected(new Set())
    setAuthorIds([])
  }, [platform])

  const tsRange = useMemo(() => {
    const now = Date.now()
    if (range === 'today') return { start_ts: now - DAY_MS }
    if (range === 'week') return { start_ts: now - 7 * DAY_MS }
    if (range === 'month') return { start_ts: now - 30 * DAY_MS }
    return {}
  }, [range])

  // 发帖人 id 排序后入 key，避免勾选顺序不同导致缓存不命中
  const authorKey = [...authorIds].sort((a, b) => a - b).join(',')
  const listQ = useQuery({
    queryKey: ['stories', platform, keyword, tag, range, sort, page, authorKey],
    queryFn: async () => (await storyApi.list({
      platform: platform || undefined,
      keyword: keyword || undefined,
      tag: tag || undefined,
      sort, page, page_size: pageSize,
      author_ids: authorIds.length ? authorIds : undefined,
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
      {/* 平台品牌头部：标识 + 平台名 + 品牌色，一眼区分当前平台页 */}
      <PlatformPageHeader platform={platform}>
        <span className="text-[11px] font-mono text-cyber-text-muted">
          {total} {t('manage.totalUnit', { defaultValue: '条' })}
        </span>
      </PlatformPageHeader>

      {/* 筛选区 */}
      <div className="flex items-center gap-2 flex-wrap">


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

        {/* 发帖人筛选（多选）：勾选后只展示这些用户的帖子，清空即恢复全部 */}
        <div className="relative" ref={authorBoxRef}>
          <button
            type="button"
            onClick={() => setAuthorOpen((v) => !v)}
            className={`flex h-8 items-center gap-1 rounded-md border px-2 text-xs font-mono transition-all ${
              authorIds.length > 0
                ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
                : 'border-cyber-border-subtle bg-cyber-bg-tertiary text-cyber-text-primary hover:border-cyber-neon-cyan/40'
            }`}
          >
            <Users className="w-3.5 h-3.5" />
            {t('manage.authorFilter', { defaultValue: '发帖人' })}
            {authorIds.length > 0 && (
              <span className="rounded-full bg-cyber-neon-cyan/20 px-1.5 text-[10px]">
                {authorIds.length}
              </span>
            )}
          </button>

          {authorOpen && (
            <div className="absolute left-0 top-9 z-20 w-64 rounded-md border border-cyber-border-subtle bg-cyber-bg-panel p-2 shadow-lg">
              <div className="flex items-center gap-1">
                <input
                  value={authorKw}
                  onChange={(e) => setAuthorKw(e.target.value)}
                  placeholder={t('manage.authorSearch', { defaultValue: '搜索发帖人' })}
                  className="h-7 flex-1 px-2 rounded text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary placeholder:text-cyber-text-muted"
                />
                {authorIds.length > 0 && (
                  <button
                    type="button"
                    onClick={() => { setAuthorIds([]); setPage(1) }}
                    title={t('manage.clear', { defaultValue: '清空选择' })}
                    className="flex h-7 w-7 items-center justify-center rounded border border-cyber-border-subtle text-cyber-text-muted hover:text-cyber-neon-pink"
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>

              <div className="mt-1.5 max-h-60 space-y-0.5 overflow-y-auto">
                {authorsQ.isLoading ? (
                  <div className="py-3 text-center text-[11px] font-mono text-cyber-text-muted">
                    {t('manage.loading')}
                  </div>
                ) : (authorsQ.data || []).length === 0 ? (
                  <div className="py-3 text-center text-[11px] font-mono text-cyber-text-muted">
                    {t('manage.authorEmpty', { defaultValue: '没有匹配的发帖人' })}
                  </div>
                ) : (
                  (authorsQ.data || []).map((a) => {
                    const checked = authorIds.includes(a.id)
                    return (
                      <label
                        key={a.id}
                        className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 hover:bg-cyber-bg-tertiary/70"
                      >
                        <Checkbox
                          checked={checked}
                          onCheckedChange={() => toggleAuthor(a.id)}
                        />
                        <PlatformAvatar
                          name={a.name || '-'}
                          avatar={a.avatar}
                          theme={getPlatformTheme(platform || undefined)}
                          size={20}
                        />
                        <span className="min-w-0 flex-1 truncate text-xs text-cyber-text-primary">
                          {a.name || '-'}
                        </span>
                        <span className="flex-shrink-0 text-[10px] font-mono text-cyber-text-muted">
                          {a.story_count}
                        </span>
                      </label>
                    )
                  })
                )}
              </div>
            </div>
          )}
        </div>

        {/* 排序 */}
        <select
          value={sort}
          onChange={(e) => { setSort(e.target.value); setPage(1) }}
          className="h-8 px-2 rounded-md text-xs font-mono bg-cyber-bg-tertiary border border-cyber-border-subtle text-cyber-text-primary"
        >
          <option value="publish">{t('manage.sortPublish', { defaultValue: '按发布时间' })}</option>
          <option value="heat">{t('manage.sortHeat')}</option>
          <option value="time">{t('manage.sortTime')}</option>
          <option value="like">{t('manage.sortLike')}</option>
          <option value="comment">{t('manage.sortComment')}</option>
        </select>

        {/* 列表 / 网格视图切换（选择持久化） */}
        <div className="flex flex-shrink-0 items-center overflow-hidden rounded-md border border-cyber-border-subtle">
          <button
            type="button"
            onClick={() => setViewMode('list')}
            title={t('manage.viewList', { defaultValue: '列表视图' })}
            className={`flex h-8 w-8 items-center justify-center transition-all ${
              viewMode === 'list'
                ? 'bg-cyber-neon-cyan/20 text-cyber-neon-cyan'
                : 'text-cyber-text-muted hover:text-cyber-text-primary'
            }`}
          >
            <List className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setViewMode('grid')}
            title={t('manage.viewGrid', { defaultValue: '网格视图' })}
            className={`flex h-8 w-8 items-center justify-center transition-all ${
              viewMode === 'grid'
                ? 'bg-cyber-neon-cyan/20 text-cyber-neon-cyan'
                : 'text-cyber-text-muted hover:text-cyber-text-primary'
            }`}
          >
            <LayoutGrid className="h-3.5 w-3.5" />
          </button>
        </div>

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
          {authorIds.length > 0 && ` · ${t('manage.authorSelected', { count: authorIds.length, defaultValue: '发帖人 {{count}} 位' })}`}
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

      {/* 列表 / 网格 */}
      <div className={
        viewMode === 'grid'
          ? 'flex-1 overflow-y-auto min-h-0 grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 content-start'
          : 'flex-1 overflow-y-auto min-h-0 space-y-1'
      }>
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
            viewMode === 'grid' ? (
              <StoryCard
                key={it.id}
                it={it}
                checked={selected.has(it.id)}
                onCheck={() => toggle(it.id)}
                onOpen={() => setDetailId(it.id)}
              />
            ) : (
            <div
              key={it.id}
              onClick={() => setDetailId(it.id)}
              className="flex cursor-pointer items-start gap-3 px-3 py-2 rounded-md hover:bg-cyber-bg-tertiary/60 transition-colors"
            >
              <span className="mt-1" onClick={(e) => e.stopPropagation()}>
                <Checkbox
                  checked={selected.has(it.id)}
                  onCheckedChange={() => toggle(it.id)}
                />
              </span>
              <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                {/* 平台品牌标识：只在「全部平台」视图下显示；
                    进入某个平台页时列表已天然同质，再放标识是冗余 */}
                {!platform && <PlatformBrandMark platform={it.platform} size={16} />}
                {/* 发帖人：头像 + 昵称（列表模式昵称固定 2 字 + 省略号，hover 看全名） */}
                <span className="flex flex-shrink-0 items-center gap-1">
                  <PlatformAvatar
                    name={it.author || '-'}
                    avatar={it.author_avatar}
                    theme={getPlatformTheme(it.platform)}
                    size={18}
                  />
                  <AuthorShortName
                    name={it.author || '-'}
                    className="text-[11px] font-mono text-cyber-text-secondary"
                  />
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
                variant="ghost" size="sm"
                className="hidden h-7 px-2 font-mono flex-shrink-0 sm:flex"
                onClick={(e) => { e.stopPropagation(); setDetailId(it.id) }}
              >
                <Eye className="w-3 h-3 mr-1" />
                {t('manage.detail')}
              </Button>
            </div>
            )
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
