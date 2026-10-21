import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Flame, Loader2, Radio, ArrowUp, ArrowDown, AlertCircle, X } from 'lucide-react'
import { discoverApi, accountsApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { toast } from 'sonner'

function fmtHot(v: number | null | undefined): string {
  if (v === null || v === undefined) return '-'
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(1)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)}万`
  return String(v)
}

/** 日历日标签（day_offset: 0=今天 1=昨天 2=前天 …） */
function dayLabel(offset: number, t: (k: string, o?: any) => string): string {
  if (offset <= 0) return t('today')
  if (offset === 1) return t('yesterday')
  if (offset === 2) return t('dayBefore')
  return t('daysAgo', { count: offset })
}

export function HotTimelineView() {
  const { t } = useTranslation('discover')
  const qc = useQueryClient()
  const [platform, setPlatform] = useState('wb')
  const [picked, setPicked] = useState<number | null>(null)

  const boardsQ = useQuery({
    queryKey: ['discoverBoards'],
    queryFn: async () => (await discoverApi.boards()).data,
  })

  const historyQ = useQuery({
    queryKey: ['hotHistory', platform],
    queryFn: async () => (await discoverApi.history(platform, 100)).data.items,
  })

  // 后端已按东八区策展（今天≤10 条/每小时最新一条、昨天前天各 1 条、最多前 3 天），
  // 并按时间升序返回，前端直接渲染
  const snapshots = useMemo(() => [...(historyQ.data || [])], [historyQ.data])
  const activeId = picked ?? (snapshots.length ? snapshots[snapshots.length - 1].board_id : null)
  const activeIdx = snapshots.findIndex((s) => s.board_id === activeId)
  const prev = activeIdx > 0 ? snapshots[activeIdx - 1] : null

  const boardQ = useQuery({
    queryKey: ['hotBoard', activeId],
    enabled: !!activeId,
    queryFn: async () => (await discoverApi.hotBoard(activeId as number)).data.data,
  })

  const compareQ = useQuery({
    queryKey: ['hotCompare', prev?.board_id, activeId],
    enabled: !!prev && !!activeId,
    queryFn: async () =>
      (await discoverApi.compare(prev!.board_id, activeId as number)).data.data,
  })

  const captureM = useMutation({
    mutationFn: () => discoverApi.capture(platform),
    onSuccess: (res) => {
      const payload = res.data as any
      if (payload?.code === 0) {
        toast.success(t('captureOk'))
        qc.invalidateQueries({ queryKey: ['hotHistory', platform] })
        qc.invalidateQueries({ queryKey: ['hotBoard'] })
        setPicked(payload.data?.board_id ?? null)
      } else {
        const msg = payload?.message || ''
        // 401 = 缺登录态，给出可操作的引导，而不是只抛一个 HTTP 错误
        toast.error(
          /401|Authorization/i.test(msg)
            ? `${t('needsLoginHint')}`
            : `${t('captureFail')}: ${msg}`,
        )
      }
    },
    onError: (e: any) => {
      const msg = e?.message || ''
      toast.error(
        /401|Authorization/i.test(msg) ? `${t('needsLoginHint')}` : `${t('captureFail')}: ${msg}`,
      )
    },
  })

  const items = boardQ.data?.items || []
  const maxHot = Math.max(1, ...items.map((i) => i.hot_value || 0))
  const newSet = new Set(compareQ.data?.new || [])
  const movedMap = new Map((compareQ.data?.moved || []).map((m) => [m.title, m]))

  // 删除快照：删完若当前选中的正是它，则回退到最新一份
  const deleteM = useMutation({
    mutationFn: (boardId: number) => discoverApi.deleteBoard(boardId),
    onSuccess: (_res, boardId) => {
      toast.success(t('deleteOk'))
      if (picked === boardId) setPicked(null)
      qc.invalidateQueries({ queryKey: ['hotHistory', platform] })
      qc.invalidateQueries({ queryKey: ['hotBoard', boardId] })
    },
    onError: (e: any) => toast.error(`${t('deleteFail')}: ${e?.message || ''}`),
  })

  const handleDelete = (e: React.MouseEvent, boardId: number) => {
    e.stopPropagation()   // 别触发父按钮的"选中该快照"
    if (!window.confirm(t('deleteConfirm'))) return
    deleteM.mutate(boardId)
  }

  // 该平台是否已有可用 Cookie（热榜抓取需要登录态时靠它）
  const accountQ = useQuery({
    queryKey: ['accountStatus', platform],
    queryFn: async () => (await accountsApi.list(platform)).data.data?.items || [],
  })
  const hasCookie = (accountQ.data || []).some((a) => a.has_cookie && a.status === 'active')

  const platforms = boardsQ.data?.hot || []

  return (
    <div className="h-full flex flex-col gap-3">
      {/* 头部：平台切换 + 抓取 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <Flame className="w-5 h-5 text-cyber-neon-orange" />
          <h2 className="text-base font-mono font-bold text-cyber-text-primary tracking-wider">
            {t('title')}
          </h2>
          <span className="text-xs font-mono text-cyber-text-muted hidden sm:inline">
            {t('subtitle')}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 flex-wrap">
            {platforms.map((p) => (
              <button
                key={p.platform}
                onClick={() => {
                  setPlatform(p.platform)
                  setPicked(null)
                }}
                className={`px-3 py-1.5 rounded-md text-xs font-mono transition-all ${
                  platform === p.platform
                    ? 'bg-cyber-neon-orange text-black font-bold'
                    : 'bg-cyber-bg-tertiary text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle'
                }`}
              >
                {p.board_name}
              </button>
            ))}
          </div>
          {/* 登录态指示：需要登录的平台没有 Cookie 时提前告知 */}
          {!hasCookie && (
            <span
              className="text-[11px] font-mono text-cyber-neon-orange/80 hidden md:inline"
              title={t('needsLoginHint')}
            >
              ⚠ {t('noCookie')}
            </span>
          )}

          <Button
            variant="outline"
            size="sm"
            className="font-mono"
            disabled={captureM.isPending}
            onClick={() => captureM.mutate()}
          >
            {captureM.isPending ? (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            ) : (
              <Radio className="w-4 h-4 mr-1" />
            )}
            {captureM.isPending ? t('capturing') : t('capture')}
          </Button>
        </div>
      </div>

      {/* 时间轴：按东八区日历日分组，节点右上角可删除 */}
      {snapshots.length > 0 && (
        <div className="flex-shrink-0 border-y border-cyber-border-subtle py-2">
          <div className="flex items-center gap-2 overflow-x-auto pb-1">
            {snapshots.map((s, i) => {
              const active = s.board_id === activeId
              // 与上一条不同日期时，插入日期分隔标签
              const showDaySep = i === 0 || snapshots[i - 1].day_offset !== s.day_offset
              return (
                <div key={s.board_id} className="flex items-center gap-2 flex-shrink-0">
                  {showDaySep && (
                    <span className="px-1 text-[11px] font-mono text-cyber-neon-orange font-bold">
                      {dayLabel(s.day_offset, t)}
                    </span>
                  )}
                  <div className="relative">
                    <button
                      onClick={() => setPicked(s.board_id)}
                      className={`flex-shrink-0 px-2.5 py-1.5 pr-6 rounded-md text-[11px] font-mono border transition-all ${
                        active
                          ? 'bg-cyber-neon-cyan text-black border-cyber-neon-cyan font-bold'
                          : 'bg-cyber-bg-tertiary text-cyber-text-secondary border-cyber-border-subtle hover:border-cyber-neon-cyan/50'
                      }`}
                    >
                      <div>{s.captured_at_short}</div>
                      <div className="opacity-70">
                        {t('items', { count: s.item_count })}
                        {i === snapshots.length - 1 && ` · ${t('latest')}`}
                      </div>
                    </button>
                    {/* 删除按钮：节点右上角 */}
                    <button
                      onClick={(e) => handleDelete(e, s.board_id)}
                      disabled={deleteM.isPending}
                      title={t('delete')}
                      className={`absolute -top-1.5 -right-1.5 flex h-4 w-4 items-center justify-center rounded-full border transition-all ${
                        active
                          ? 'border-cyber-neon-cyan bg-black text-cyber-neon-cyan hover:bg-cyber-neon-pink hover:text-black hover:border-cyber-neon-pink'
                          : 'border-cyber-border-subtle bg-cyber-bg-panel text-cyber-text-muted hover:bg-cyber-neon-pink hover:text-black hover:border-cyber-neon-pink'
                      }`}
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
          <div className="flex items-center justify-between gap-3 mt-1">
            {prev ? (
              <div className="text-[11px] font-mono text-cyber-text-muted">
                {t('comparedWith')}：{prev.captured_at_local}
              </div>
            ) : (
              <span />
            )}
            <div className="text-[11px] font-mono text-cyber-text-muted/70 hidden sm:block">
              {t('tzNote')} · {t('timelineRule')}
            </div>
          </div>
        </div>
      )}

      {/* 榜单 */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {historyQ.isLoading ? (
          <div className="text-center py-10 text-cyber-text-muted font-mono animate-pulse">
            {t('loading')}
          </div>
        ) : snapshots.length === 0 ? (
          <div className="flex flex-col items-center justify-center text-center py-12">
            <AlertCircle className="w-12 h-12 text-cyber-text-muted/40 mb-3" />
            <h3 className="text-sm font-mono text-cyber-text-primary mb-1">{t('noSnapshot')}</h3>
            <p className="text-xs font-mono text-cyber-text-muted max-w-md">
              {t('noSnapshotHint')}
            </p>
          </div>
        ) : boardQ.isLoading ? (
          <div className="text-center py-10 text-cyber-text-muted font-mono animate-pulse">
            {t('loading')}
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-10 text-cyber-text-muted font-mono">{t('empty')}</div>
        ) : (
          <>
            <div className="flex items-center gap-2 px-1 pb-2 text-[11px] font-mono text-cyber-text-muted">
              <span className="text-cyber-neon-orange">{t('viewing')}</span>
              <span>{boardQ.data?.captured_at_local}</span>
              <span className="opacity-60">({t('tzNote')})</span>
            </div>
            <div className="space-y-1">
            {items.map((it) => {
              const mv = movedMap.get(it.title)
              const pct = Math.max(3, ((it.hot_value || 0) / maxHot) * 100)
              return (
                <div
                  key={it.rank_no}
                  className="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-cyber-bg-tertiary/60 transition-colors group"
                >
                  <span
                    className={`w-6 text-center text-xs font-mono font-bold flex-shrink-0 ${
                      it.rank_no <= 3 ? 'text-cyber-neon-orange' : 'text-cyber-text-muted'
                    }`}
                  >
                    {it.rank_no}
                  </span>

                  <a
                    href={it.content_url || '#'}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex-1 min-w-0 text-sm text-cyber-text-primary hover:text-cyber-neon-cyan truncate"
                    title={it.title}
                  >
                    {it.title}
                  </a>

                  {newSet.has(it.title) && (
                    <Badge className="text-[10px] bg-cyber-neon-orange/20 text-cyber-neon-orange border-cyber-neon-orange/40 flex-shrink-0">
                      {t('new')}
                    </Badge>
                  )}
                  {mv && mv.delta > 0 && (
                    <span
                      className="text-[11px] font-mono text-red-500 flex-shrink-0"
                      title={t('up')}
                    >
                      <ArrowUp className="w-3 h-3 inline" />
                      {mv.delta}
                    </span>
                  )}
                  {mv && mv.delta < 0 && (
                    <span
                      className="text-[11px] font-mono text-green-500 flex-shrink-0"
                      title={t('down')}
                    >
                      <ArrowDown className="w-3 h-3 inline" />
                      {-mv.delta}
                    </span>
                  )}
                  {it.tag && (
                    <Badge
                      variant="outline"
                      className="text-[10px] font-mono flex-shrink-0 hidden sm:inline-flex"
                    >
                      {it.tag}
                    </Badge>
                  )}

                  <div className="w-24 h-1.5 bg-cyber-bg-tertiary rounded-full overflow-hidden flex-shrink-0 hidden md:block">
                    <div
                      className="h-full bg-cyber-neon-orange rounded-full"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="w-16 text-right text-xs font-mono text-cyber-text-muted flex-shrink-0">
                    {fmtHot(it.hot_value)}
                  </span>
                </div>
              )
            })}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
