import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  CalendarClock, Loader2, Play, Plus, RefreshCw, Square, Trash2, X,
  CheckCircle2, XCircle, Clock, MinusCircle, TimerReset, ListTree, Filter,
  UserRound, FileText, ScanFace,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Badge } from '@/components/ui/badge'
import {
  useSchedulerTasks, useSchedulerStatus, useSchedulerRuns, useSchedulerDimensions,
  useCreateTask, useUpdateTask, useDeleteTask, useToggleTask, useRunTask,
  useStopScheduler,
} from '@/hooks/useScheduler'
import type { ScheduledTask, ScheduledTaskPayload, TaskType, TaskDimension,
              RunStatus, RunStatsItem } from '@/types/scheduler'

const PLATFORMS = [
  { value: 'xhs', label: '小红书' },
  { value: 'dy', label: '抖音' },
  { value: 'ks', label: '快手' },
  { value: 'bili', label: 'B站' },
  { value: 'wb', label: '微博' },
  { value: 'tieba', label: '贴吧' },
  { value: 'zhihu', label: '知乎' },
]

const SAVE_OPTIONS = [
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'jsonl', label: 'JSONL 文件' },
  { value: 'json', label: 'JSON 文件' },
  { value: 'csv', label: 'CSV 文件' },
  { value: 'sqlite', label: 'SQLite' },
]

/** 补齐率百分比：0~1 -> 92% */
const fmtPct = (v: number) => `${Math.round((v || 0) * 100)}%`

/** 明细行数上限：一轮可能上百篇，全部渲染会把面板撑爆 */
const STAT_ROW_LIMIT = 50

function StatRow({ item }: { item: RunStatsItem }) {
  const b = item.before
  const a = item.after
  return (
    <div className="flex items-center gap-1.5 text-[9px] font-mono leading-4">
      <span className="text-cyber-text-muted">{item.content_id.slice(0, 8)}</span>
      <span className="text-cyber-text-secondary shrink-0">
        {fmtPct(b.completeness)}→{fmtPct(a.completeness)}
      </span>
      <span className="text-cyber-text-muted shrink-0">
        {b.fetched}→{a.fetched}/{a.declared}
      </span>
      {item.gained > 0 && (
        <span className="text-cyber-neon-green shrink-0">+{item.gained}</span>
      )}
      <span className="text-cyber-text-muted truncate max-w-[110px]" title={item.title}>
        {item.title}
      </span>
    </div>
  )
}

const EMPTY_FORM: ScheduledTaskPayload = {
  name: '',
  platform: 'xhs',
  task_type: 'profile',
  target: '',
  mode: 'incremental',
  schedule_type: 'interval',
  interval_sec: 3600,
  cron_expr: '',
  max_concurrency: 1,
  timeout_sec: 1800,
  retry_count: 1,
  retry_delay_sec: 60,
  rate_interval_sec: 3,
  options: {
    login_type: 'cookie',
    enable_comments: true,
    enable_sub_comments: false,
    enable_media: false,
    save_option: 'postgres',
    headless: true,
    cookies: '',
    max_comments_count: null,
    behaviors: ['likes', 'collects'],
    account_id: null,
    account_name: null,
    max_notes: null,
  },
  enabled: true,
}

/** 维度 → 展示元信息（徽标 / 筛选 / 图标） */
const DIMENSION_META: Record<TaskDimension, { label: string; icon: typeof UserRound }> = {
  author: { label: '关注用户', icon: UserRound },
  note: { label: '关注帖子', icon: FileText },
  account: { label: '登录用户', icon: ScanFace },
}

