import { useEffect, useMemo, useRef, useState } from 'react'
import { StoryEditLogDialog } from '@/components/data/StoryEditLogDialog'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  ArrowLeft, Loader2, RefreshCw, Trash2, Bell, BellOff, UserPlus, UserCheck, History
} from 'lucide-react'
import { crawlerApi, storyApi } from '@/lib/api'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { getPlatformTheme } from './platformTheme'
import { pickSourceUrl, extractTopics, RepliesContext } from './shared'
import { CommentToolbar } from './CommentToolbar'
import type { CommentSort, HasRepliesFilter, StanceFilter, StanceStats } from './CommentToolbar'
import { XhsLayout } from './XhsLayout'
import { ZhihuLayout } from './ZhihuLayout'
import { WeiboLayout } from './WeiboLayout'
import { DouyinLayout } from './DouyinLayout'
import { BiliLayout } from './BiliLayout'
import { TiebaLayout } from './TiebaLayout'
import { KuaishouLayout } from './KuaishouLayout'
import { TwitterLayout } from './TwitterLayout'
import { FacebookLayout } from './FacebookLayout'
import { RedditLayout } from './RedditLayout'
import { DefaultLayout } from './DefaultLayout'

/** 每页主评论数：滚动到底自动再拉一页（只翻已入库的数据，不触发新抓取） */
const COMMENT_PAGE_SIZE = 20

const EMPTY_STANCE_STATS: StanceStats = {
  agree: 0, neutral: 0, oppose: 0, total: 0, agreement: '',
}

/**
 * 平台化数据详情页
 *
 * 按平台分发到各自的布局组件，使布局 / 组件风格 / 交互细节与对应平台保持一致：
 * - xhs    → 小红书笔记卡
 * - zhihu  → 知乎问答卡
 * - wb     → 微博卡片
 * - dy     → 抖音深色播放卡
 * - bili   → B站视频卡
 * - tieba  → 贴吧帖子卡
 * - ks     → 快手视频卡
 * - 其他   → 通用卡片
 */
