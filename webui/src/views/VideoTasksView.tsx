import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Ban, ChevronDown, ChevronRight, Film, Loader2, Play, RefreshCw, Terminal, Wifi,
} from 'lucide-react'
import { videoApi, type VideoAssetRow } from '@/lib/api'
import { formatBytes } from '@/lib/format'

/** 视频转储执行记录：状态 / 耗时 / 过程日志 / 终止。 */

type Filter = '' | 'pending' | 'uploading' | 'uploaded' | 'failed' | 'cancelled'

const STATUS_META: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队中', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  uploading: { label: '处理中', cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30' },
  uploaded: { label: '已完成', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  failed: { label: '失败', cls: 'bg-rose-500/15 text-rose-300 border-rose-500/30' },
  cancelled: { label: '已终止', cls: 'bg-slate-500/15 text-slate-400 border-slate-500/30' },
}

const FILTERS: { key: Filter; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'pending', label: '排队中' },
  { key: 'uploading', label: '处理中' },
  { key: 'uploaded', label: '已完成' },
  { key: 'failed', label: '失败' },
  { key: 'cancelled', label: '已终止' },
]

function fmtDuration(sec?: number | null): string {
  if (sec == null || !Number.isFinite(sec)) return '-'
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r ? `${m}m${r}s` : `${m}m`
}