function dimensionOf(task: ScheduledTask): TaskDimension {
  if (task.dimension) return task.dimension
  return task.task_type === 'profile' ? 'author'
    : task.task_type === 'account' ? 'account' : 'note'
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function runStatusMeta(status: RunStatus, t: (k: string) => string) {
  switch (status) {
    case 'success': return { label: t('runStatus.success'), cls: 'bg-cyber-neon-green/15 text-cyber-neon-green border-cyber-neon-green/30', Icon: CheckCircle2 }
    case 'failed': return { label: t('runStatus.failed'), cls: 'bg-cyber-neon-pink/15 text-cyber-neon-pink border-cyber-neon-pink/30', Icon: XCircle }
    case 'timeout': return { label: t('runStatus.timeout'), cls: 'bg-cyber-neon-orange/15 text-cyber-neon-orange border-cyber-neon-orange/30', Icon: Clock }
    case 'skipped': return { label: t('runStatus.skipped'), cls: 'bg-cyber-text-muted/15 text-cyber-text-muted border-cyber-border-subtle', Icon: MinusCircle }
    default: return { label: t('runStatus.running'), cls: 'bg-cyber-neon-cyan/15 text-cyber-neon-cyan border-cyber-neon-cyan/30', Icon: Loader2 }
  }
}

export function ScheduledTasksView() {
  const { t } = useTranslation('scheduler')
  const tasksQ = useSchedulerTasks()
  const statusQ = useSchedulerStatus()
  const runsQ = useSchedulerRuns(60)
  const dimensionsQ = useSchedulerDimensions()

  const createM = useCreateTask()
  const updateM = useUpdateTask()
  const deleteM = useDeleteTask()
  const toggleM = useToggleTask()
  const runM = useRunTask()
  const stopM = useStopScheduler()

  const [editing, setEditing] = useState<ScheduledTask | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<ScheduledTaskPayload>(EMPTY_FORM)
  const [expandedRun, setExpandedRun] = useState<number | null>(null)
  // 任务列表维度筛选：全部 / 关注用户 / 关注帖子 / 登录用户
  const [dimFilter, setDimFilter] = useState<'all' | TaskDimension>('all')

  const tasks = tasksQ.data || []
  const runs = runsQ.data || []
  const status = statusQ.data
  const dimensions = dimensionsQ.data
  const accounts = dimensions?.accounts || []
  const busy = createM.isPending || updateM.isPending || deleteM.isPending
    || toggleM.isPending || runM.isPending || stopM.isPending

  // 拆分部署后调度器跑在独立 worker 进程里，本进程的 started 永远是 false，
  // 所以「运行中」要同时看 worker_alive，否则顶部会显示"未启动"但任务明明在跑
  const schedulerLive = Boolean(status?.started || status?.worker_alive)
  const hasRunning = useMemo(
    () => (status?.running_tasks || 0) > 0 || tasks.some((x) => x.status === 'running'),
    [status?.running_tasks, tasks],
  )

  const filteredTasks = useMemo(
    () => (dimFilter === 'all' ? tasks : tasks.filter((x) => dimensionOf(x) === dimFilter)),
    [tasks, dimFilter]
  )

  // 各维度任务数（筛选栏上用）
  const dimCounts = useMemo(() => {
    const counts: Record<string, number> = { all: tasks.length, author: 0, note: 0, account: 0 }
    for (const x of tasks) counts[dimensionOf(x)] = (counts[dimensionOf(x)] || 0) + 1
    return counts
  }, [tasks])

  const openCreate = () => {
    setEditing(null)
    setForm(EMPTY_FORM)
    setShowForm(true)
  }

  const openEdit = (task: ScheduledTask) => {
    setEditing(task)
    setForm({
      name: task.name,
      platform: task.platform,
      task_type: task.task_type,
      target: task.target,
      mode: task.mode,
      schedule_type: task.schedule_type,
      interval_sec: task.interval_sec ?? 3600,
      cron_expr: task.cron_expr ?? '',
      max_concurrency: task.max_concurrency,
      timeout_sec: task.timeout_sec,
      retry_count: task.retry_count,
      retry_delay_sec: task.retry_delay_sec,
      rate_interval_sec: task.rate_interval_sec,
      options: { ...EMPTY_FORM.options, ...(task.options || {}) },
      enabled: task.enabled,
    })
    setShowForm(true)
  }

  const submit = () => {
    if (!form.name.trim()) return
    let payload = form
    if (form.task_type === 'account') {
      // 账号维度：target = 账号池 ID，账号名一并写进 options 便于列表展示
      const accountId = form.options.account_id
      if (!accountId) return
      const account = accounts.find((a) => a.id === accountId)
      payload = {
        ...form,
        target: String(accountId),
        options: { ...form.options, account_name: account?.account_name ?? null },
      }
    } else if (!form.target.trim()) {
      return
    }
    if (editing) {
      updateM.mutate({ id: editing.id, payload }, { onSuccess: () => setShowForm(false) })
    } else {
      createM.mutate(payload, { onSuccess: () => setShowForm(false) })
    }
  }

  const setOpt = (key: string, value: unknown) =>
    setForm((f) => ({ ...f, options: { ...f.options, [key]: value } as ScheduledTaskPayload['options'] }))

  const targetPlaceholder = useMemo(
    () => (form.task_type === 'profile' ? t('field.targetProfileHint') : t('field.targetNoteHint')),
    [form.task_type, t]
  )

  return (
    <div className="flex-1 flex flex-col gap-4 overflow-hidden min-h-0 animate-slide-up">
      {/* 头部：标题 + 调度器状态 */}
      <div className="flex-shrink-0 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-md glass-panel border border-cyber-border-subtle flex items-center justify-center">
            <CalendarClock className="h-4.5 w-4.5 text-cyber-neon-purple" />
          </div>
          <div>
            <h1 className="text-sm font-mono font-bold text-cyber-text-primary tracking-wide">{t('title')}</h1>
            <p className="text-[10px] text-cyber-text-muted">{t('subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {status && (
            <>
              <Badge
                variant="outline"
                title={status.mode === 'worker'
                  ? `执行器独立运行${status.worker?.pid ? `（pid ${status.worker.pid}）` : ''}`
                  : undefined}
                className={`text-[10px] font-mono ${schedulerLive ? 'border-cyber-neon-green/40 text-cyber-neon-green' : 'border-cyber-neon-pink/40 text-cyber-neon-pink'}`}>
                {schedulerLive ? t('statusRunning') : t('statusStopped')}
              </Badge>
              {(status.queued_runs || 0) > 0 && (
                <Badge variant="outline" className="text-[10px] font-mono border-cyber-neon-orange/40 text-cyber-neon-orange">
                  {t('queuedRuns', { count: status.queued_runs })}
                </Badge>
              )}
              <Badge variant="outline" className="text-[10px] font-mono border-cyber-border-subtle text-cyber-text-secondary">
                {t('enabledTasks', { count: status.enabled_tasks })}
              </Badge>
              {status.running_tasks > 0 && (
                <Badge variant="outline" className="text-[10px] font-mono border-cyber-neon-cyan/40 text-cyber-neon-cyan">
                  <Loader2 className="w-2.5 h-2.5 animate-spin mr-1" />
                  {t('runningTasks', { count: status.running_tasks })}
                </Badge>
              )}
              <Badge variant="outline" className="text-[10px] font-mono border-cyber-border-subtle text-cyber-text-muted">
                {t('maxConcurrency', { count: status.max_concurrency })} · {t('tickEvery', { sec: status.tick_sec })}
              </Badge>
            </>
          )}
          {/* 不用 hasRunning 禁用：worker 模式下状态可能滞后（正是本次要修的
              「状态不同步」），一旦判错按钮就点不了。后端 /scheduler/stop 是幂等
              的，此刻没任务在跑也返回 200 + stopped:false，点了只是提示一句，
              没有副作用。hasRunning 仅用于提示文案。 */}
          <Button
            onClick={() => stopM.mutate()}
            variant="outline" size="sm"
            disabled={stopM.isPending}
            title={hasRunning
              ? '停止正在执行的抓取（含执行器里的定时任务）'
              : '停止当前抓取；若此刻没有任务在执行会提示「当前没有正在执行的抓取」'}
            className="h-8 border-cyber-border-DEFAULT font-mono text-[11px] hover:border-cyber-neon-pink/50 hover:text-cyber-neon-pink disabled:opacity-40"
          >
            {stopM.isPending
              ? <Loader2 className="w-3 h-3 animate-spin" />
              : <Square className="w-3 h-3" />}
            {t('actions.stop')}
          </Button>
          <Button
            onClick={() => { tasksQ.refetch(); runsQ.refetch(); statusQ.refetch() }}
            variant="outline" size="sm"
            className="h-8 border-cyber-border-DEFAULT font-mono text-[11px] hover:border-cyber-neon-cyan/50"
          >
            <RefreshCw className="w-3 h-3" />
            {t('actions.refresh')}
          </Button>
          <Button onClick={openCreate} size="sm" className="h-8 bg-cyber-neon-purple text-white font-mono text-[11px] font-bold hover:bg-cyber-neon-purple/90">
            <Plus className="w-3.5 h-3.5" />
            {t('create')}
          </Button>
        </div>
      </div>

      {/* 任务表单（新建 / 编辑） */}
      {showForm && (
        <div className="flex-shrink-0 rounded-lg glass-panel float-panel p-4 space-y-3 max-h-[46vh] overflow-y-auto">
          <div className="flex items-center justify-between">
            <span className="text-xs font-mono font-semibold text-cyber-text-primary">
              {editing ? t('edit') : t('create')}
            </span>
            <button onClick={() => setShowForm(false)} className="text-cyber-text-muted hover:text-cyber-text-primary">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.name')}</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="h-8 text-[11px] font-mono" placeholder="每日刷新某作者主页" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.platform')}</Label>
              <select value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}
                className="w-full h-8 rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-2 text-[11px] font-mono text-cyber-text-primary">
                {PLATFORMS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </div>
          </div>

          <div className="space-y-1">
            <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.taskType')}</Label>
            <div className="grid grid-cols-3 gap-2">
              {(['profile', 'note', 'account'] as TaskType[]).map((tt) => {
                const dim: TaskDimension = tt === 'profile' ? 'author' : tt === 'account' ? 'account' : 'note'
                const Icon = DIMENSION_META[dim].icon
                return (
                  <button key={tt} onClick={() => setForm({ ...form, task_type: tt })}
                    className={`rounded-md border px-2.5 py-1.5 text-left text-[11px] font-mono transition-all ${
                      form.task_type === tt
                        ? 'border-cyber-neon-purple/60 bg-cyber-neon-purple/10 text-cyber-text-primary'
                        : 'border-cyber-border-subtle text-cyber-text-muted hover:border-cyber-border-DEFAULT'
                    }`}>
                    <span className="flex items-center gap-1">
                      <Icon className="w-3 h-3" />
                      {tt === 'profile' ? t('field.typeProfile') : tt === 'note' ? t('field.typeNote') : t('field.typeAccount')}
                    </span>
                  </button>
                )
              })}
            </div>
            {form.task_type === 'account' && (
              <p className="text-[10px] text-cyber-text-muted">{t('field.typeAccountHint')}</p>
            )}
          </div>

          {form.task_type === 'account' ? (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.account')}</Label>
              {accounts.length > 0 ? (
                <select
                  value={form.options.account_id ?? ''}
                  onChange={(e) => {
                    const id = Number(e.target.value) || null
                    const account = accounts.find((a) => a.id === id)
                    setForm({
                      ...form,
                      target: id ? String(id) : '',
                      options: { ...form.options, account_id: id, account_name: account?.account_name ?? null },
                    })
                  }}
                  className="w-full h-8 rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-2 text-[11px] font-mono text-cyber-text-primary">
                  <option value="">{t('field.accountPlaceholder')}</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.account_name}（#{a.id}{a.status !== 'active' ? ` · ${a.status}` : ''}）
                    </option>
                  ))}
                </select>
              ) : (
                <p className="text-[10px] text-cyber-neon-orange py-1">{t('field.noAccounts')}</p>
              )}
            </div>
          ) : (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.target')}</Label>
              <Input value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })}
                className="h-8 text-[11px] font-mono" placeholder={targetPlaceholder} />
            </div>
          )}

          {form.task_type === 'account' && (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.behaviors')}</Label>
              <div className="flex items-center gap-4">
                {(['likes', 'collects'] as const).map((b) => (
                  <label key={b}
                    className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                    <Checkbox
                      checked={(form.options.behaviors || []).includes(b)}
                      onCheckedChange={(v) => {
                        const cur = form.options.behaviors || []
                        const next = v ? [...new Set([...cur, b])] : cur.filter((x) => x !== b)
                        setForm({ ...form, options: { ...form.options, behaviors: next } })
                      }} />
                    {b === 'likes' ? t('field.behaviorLikes') : t('field.behaviorCollects')}
                  </label>
                ))}
              </div>
              <p className="text-[10px] text-cyber-text-muted">{t('field.behaviorsHint')}</p>
            </div>
          )}

          {form.task_type === 'account' && (
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.mode')}</Label>
                <div className="grid grid-cols-2 gap-2">
                  {(['incremental', 'full'] as const).map((m) => (
                    <button key={m} onClick={() => setForm({ ...form, mode: m })}
                      className={`rounded-md border px-2.5 py-1.5 text-left text-[11px] font-mono transition-all ${
                        form.mode === m
                          ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-text-primary'
                          : 'border-cyber-border-subtle text-cyber-text-muted hover:border-cyber-border-DEFAULT'
                      }`}>
                      {m === 'incremental' ? t('field.modeIncrementalAccount') : t('field.modeFullAccount')}
                    </button>
                  ))}
                </div>
              </div>
              <div className="space-y-1">
                <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.maxNotes')}</Label>
                <Input type="number" min={1} value={form.options.max_notes ?? ''}
                  placeholder="100"
                  onChange={(e) => setForm({
                    ...form,
                    options: {
                      ...form.options,
                      max_notes: e.target.value ? Number(e.target.value) : null,
                    },
                  })}
                  className="h-8 text-[11px] font-mono" />
              </div>
              <p className="col-span-2 text-[10px] text-cyber-text-muted">{t('field.maxNotesHint')}</p>
            </div>
          )}

          {form.task_type === 'profile' && (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.mode')}</Label>
              <div className="grid grid-cols-2 gap-2">
                {(['incremental', 'full'] as const).map((m) => (
                  <button key={m} onClick={() => setForm({ ...form, mode: m })}
                    className={`rounded-md border px-2.5 py-1.5 text-left text-[11px] font-mono transition-all ${
                      form.mode === m
                        ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/10 text-cyber-text-primary'
                        : 'border-cyber-border-subtle text-cyber-text-muted hover:border-cyber-border-DEFAULT'
                    }`}>
                    {m === 'incremental' ? t('field.modeIncremental') : t('field.modeFull')}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="space-y-1">
            <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.scheduleType')}</Label>
            <div className="grid grid-cols-2 gap-2">
              {(['interval', 'cron'] as const).map((s) => (
                <button key={s} onClick={() => setForm({ ...form, schedule_type: s })}
                  className={`rounded-md border px-2.5 py-1.5 text-[11px] font-mono transition-all ${
                    form.schedule_type === s
                      ? 'border-cyber-neon-purple/60 bg-cyber-neon-purple/10 text-cyber-text-primary'
                      : 'border-cyber-border-subtle text-cyber-text-muted hover:border-cyber-border-DEFAULT'
                  }`}>
                  {s === 'interval' ? t('field.interval') : t('field.cron')}
                </button>
              ))}
            </div>
          </div>

          {form.schedule_type === 'interval' ? (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.intervalSec')}</Label>
              <Input type="number" min={60} value={form.interval_sec ?? 3600}
                onChange={(e) => setForm({ ...form, interval_sec: Number(e.target.value) })}
                className="h-8 text-[11px] font-mono" />
            </div>
          ) : (
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.cronExpr')}</Label>
              <Input value={form.cron_expr ?? ''} onChange={(e) => setForm({ ...form, cron_expr: e.target.value })}
                className="h-8 text-[11px] font-mono" placeholder="0 */6 * * *" />
              <p className="text-[10px] text-cyber-text-muted">{t('field.cronHint')}</p>
            </div>
          )}

          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.maxConcurrency')}</Label>
              <Input type="number" min={1} max={10} value={form.max_concurrency}
                onChange={(e) => setForm({ ...form, max_concurrency: Number(e.target.value) })} className="h-8 text-[11px] font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.timeout')}</Label>
              <Input type="number" min={60} value={form.timeout_sec}
                onChange={(e) => setForm({ ...form, timeout_sec: Number(e.target.value) })} className="h-8 text-[11px] font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.retryCount')}</Label>
              <Input type="number" min={0} max={5} value={form.retry_count}
                onChange={(e) => setForm({ ...form, retry_count: Number(e.target.value) })} className="h-8 text-[11px] font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.retryDelay')}</Label>
              <Input type="number" min={0} value={form.retry_delay_sec}
                onChange={(e) => setForm({ ...form, retry_delay_sec: Number(e.target.value) })} className="h-8 text-[11px] font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.rateInterval')}</Label>
              <Input type="number" min={0} max={60} value={form.rate_interval_sec}
                onChange={(e) => setForm({ ...form, rate_interval_sec: Number(e.target.value) })} className="h-8 text-[11px] font-mono" />
            </div>
          </div>

          {/* 抓取选项 */}
          <div className="space-y-1">
            <Label className="text-[11px] text-cyber-text-secondary font-mono">{t('field.options')}</Label>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <div className="space-y-1">
                <span className="text-[10px] text-cyber-text-muted">{t('field.saveOption')}</span>
                <select value={form.options.save_option} onChange={(e) => setOpt('save_option', e.target.value)}
                  className="w-full h-8 rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-2 text-[11px] font-mono text-cyber-text-primary">
                  {SAVE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
              <div className="space-y-1">
                <span className="text-[10px] text-cyber-text-muted">{t('field.loginType')}</span>
                <select value={form.options.login_type} onChange={(e) => setOpt('login_type', e.target.value)}
                  className="w-full h-8 rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-2 text-[11px] font-mono text-cyber-text-primary">
                  <option value="cookie">Cookie</option>
                  <option value="qrcode">扫码</option>
                </select>
              </div>
              <div className="flex items-end gap-3 pb-1">
                <label className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                  <Checkbox checked={form.options.enable_comments} onCheckedChange={(v) => setOpt('enable_comments', !!v)} />
                  {t('field.enableComments')}
                </label>
              </div>
              <div className="flex items-end gap-3 pb-1">
                <label className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                  <Checkbox checked={form.options.headless} onCheckedChange={(v) => setOpt('headless', !!v)} />
                  {t('field.headless')}
                </label>
              </div>
              <div className="flex items-end gap-3 pb-1">
                <label className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                  <Checkbox checked={form.options.enable_sub_comments} onCheckedChange={(v) => setOpt('enable_sub_comments', !!v)} />
                  {t('field.enableSubComments')}
                </label>
              </div>
              <div className="flex items-end gap-3 pb-1">
                <label className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                  <Checkbox checked={form.options.enable_media} onCheckedChange={(v) => setOpt('enable_media', !!v)} />
                  {t('field.enableMedia')}
                </label>
              </div>
              <div className="flex items-end gap-3 pb-1">
                <label className="flex items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary cursor-pointer">
                  <Checkbox checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: !!v })} />
                  {t('field.enabled')}
                </label>
              </div>
            </div>
            <Input value={form.options.cookies} onChange={(e) => setOpt('cookies', e.target.value)}
              className="h-8 text-[11px] font-mono mt-2" placeholder={t('field.cookies')} />
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={() => setShowForm(false)}
              className="h-8 border-cyber-border-DEFAULT font-mono text-[11px]">
              {t('actions.cancel')}
            </Button>
            <Button size="sm" onClick={submit}
              disabled={busy || !form.name.trim()
                || (form.task_type === 'account' ? !form.options.account_id : !form.target.trim())}
              className="h-8 bg-cyber-neon-purple text-white font-mono text-[11px] font-bold hover:bg-cyber-neon-purple/90">
              {(createM.isPending || updateM.isPending) && <Loader2 className="w-3 h-3 animate-spin" />}
              {editing ? t('actions.save') : t('actions.create')}
            </Button>
          </div>
        </div>
      )}

      {/* 主体：任务列表 + 执行记录 */}
      <div className="flex-1 grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-4 overflow-hidden min-h-0">
        {/* 任务列表 */}
        <div className="flex flex-col gap-2 overflow-y-auto min-h-0 pr-1">
          {/* 维度筛选栏 */}
          <div className="flex-shrink-0 flex items-center gap-2 flex-wrap">
            <span className="flex items-center gap-1 text-[10px] font-mono text-cyber-text-muted">
              <Filter className="w-3 h-3" />
              {t('filter.label')}
            </span>
            {([
              ['all', t('filter.all')],
              ['author', DIMENSION_META.author.label],
              ['note', DIMENSION_META.note.label],
              ['account', DIMENSION_META.account.label],
            ] as Array<[('all' | TaskDimension), string]>).map(([key, label]) => (
              <button key={key} onClick={() => setDimFilter(key)}
                className={`rounded-full border px-2.5 py-0.5 text-[10px] font-mono transition-all ${
                  dimFilter === key
                    ? 'border-cyber-neon-purple/60 bg-cyber-neon-purple/10 text-cyber-text-primary'
                    : 'border-cyber-border-subtle text-cyber-text-muted hover:border-cyber-border-DEFAULT'
                }`}>
                {label}
                <span className="ml-1 text-cyber-text-muted">{dimCounts[key] || 0}</span>
              </button>
            ))}
          </div>
          {tasksQ.isLoading && (
            <div className="flex items-center justify-center py-10 text-cyber-text-muted">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}
          {!tasksQ.isLoading && filteredTasks.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <ListTree className="w-8 h-8 text-cyber-text-muted mb-3" />
              <p className="text-xs font-mono text-cyber-text-secondary">{t('empty')}</p>
              <p className="text-[10px] text-cyber-text-muted mt-1 max-w-xs">{t('emptyHint')}</p>
            </div>
          )}
          {filteredTasks.map((task) => {
            const meta = task.last_status ? runStatusMeta(task.last_status as RunStatus, t) : null
            const running = task.status === 'running'
            const dim = dimensionOf(task)
            const DimIcon = DIMENSION_META[dim].icon
            return (
              <div key={task.id}
                className={`rounded-lg glass-panel border p-3 transition-all ${running ? 'border-cyber-neon-cyan/40' : 'border-cyber-border-subtle hover:border-cyber-border-DEFAULT'}`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-mono font-semibold text-cyber-text-primary truncate">{task.name}</span>
                      <Badge variant="outline" className="text-[9px] font-mono border-cyber-neon-purple/40 text-cyber-neon-purple">
                        <DimIcon className="w-2.5 h-2.5 mr-0.5" />{DIMENSION_META[dim].label}
                      </Badge>
                      {dim === 'author' && task.mode === 'incremental' && (
                        <Badge variant="outline" className="text-[9px] font-mono border-cyber-border-subtle text-cyber-text-muted">增量</Badge>
                      )}
                      {dim === 'account' && (task.options?.behaviors || []).length > 0 && (
                        <Badge variant="outline" className="text-[9px] font-mono border-cyber-border-subtle text-cyber-text-muted">
                          {(task.options?.behaviors || []).includes('likes') ? '点赞' : ''}
                          {(task.options?.behaviors || []).includes('collects') ? ' 收藏' : ''}
                        </Badge>
                      )}
                      {!task.enabled && (
                        <Badge variant="outline" className="text-[9px] font-mono border-cyber-border-subtle text-cyber-text-muted">已停用</Badge>
                      )}
                      {running && (
                        <Badge variant="outline" className="text-[9px] font-mono border-cyber-neon-cyan/40 text-cyber-neon-cyan">
                          <Loader2 className="w-2 h-2 animate-spin mr-0.5" />执行中
                        </Badge>
                      )}
                    </div>
                    <p className="text-[10px] text-cyber-text-muted mt-1 truncate" title={task.target}>
                      {dim === 'account'
                        ? `${t('field.account')}: ${task.options?.account_name || `#${task.target}`}`
                        : task.target}
                    </p>
                    <div className="flex items-center gap-3 mt-1.5 text-[10px] font-mono text-cyber-text-muted flex-wrap">
                      <span className="flex items-center gap-1">
                        {task.schedule_type === 'cron'
                          ? <><TimerReset className="w-2.5 h-2.5" />{task.cron_expr}</>
                          : <><RefreshCw className="w-2.5 h-2.5" />{Math.round((task.interval_sec || 0) / 60)} 分钟</>}
                      </span>
                      <span>{t('next')}: {fmtTime(task.next_run_at)}</span>
                      <span>{t('last')}: {fmtTime(task.last_run_at)}</span>
                      {meta && (
                        <span className="flex items-center gap-1">
                          <meta.Icon className={`w-2.5 h-2.5 ${running ? 'animate-spin' : ''}`} />
                          {meta.label}
                        </span>
                      )}
                      <span>{t('runCount', { count: task.run_count })}</span>
                    </div>
                    {task.last_error && (
                      <p className="text-[10px] text-cyber-neon-pink mt-1 line-clamp-2">{task.last_error}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <Button variant="outline" size="sm" disabled={busy || running}
                      onClick={() => runM.mutate(task.id)}
                      className="h-7 px-2 border-cyber-border-DEFAULT font-mono text-[10px] hover:border-cyber-neon-green/50 hover:text-cyber-neon-green"
                      title={t('actions.run')}>
                      <Play className="w-3 h-3" />
                    </Button>
                    <Button variant="outline" size="sm" disabled={busy || running}
                      onClick={() => toggleM.mutate({ id: task.id, enabled: !task.enabled })}
                      className="h-7 px-2 border-cyber-border-DEFAULT font-mono text-[10px] hover:border-cyber-neon-orange/50 hover:text-cyber-neon-orange"
                      title={task.enabled ? t('actions.toggleOff') : t('actions.toggleOn')}>
                      {task.enabled ? <Square className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                    </Button>
                    <Button variant="outline" size="sm" disabled={busy}
                      onClick={() => openEdit(task)}
                      className="h-7 px-2 border-cyber-border-DEFAULT font-mono text-[10px] hover:border-cyber-neon-cyan/50 hover:text-cyber-neon-cyan">
                      {t('actions.edit')}
                    </Button>
                    <Button variant="outline" size="sm" disabled={busy}
                      onClick={() => { if (confirm(t('msg.confirmDelete', { name: task.name }))) deleteM.mutate(task.id) }}
                      className="h-7 px-2 border-cyber-border-DEFAULT font-mono text-[10px] hover:border-cyber-neon-pink/50 hover:text-cyber-neon-pink">
                      <Trash2 className="w-3 h-3" />
                    </Button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* 执行记录 */}
        <div className="flex flex-col rounded-lg glass-panel border border-cyber-border-subtle overflow-hidden min-h-0">
          <div className="px-3 py-2 border-b border-cyber-border-subtle/50 flex items-center gap-2 bg-cyber-bg-tertiary/30 flex-shrink-0">
            <ListTree className="h-3.5 w-3.5 text-cyber-neon-purple" />
            <span className="text-xs font-mono font-semibold text-cyber-text-primary">{t('runs.title')}</span>
            {status?.live_stats && (
              <span
                className="ml-auto flex items-center gap-1.5 text-[10px] font-mono text-cyber-text-muted"
                title={t('runs.liveStatsHint')}
              >
                <span>{t('runs.liveFetched')} <b className="text-cyber-neon-cyan">{status.live_stats.fetched}</b></span>
                <span className="text-cyber-border-subtle">/</span>
                <span>{t('runs.liveSaved')} <b className="text-cyber-neon-green">{status.live_stats.saved}</b></span>
              </span>
            )}
          </div>
          <div className="flex-1 overflow-y-auto min-h-0 divide-y divide-cyber-border-subtle/30">
            {runs.length === 0 && (
              <p className="text-[10px] text-cyber-text-muted text-center py-8">{t('runs.empty')}</p>
            )}
            {runs.map((run) => {
              const meta = runStatusMeta(run.status, t)
              const open = expandedRun === run.id
              const task = tasks.find((x) => x.id === run.task_id)
              return (
                <div key={run.id} className="px-3 py-2">
                  <button onClick={() => setExpandedRun(open ? null : run.id)}
                    className="w-full text-left space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-1.5 text-[11px] font-mono">
                        <meta.Icon className={`w-3 h-3 ${run.status === 'running' ? 'animate-spin' : ''}`} />
                        <span className={meta.cls.split(' ')[1]}>{meta.label}</span>
                        <span className="text-cyber-text-muted">
                          {run.trigger_type === 'schedule' ? t('trigger.schedule') : t('trigger.manual')}
                        </span>
                      </span>
                      <span className="text-[10px] font-mono text-cyber-text-muted">{fmtTime(run.started_at)}</span>
                    </div>
                    <div className="flex items-center gap-2 text-[10px] font-mono text-cyber-text-muted flex-wrap">
                      <span className="text-cyber-text-secondary truncate max-w-[140px]" title={task?.name}>{task?.name || `#${run.task_id}`}</span>
                      {run.status !== 'running' && (
                        <>
                          <span className="text-cyber-neon-green">{t('runs.newCount', { count: run.new_count })}</span>
                          <span className="text-cyber-neon-cyan">{t('runs.updatedCount', { count: run.updated_count })}</span>
                          <span>{t('runs.skippedCount', { count: run.skipped_count })}</span>
                          <span>{t('runs.duration', { sec: ((run.duration_ms || 0) / 1000).toFixed(1) })}</span>
                          {run.attempt_count > 1 && <span>{t('runs.attempts', { count: run.attempt_count })}</span>}
                        </>
                      )}
                    </div>
                    {run.status === 'running' && status?.live_stats && (
                      <p className="text-[10px] font-mono text-cyber-neon-cyan">
                        {t('runs.liveProgress', {
                          fetched: status.live_stats.fetched,
                          saved: status.live_stats.saved,
                        })}
                      </p>
                    )}
                    {run.error_message && (
                      <p className="text-[10px] text-cyber-neon-pink line-clamp-2">{run.error_message}</p>
                    )}
                  </button>
                  {open && run.stats_json && (() => {
                    const st = run.stats_json
                    const all = [...st.added, ...st.updated]
                    const gained = all.reduce((s, i) => s + (i.gained || 0), 0)
                    const page = (list: RunStatsItem[]) => list.slice(0, STAT_ROW_LIMIT)
                    return (
                      <div className="mt-1.5 space-y-1 rounded bg-cyber-bg-primary/40 p-2">
                        <div className="flex items-center gap-2 text-[9px] font-mono flex-wrap">
                          <span className="text-cyber-neon-purple font-semibold">{t('runs.statsTitle')}</span>
                          <span className="text-cyber-neon-green">{t('runs.statsGained', { count: gained })}</span>
                        </div>
                        {all.length === 0 ? (
                          <p className="text-[9px] font-mono text-cyber-text-muted">{t('runs.statsEmpty')}</p>
                        ) : (
                          <>
                            {st.added.length > 0 && (
                              <div>
                                <p className="text-[9px] font-mono text-cyber-neon-green">
                                  {t('runs.statsAdded', { count: st.added.length })}
                                </p>
                                {page(st.added).map((it) => (
                                  <StatRow key={it.content_id} item={it} />
                                ))}
                                {st.added.length > STAT_ROW_LIMIT && (
                                  <p className="text-[9px] font-mono text-cyber-text-muted">
                                    …+{st.added.length - STAT_ROW_LIMIT}
                                  </p>
                                )}
                              </div>
                            )}
                            {st.updated.length > 0 && (
                              <div>
                                <p className="text-[9px] font-mono text-cyber-neon-cyan">
                                  {t('runs.statsUpdated', { count: st.updated.length })}
                                </p>
                                {page(st.updated).map((it) => (
                                  <StatRow key={it.content_id} item={it} />
                                ))}
                                {st.updated.length > STAT_ROW_LIMIT && (
                                  <p className="text-[9px] font-mono text-cyber-text-muted">
                                    …+{st.updated.length - STAT_ROW_LIMIT}
                                  </p>
                                )}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )
                  })()}
                  {open && (run.log_tail || []).length > 0 && (
                    <pre className="mt-1.5 max-h-40 overflow-auto rounded bg-cyber-bg-primary/60 p-2 text-[9px] leading-4 font-mono text-cyber-text-muted whitespace-pre-wrap">
                      {(run.log_tail || []).join('\n')}
                    </pre>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