export function PlatformStoryDetail({
  storyId,
  onBack,
}: {
  storyId: number
  onBack: () => void
}) {
  const { t } = useTranslation('data')
  const qc = useQueryClient()

  const detailQ = useQuery({
    queryKey: ['storyDetail', storyId],
    queryFn: async () => (await storyApi.detail(storyId)).data,
  })

  // ---------- 评论筛选状态（Steam 风格：排序/立场走后端，回复/关键词走前端） ----------
  const [cmtSort, setCmtSort] = useState<CommentSort>('like')
  const [cmtStance, setCmtStance] = useState<StanceFilter>('')
  const [cmtHasReplies, setCmtHasReplies] = useState<HasRepliesFilter>('all')
  const [cmtKeyword, setCmtKeyword] = useState('')

  // ---------- 评论：滚动到底自动加载下一页 ----------
  // 只分页读取库里已抓到的评论，不会触发新的抓取任务。
  // sort / stance 变化会改变 queryKey 重新拉取（服务端筛选）。
  const commentsQ = useInfiniteQuery({
    queryKey: ['storyComments', storyId, cmtSort, cmtStance],
    initialPageParam: 1,
    queryFn: async ({ pageParam }) =>
      (await storyApi.comments(
        storyId,
        cmtSort === 'like' ? 'like' : 'ts',
        pageParam as number,
        COMMENT_PAGE_SIZE,
        20,
        cmtStance || undefined,
      )).data,
    getNextPageParam: (last) => {
      const size = last?.page_size ?? COMMENT_PAGE_SIZE
      const page = last?.page ?? 1
      const total = last?.total ?? 0
      return page * size < total ? page + 1 : undefined
    },
  })

  // 立场统计取第一页的快照（全量口径，不随分页变化）
  const stanceStats: StanceStats = useMemo(() => {
    const first = commentsQ.data?.pages?.[0]
    const s = first?.stance_stats
    if (!s) return EMPTY_STANCE_STATS
    return {
      agree: s.agree ?? 0,
      neutral: s.neutral ?? 0,
      oppose: s.oppose ?? 0,
      total: s.total ?? 0,
      agreement: s.agreement ?? '',
    }
  }, [commentsQ.data])

  // 客户端二次过滤：是否含回复 + 关键词（对已加载的主评论生效）
  const allComments = useMemo(
    () => (commentsQ.data?.pages || []).flatMap((p: any) => p.items || []),
    [commentsQ.data],
  )
  const comments = useMemo(() => {
    const kw = cmtKeyword.trim().toLowerCase()
    return allComments.filter((c: any) => {
      if (cmtHasReplies === 'with' && !(c.reply_count > 0)) return false
      if (cmtHasReplies === 'without' && (c.reply_count > 0)) return false
      if (kw && !String(c.content || '').toLowerCase().includes(kw)) return false
      return true
    })
  }, [allComments, cmtHasReplies, cmtKeyword])

  // 「正在显示与上方筛选条件匹配的 N 篇评测」：N = 当前筛选结果数。
  // 服务端筛选（情感）用接口 total；客户端过滤时用本地条数。
  const serverTotal = commentsQ.data?.pages?.[0]?.total ?? 0
  const clientFiltered = cmtHasReplies !== 'all' || cmtKeyword.trim().length > 0
  const filteredTotal = clientFiltered ? comments.length : serverTotal

  // ---------- 子评论开关：默认隐藏，只显示条数（开关在评论区筛选栏） ----------
  const [showReplies, setShowReplies] = useState(false)

  // ---------- 关注（定时更新）：复用定时调度器，每天一次 ----------
  const watchQ = useQuery({
    queryKey: ['storyWatch', storyId],
    queryFn: async () => (await storyApi.watch(storyId)).data,
    // 30s 轮询：定时任务跑到一半或触发自动停止时能及时反映到开关上
    refetchInterval: 30_000,
  })

  const watchM = useMutation({
    mutationFn: ({ kind, enabled }: { kind: 'note' | 'author'; enabled: boolean }) =>
      storyApi.setWatch(storyId, kind, enabled),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: ['storyWatch', storyId] })
      qc.invalidateQueries({ queryKey: ['schedulerTasks'] })
      toast.success(vars.enabled
        ? (t('manage.watchOn', { defaultValue: '已开启关注：立即更新一次，之后每天一次' }))
        : (t('manage.watchOff', { defaultValue: '已取消关注' })))
    },
    onError: (e: any) => toast.error(
      `${t('manage.watchFail', { defaultValue: '关注设置失败' })}: ${e?.response?.data?.detail || e?.message || ''}`),
  })

  const toggleWatch = (kind: 'note' | 'author') => {
    const cur = watchQ.data?.[kind]
    if (!cur?.supported) return
    watchM.mutate({ kind, enabled: !cur.enabled })
  }

  // 目标失效（帖子被删 / 作者注销 / 无权限）时任务会自动停用，这里提示一次
  const stoppedNotified = useRef('')
  useEffect(() => {
    const data = watchQ.data
    if (!data) return
    for (const kind of ['note', 'author'] as const) {
      const w = data[kind]
      if (!w?.stopped) continue
      const key = `${kind}:${w.task_id}:${w.stop_reason}`
      if (stoppedNotified.current === key) continue
      stoppedNotified.current = key
      const label = kind === 'note'
        ? t('manage.watchNote', { defaultValue: '关注此贴' })
        : t('manage.watchAuthor', { defaultValue: '关注此人' })
      toast.error(
        `${t('manage.watchStopped', { defaultValue: '已自动停止关注' })}｜${label}：${w.stop_reason}`,
        { duration: 8000 },
      )
    }
  }, [watchQ.data, t])

  // ---------- 滚动加载：到底自动翻页 ----------
  const scrollRef = useRef<HTMLDivElement>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = commentsQ
  useEffect(() => {
    const el = sentinelRef.current
    const root = scrollRef.current
    if (!el || !root || !hasNextPage) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetchingNextPage) fetchNextPage()
      },
      { root, rootMargin: '240px 0px' },
    )
    io.observe(el)
    return () => io.disconnect()
    // detailQ.isLoading 必须进依赖：布局（含哨兵）要等详情加载完才挂载
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, detailQ.isLoading])

  // ---------- 重新拉取：按原文链接重跑一次详情抓取 ----------
  // 新数据经 v2 同步 upsert 叠加到旧数据；若平台已删内容，
  // 爬虫侧会把 story 标记 deleted，刷新后显示「已删除」徽标。
  const [refetching, setRefetching] = useState(false)
  /** 编辑记录弹层（P2） */
  const [editsOpen, setEditsOpen] = useState(false)
  const statusQ = useQuery({
    queryKey: ['crawlerStatus'],
    queryFn: async () => (await crawlerApi.getStatus()).data,
    enabled: refetching,
    refetchInterval: 2000,
  })

  const handleRefetch = async () => {
    const sourceUrl = pickSourceUrl(d)
    if (!sourceUrl) {
      toast.error(t('manage.noSourceUrl') || '这条数据没有原文链接，无法重新拉取')
      return
    }
    try {
      const res = await crawlerApi.start({
        platform: d.platform,
        crawler_type: 'detail',
        specified_ids: sourceUrl,
        enable_comments: true,
        enable_sub_comments: true,
        enable_media: true,
        // 直接写 PG：v2 同步链路只对 DB 存储模式生效，数据管理页才有数据
        save_option: 'postgres',
      } as any)
      setRefetching(true)
      // 爬虫正忙时后端会把这条排进队列，而不是拒绝
      if ((res.data as { queued?: number } | undefined)?.queued) {
        toast.info(t('manage.refetchQueued') || '爬虫正在运行，已加入抓取队列，轮到它时会自动执行')
      } else {
        toast.info(t('manage.refetchStarted') || '已开始重新拉取，完成后自动刷新')
      }
    } catch (e: any) {
      toast.error(`${t('manage.refetchFail') || '重新拉取失败'}: ${e?.message || ''}`)
    }
  }

  // 爬虫跑完 → 刷新详情与评论
  useEffect(() => {
    if (refetching && statusQ.data && statusQ.data.status !== 'running' && statusQ.data.status !== 'stopping') {
      setRefetching(false)
      qc.invalidateQueries({ queryKey: ['storyDetail', storyId] })
      qc.invalidateQueries({ queryKey: ['storyComments', storyId] })
      qc.invalidateQueries({ queryKey: ['stories'] })
      toast.success(t('manage.refetchDone') || '重新拉取完成，数据已刷新')
    }
  }, [refetching, statusQ.data, storyId, qc, t])

  const deleteM = useMutation({
    mutationFn: () => storyApi.remove(storyId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['stories'] })
      qc.removeQueries({ queryKey: ['storyDetail', storyId] })
      onBack()
    },
  })

  const handleDelete = () => {
    if (!window.confirm(t('manage.deleteConfirm') || '确认【永久删除】这条内容？删除后不可恢复。')) return
    deleteM.mutate()
  }

  // 话题从标题/正文里摘出来：标题栏只留纯文本，话题走正文底部的彩色标签
  // topics 一并并进 d.tags，各布局现有的标签渲染块直接复用
  const { d } = useMemo(() => extractTopics(detailQ.data), [detailQ.data])
  const theme = getPlatformTheme(d.platform)

  const commentsToolbar = (
    <CommentToolbar
      filteredTotal={filteredTotal}
      stats={stanceStats}
      sort={cmtSort}
      stance={cmtStance}
      hasReplies={cmtHasReplies}
      keyword={cmtKeyword}
      showReplies={showReplies}
      onSort={setCmtSort}
      onStance={setCmtStance}
      onHasReplies={setCmtHasReplies}
      onKeyword={setCmtKeyword}
      onToggleReplies={() => setShowReplies((v) => !v)}
    />
  )

  const layoutProps = {
    d,
    comments,
    commentsTotal: filteredTotal,
    commentsToolbar,
    t: t as (key: string, opts?: any) => string,
    theme,
  }

  const renderLayout = () => {
    switch (theme.layout) {
      case 'xhs':
        return <XhsLayout {...layoutProps} />
      case 'zhihu':
        return <ZhihuLayout {...layoutProps} />
      case 'weibo':
        return <WeiboLayout {...layoutProps} />
      case 'douyin':
        return <DouyinLayout {...layoutProps} />
      case 'bili':
        return <BiliLayout {...layoutProps} />
      case 'tieba':
        return <TiebaLayout {...layoutProps} />
      case 'kuaishou':
        return <KuaishouLayout {...layoutProps} />
      case 'twitter':
        return <TwitterLayout {...layoutProps} />
      case 'facebook':
        return <FacebookLayout {...layoutProps} />
      case 'reddit':
        return <RedditLayout {...layoutProps} />
      default:
        return <DefaultLayout {...layoutProps} />
    }
  }

  return (
    <div className="h-full flex flex-col gap-3">
      {/* 顶部返回栏：平台徽标使用平台品牌色 */}
      <div className="flex flex-shrink-0 items-center gap-2">
        <Button variant="ghost" size="sm" className="h-8 font-mono" onClick={onBack}>
          <ArrowLeft className="mr-1 h-3.5 w-3.5" />
          {t('manage.back')}
        </Button>

        {/* 关注：【关注此贴】每天定时更新这条内容；【关注此人】每天定时更新该作者 */}
        <button
          type="button"
          onClick={() => toggleWatch('note')}
          disabled={!watchQ.data?.note?.supported || watchM.isPending}
          className={`flex h-8 items-center gap-1.5 rounded border px-2 font-mono text-[11px] transition-all disabled:opacity-40 ${
            watchQ.data?.note?.enabled
              ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
              : 'border-cyber-border-DEFAULT text-cyber-text-secondary hover:border-cyber-neon-cyan/40 hover:text-cyber-neon-cyan'
          }`}
          title={watchQ.data?.note?.supported
            ? (watchQ.data.note.stopped
                ? `${t('manage.watchStopped', { defaultValue: '已自动停止关注' })}：${watchQ.data.note.stop_reason}`
                : t('manage.watchNoteHint', { defaultValue: '每天定时拉取一次这条内容并更新' }))
            : t('manage.watchNoteUnsupported', { defaultValue: '该内容没有原文链接，无法关注' })}
        >
          {watchQ.data?.note?.enabled ? (
            <Bell className="h-3.5 w-3.5" />
          ) : (
            <BellOff className="h-3.5 w-3.5" />
          )}
          {t('manage.watchNote', { defaultValue: '关注此贴' })}
        </button>
        <button
          type="button"
          onClick={() => toggleWatch('author')}
          disabled={!watchQ.data?.author?.supported || watchM.isPending}
          className={`flex h-8 items-center gap-1.5 rounded border px-2 font-mono text-[11px] transition-all disabled:opacity-40 ${
            watchQ.data?.author?.enabled
              ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
              : 'border-cyber-border-DEFAULT text-cyber-text-secondary hover:border-cyber-neon-cyan/40 hover:text-cyber-neon-cyan'
          }`}
          title={watchQ.data?.author?.supported
            ? (watchQ.data.author.stopped
                ? `${t('manage.watchStopped', { defaultValue: '已自动停止关注' })}：${watchQ.data.author.stop_reason}`
                : t('manage.watchAuthorHint', { defaultValue: '每天定时拉取一次该作者的内容并更新' }))
            : t('manage.watchAuthorUnsupported', { defaultValue: '该作者没有主页链接，无法关注' })}
        >
          {watchQ.data?.author?.enabled ? (
            <UserCheck className="h-3.5 w-3.5" />
          ) : (
            <UserPlus className="h-3.5 w-3.5" />
          )}
          {t('manage.watchAuthor', { defaultValue: '关注此人' })}
        </button>

        <button
          type="button"
          onClick={() => setEditsOpen(true)}
          className="flex h-8 items-center gap-1.5 rounded border border-cyber-border-DEFAULT px-2 font-mono text-[11px] text-cyber-text-secondary transition-all hover:border-cyber-neon-cyan/40 hover:text-cyber-neon-cyan"
          title={t('manage.editLogHint', { defaultValue: '查看这条内容被修改前后的差异' })}
        >
          <History className="h-3.5 w-3.5" />
          {t('manage.editLog', { defaultValue: '编辑记录' })}
          {(d?.edit_count ?? 0) > 0 && (
            <span className="rounded bg-cyber-bg-tertiary px-1 text-[10px] text-cyber-text-muted">
              {d?.edit_count}
            </span>
          )}
        </button>

        <Button
          variant="ghost"
          size="sm"
          className="ml-auto h-8 px-2 font-mono text-cyber-text-secondary hover:bg-cyber-neon-cyan/10"
          onClick={handleRefetch}
          disabled={refetching || detailQ.isFetching}
          title={t('manage.refetch') || '重新拉取'}
        >
          {refetching ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 px-2 font-mono text-cyber-neon-pink hover:bg-cyber-neon-pink/10"
          onClick={handleDelete}
          disabled={deleteM.isPending}
          title={t('manage.delete') || '删除'}
        >
          {deleteM.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Trash2 className="h-3.5 w-3.5" />
          )}
        </Button>
        <span
          className="rounded-full px-2.5 py-0.5 text-[11px] font-medium"
          style={{ color: theme.accent, background: theme.accentSoft }}
        >
          {theme.label}
        </span>
        {/* 平台侧已删除的标记（数据仍保留可查） */}
        {d.status === 'deleted' && (
          <span
            className="rounded-full bg-cyber-neon-pink/15 px-2.5 py-0.5 text-[11px] font-medium text-cyber-neon-pink"
            title={t('manage.deletedHint') || '原内容在平台已被删除，以下是最后抓到的快照'}
          >
            {t('manage.deletedBadge') || '已删除'}
          </span>
        )}
        {/* 标题栏只显示去掉话题后的纯标题 */}
        <span className="truncate text-sm text-cyber-text-primary">{d.title || ''}</span>
        {detailQ.isFetching && (
          <Loader2 className="ml-auto h-3.5 w-3.5 animate-spin text-cyber-text-muted" />
        )}
      </div>

      {/* 详情主体 */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto pb-4">
        {detailQ.isLoading ? (
          <div className="py-10 text-center font-mono text-cyber-text-muted animate-pulse">
            {t('manage.loading')}
          </div>
        ) : (
          <RepliesContext.Provider value={{ showReplies, storyId }}>
            {renderLayout()}

            {/* 滚动到底自动加载下一页评论；没有更多时给出总数提示 */}
            <div className="mx-auto max-w-2xl px-1 py-4">
              <div ref={sentinelRef} />
              {commentsQ.isFetchingNextPage && (
                <div className="flex items-center justify-center gap-2 py-2 font-mono text-[11px] text-cyber-text-muted">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {t('manage.loadingMore', { defaultValue: '加载更多评论...' })}
                </div>
              )}
              {!hasNextPage && comments.length > 0 && (
                <div className="py-2 text-center font-mono text-[11px] text-cyber-text-muted">
                  {t('manage.allCommentsLoaded', {
                    count: comments.length,
                    total: serverTotal,
                    defaultValue: `已加载全部 ${serverTotal} 条主评论`,
                  })}
                </div>
              )}
              {commentsQ.isError && (
                <button
                  type="button"
                  onClick={() => commentsQ.refetch()}
                  className="mx-auto block font-mono text-[11px] text-cyber-neon-pink hover:underline"
                >
                  {t('manage.loadCommentsFail', { defaultValue: '评论加载失败，点击重试' })}
                </button>
              )}
            </div>
          </RepliesContext.Provider>
        )}
      </div>

      {/* 编辑记录：前后比对弹层（P2） */}
      <StoryEditLogDialog
        storyId={storyId}
        open={editsOpen}
        onClose={() => setEditsOpen(false)}
      />
    </div>
  )
}