function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] || { label: status, cls: 'bg-slate-500/15 text-slate-300' }
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-mono ${meta.cls}`}>
      {meta.label}
    </span>
  )
}

function LogPanel({ id }: { id: number }) {
  const q = useQuery({
    queryKey: ['videoLogs', id],
    queryFn: async () => (await videoApi.logs(id, 300)).data.items,
    refetchInterval: 3000,
  })
  return (
    <div className="border-t border-cyber-border-subtle bg-cyber-bg-base/60 px-4 py-2">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-muted">
        <Terminal className="h-3.5 w-3.5" /> 过程日志
      </div>
      <div className="max-h-56 overflow-auto rounded border border-cyber-border-subtle bg-cyber-bg-base/70 p-2 text-[11px] font-mono leading-relaxed">
        {q.isLoading ? (
          <div className="text-cyber-text-muted">读取中…</div>
        ) : (q.data || []).length === 0 ? (
          <div className="text-cyber-text-muted">暂无日志（任务尚未开始或日志已被清理）</div>
        ) : (
          (q.data || []).map((e, i) => (
            <div key={i} className="whitespace-pre-wrap break-all">
              <span className="text-cyber-text-muted">{e.at}</span>{' '}
              <span className="text-cyber-text-secondary">{e.msg}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

function Row({ v, open, onToggle }: {
  v: VideoAssetRow
  open: boolean
  onToggle: () => void
}) {
  const qc = useQueryClient()
  const cancelM = useMutation({
    mutationFn: () => videoApi.cancel(v.id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['videoTasks'] }),
  })
  const canCancel = v.status !== 'uploaded'

  return (
    <div className="border-b border-cyber-border-subtle/60 last:border-0">
      <div className="flex items-center gap-3 px-4 py-2 text-xs font-mono hover:bg-cyber-bg-hover/40">
        <button type="button" onClick={onToggle} className="text-cyber-text-muted hover:text-cyber-neon-cyan">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </button>
        <span className="w-10 text-cyber-text-muted">#{v.id}</span>
        <span className="w-20 truncate text-cyber-text-secondary" title={v.content_id}>
          {v.story_id ? `帖子 ${v.story_id}` : v.content_id.slice(0, 8)}
        </span>
        <StatusBadge status={v.status} />
        <span className="w-24 text-right text-cyber-text-secondary">
          {v.original_size_bytes ? formatBytes(v.original_size_bytes) : '-'}
        </span>
        <span className="w-20 text-right text-cyber-text-muted">{fmtDuration(v.elapsed_sec)}</span>
        <span className="w-16 text-cyber-text-muted">{v.codec_v || '-'}</span>
        <span className="min-w-0 flex-1 truncate text-rose-300/90" title={v.last_error || ''}>
          {v.status === 'uploaded' ? (
            <span className="text-cyber-text-muted">{v.object_key || ''}</span>
          ) : (v.last_error || '')}
        </span>
        <button
          type="button"
          disabled={!canCancel || cancelM.isPending}
          onClick={() => cancelM.mutate()}
          title={canCancel ? '终止这条转储' : '已完成'}
          className="inline-flex items-center gap-1 rounded border border-cyber-border-subtle px-2 py-0.5 text-[11px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-30"
        >
          {cancelM.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Ban className="h-3 w-3" />}
          终止
        </button>
      </div>
      {open && <LogPanel id={v.id} />}
    </div>
  )
}

export function VideoTasksView() {
  const qc = useQueryClient()
  const [filter, setFilter] = useState<Filter>('')
  const [openId, setOpenId] = useState<number | null>(null)

  const q = useQuery({
    queryKey: ['videoTasks', filter],
    queryFn: async () => (await videoApi.list({
      status: filter || undefined, page_size: 100,
    })).data,
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? []
      const busy = items.some((v) => v.status === 'pending' || v.status === 'uploading')
      return busy ? 3000 : 15000
    },
  })

  const runM = useMutation({
    mutationFn: () => videoApi.run(10),
    onSettled: () => qc.invalidateQueries({ queryKey: ['videoTasks'] }),
  })
  const cancelAllM = useMutation({
    mutationFn: () => videoApi.cancelAll(),
    onSettled: () => qc.invalidateQueries({ queryKey: ['videoTasks'] }),
  })

  const items = q.data?.items ?? []
  const busy = items.filter((v) => v.status === 'pending' || v.status === 'uploading').length

  // 存储连通性诊断：内网 endpoint 不通时上传会先失败再回退公网（慢）
  const [diag, setDiag] = useState<{ ok: boolean; text: string } | null>(null)
  const diagM = useMutation({
    mutationFn: () => videoApi.diag(),
    onSuccess: (res) => {
      const d = res.data
      const lan = d.probes.find((p) => p.name === 'internal')
      const pub = d.probes.find((p) => p.name === 'public')
      const lanText = lan
        ? `${lan.endpoint} ${lan.ok ? `通 ${lan.ms}ms` : `不通(${lan.ms}ms)`}`
        : '未配置'
      const pubText = pub ? `${pub.ok ? `通 ${pub.ms}ms` : `不通(${pub.ms}ms)`}` : '-'
      setDiag({
        ok: !!(lan && lan.ok),
        text: `内网 ${lanText}　|　公网 ${pubText}`,
      })
    },
    onError: (e: any) => setDiag({ ok: false, text: e?.message || '诊断失败' }),
  })

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel">
      <div className="flex flex-shrink-0 flex-wrap items-center gap-2 border-b border-cyber-border-subtle px-4 py-3">
        <Film className="h-4 w-4 text-cyber-neon-cyan" />
        <h2 className="text-sm font-semibold text-cyber-text-primary">视频转储执行记录</h2>
        <span className="text-[11px] font-mono text-cyber-text-muted">
          共 {q.data?.total ?? items.length} 条{busy ? ` · ${busy} 条进行中` : ''}
        </span>

        <div className="ml-auto flex flex-wrap items-center gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.key || 'all'}
              type="button"
              onClick={() => setFilter(f.key)}
              className={`rounded border px-2 py-1 text-[11px] font-mono ${
                filter === f.key
                  ? 'border-cyber-neon-cyan/50 bg-cyber-neon-cyan/10 text-cyber-neon-cyan'
                  : 'border-cyber-border-subtle text-cyber-text-secondary hover:bg-cyber-bg-hover'
              }`}
            >
              {f.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => qc.invalidateQueries({ queryKey: ['videoTasks'] })}
            className="ml-1 inline-flex items-center gap-1 rounded border border-cyber-border-subtle px-2 py-1 text-[11px] font-mono text-cyber-text-secondary hover:bg-cyber-bg-hover"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${q.isFetching ? 'animate-spin' : ''}`} /> 刷新
          </button>
          <button
            type="button"
            disabled={runM.isPending}
            onClick={() => runM.mutate()}
            className="inline-flex items-center gap-1 rounded border border-cyber-neon-cyan/40 px-2 py-1 text-[11px] font-mono text-cyber-neon-cyan hover:bg-cyber-neon-cyan/10 disabled:opacity-50"
          >
            {runM.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            立即处理
          </button>
          <button
            type="button"
            disabled={cancelAllM.isPending || busy === 0}
            onClick={() => {
              if (window.confirm('终止所有排队中/处理中的转储？')) cancelAllM.mutate()
            }}
            className="inline-flex items-center gap-1 rounded border border-rose-500/40 px-2 py-1 text-[11px] font-mono text-rose-300 hover:bg-rose-500/10 disabled:opacity-40"
          >
            <Ban className="h-3.5 w-3.5" /> 全部终止
          </button>
          <button
            type="button"
            disabled={diagM.isPending}
            onClick={() => diagM.mutate()}
            title="探测容器到内网/公网对象存储的连通性与延迟"
            className="inline-flex items-center gap-1 rounded border border-cyber-border-subtle px-2 py-1 text-[11px] font-mono text-cyber-text-secondary hover:bg-cyber-bg-hover disabled:opacity-50"
          >
            {diagM.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wifi className="h-3.5 w-3.5" />}
            诊断存储
          </button>
          {diag && (
            <span className={`font-mono text-[11px] ${diag.ok ? 'text-emerald-400' : 'text-amber-400'}`}>
              {diag.text}
            </span>
          )}
        </div>
      </div>

      <div className="flex flex-shrink-0 items-center gap-3 border-b border-cyber-border-subtle/60 bg-cyber-bg-base/40 px-4 py-1.5 text-[11px] font-mono text-cyber-text-muted">
        <span className="w-6" />
        <span className="w-10">ID</span>
        <span className="w-20">来源</span>
        <span className="w-16">状态</span>
        <span className="w-24 text-right">原始大小</span>
        <span className="w-20 text-right">耗时</span>
        <span className="w-16">编码</span>
        <span className="flex-1">结果 / 错误</span>
        <span className="w-14 text-right">操作</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {q.isLoading ? (
          <div className="p-6 text-center text-xs font-mono text-cyber-text-muted">读取中…</div>
        ) : items.length === 0 ? (
          <div className="p-6 text-center text-xs font-mono text-cyber-text-muted">没有记录</div>
        ) : (
          items.map((v) => (
            <Row
              key={v.id}
              v={v}
              open={openId === v.id}
              onToggle={() => setOpenId(openId === v.id ? null : v.id)}
            />
          ))
        )}
      </div>
    </div>
  )
}
