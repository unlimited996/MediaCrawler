import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Play, RotateCcw, Trash2, X, Loader2, Inbox,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  useCrawlerQueue, useQueueRemove, useQueueClear,
  useQueueRetry, useQueueResume,
} from '@/hooks/useCrawler'
import type { QueueItem, QueueItemStatus } from '@/types/crawler'

const STATUS_META: Record<QueueItemStatus, { label: string; dot: string; text: string }> = {
  pending: { label: '等待', dot: 'bg-cyber-neon-orange', text: 'text-cyber-neon-orange' },
  running: { label: '执行中', dot: 'bg-cyber-neon-cyan animate-pulse', text: 'text-cyber-neon-cyan' },
  done: { label: '完成', dot: 'bg-emerald-400', text: 'text-emerald-400' },
  failed: { label: '失败', dot: 'bg-cyber-neon-pink', text: 'text-cyber-neon-pink' },
  cancelled: { label: '已取消', dot: 'bg-cyber-text-muted', text: 'text-cyber-text-muted' },
}

function hhmmss(iso?: string | null) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toTimeString().slice(0, 8)
}

function StatPill({ label, value, className }: { label: string; value: number; className: string }) {
  return (
    <div className="flex-1 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/40 px-2 py-1.5 text-center">
      <div className={`text-sm font-mono font-bold ${className}`}>{value}</div>
      <div className="text-[10px] text-cyber-text-muted font-mono">{label}</div>
    </div>
  )
}

function Row({ item, onRemove, busy }: {
  item: QueueItem
  onRemove: (id: number) => void
  busy: boolean
}) {
  const meta = STATUS_META[item.status] ?? STATUS_META.pending
  const short = item.url.length > 46 ? `${item.url.slice(0, 46)}…` : item.url

  return (
    <li className="group flex items-start gap-2 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/30 px-2.5 py-2 hover:border-cyber-border-DEFAULT transition-colors">
      <span className={`mt-1.5 h-1.5 w-1.5 rounded-full flex-shrink-0 ${meta.dot}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span
            className="truncate text-[11px] font-mono text-cyber-text-primary"
            title={item.url}
          >
            {short}
          </span>
          <span className="flex-shrink-0 rounded bg-cyber-bg-tertiary px-1 text-[9px] font-mono text-cyber-text-muted">
            {item.platform}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-2 text-[9px] font-mono text-cyber-text-muted">
          <span className={meta.text}>{meta.label}</span>
          <span>{hhmmss(item.added_at)}</span>
          {item.error ? (
            <span className="truncate text-cyber-neon-pink" title={item.error}>
              {item.error}
            </span>
          ) : null}
        </div>
      </div>
      {item.status === 'running' ? (
        <Loader2 className="mt-1 h-3.5 w-3.5 flex-shrink-0 animate-spin text-cyber-neon-cyan" />
      ) : (
        <button
          type="button"
          onClick={() => onRemove(item.id)}
          disabled={busy}
          title="从队列移除"
          className="mt-0.5 flex-shrink-0 text-cyber-text-muted opacity-0 transition-all hover:text-cyber-neon-pink group-hover:opacity-100 disabled:cursor-not-allowed"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </li>
  )
}

export function CrawlerQueuePanel() {
  const { t } = useTranslation('config')

  const { data, isLoading } = useCrawlerQueue()
  const removeM = useQueueRemove()
  const clearM = useQueueClear()
  const retryM = useQueueRetry()
  const resumeM = useQueueResume()

  const items = useMemo(() => data?.items ?? [], [data])
  const stats = {
    pending: data?.pending ?? 0,
    running: data?.running ?? 0,
    done: data?.done ?? 0,
    failed: data?.failed ?? 0,
    cancelled: data?.cancelled ?? 0,
  }
  const hasFailed = stats.failed + stats.cancelled > 0
  const busy = removeM.isPending || clearM.isPending
    || retryM.isPending || resumeM.isPending

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="p-3 space-y-3 overflow-hidden flex flex-col min-h-0 flex-1">
        {/* 统计 */}
        <div className="flex gap-1.5">
          <StatPill label={t('queue.pending', '待执行')} value={stats.pending} className="text-cyber-neon-orange" />
          <StatPill label={t('queue.running', '执行中')} value={stats.running} className="text-cyber-neon-cyan" />
          <StatPill label={t('queue.done', '完成')} value={stats.done} className="text-emerald-400" />
          <StatPill label={t('queue.failed', '失败')} value={stats.failed} className="text-cyber-neon-pink" />
        </div>

        {/* 操作栏 */}
        <div className="grid grid-cols-2 gap-1.5">
          <Button
            variant="outline"
            onClick={() => resumeM.mutate()}
            disabled={busy || stats.pending === 0}
            title={stats.pending === 0 ? '队列里没有待执行任务' : '立即开始执行队首任务'}
            className="h-7 text-[10px] font-mono border-cyber-border-subtle hover:border-cyber-neon-cyan/50 hover:text-cyber-neon-cyan"
          >
            {resumeM.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
            {t('queue.resume', '继续队列')}
          </Button>
          <Button
            variant="outline"
            onClick={() => retryM.mutate({ all: true })}
            disabled={busy || !hasFailed}
            className="h-7 text-[10px] font-mono border-cyber-border-subtle hover:border-cyber-neon-cyan/50 hover:text-cyber-neon-cyan"
          >
            {retryM.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />}
            {t('queue.retryFailed', '重试失败')}
          </Button>
          <Button
            variant="outline"
            onClick={() => clearM.mutate(['done'])}
            disabled={busy || stats.done === 0}
            className="h-7 text-[10px] font-mono border-cyber-border-subtle hover:border-cyber-neon-orange/50 hover:text-cyber-neon-orange"
          >
            <Trash2 className="w-3 h-3" />
            {t('queue.clearDone', '清已完成')}
          </Button>
          <Button
            variant="outline"
            onClick={() => clearM.mutate(undefined)}
            disabled={busy || items.length === 0}
            className="h-7 text-[10px] font-mono border-cyber-border-subtle hover:border-cyber-neon-pink/50 hover:text-cyber-neon-pink"
          >
            <Trash2 className="w-3 h-3" />
            {t('queue.clearAll', '全部清空')}
          </Button>
        </div>

        {/* 列表 */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          {isLoading && !items.length ? (
            <div className="flex items-center justify-center py-8 text-[10px] font-mono text-cyber-text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
              加载中…
            </div>
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 gap-1.5 text-cyber-text-muted">
              <Inbox className="h-6 w-6 opacity-40" />
              <p className="text-[10px] font-mono">{t('queue.empty', '队列为空')}</p>
              <p className="text-[9px] font-mono opacity-70">
                {t('queue.emptyHint', '爬虫运行中提交的链接会自动排到这里')}
              </p>
            </div>
          ) : (
            <ul className="space-y-1.5 pr-0.5">
              {items.map((item) => (
                <Row
                  key={item.id}
                  item={item}
                  busy={removeM.isPending}
                  onRemove={(id) => removeM.mutate(id)}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
