// 定时拉取任务相关类型（与后端 api/schemas/scheduler.py 对应）
import type { LiveStats } from './crawler'

export type TaskType = 'profile' | 'note' | 'account'
/** 任务维度：author=关注的用户 / note=关注的帖子 / account=扫码登录用户 */
export type TaskDimension = 'author' | 'note' | 'account'
/** 账号维度可抓取的行为来源（网页端无「评论过的帖」列表接口） */
export type BehaviorSource = 'likes' | 'collects'
export type TaskMode = 'full' | 'incremental'
export type ScheduleType = 'interval' | 'cron'
export type RunStatus = 'running' | 'success' | 'failed' | 'timeout' | 'skipped' | 'stopped'
export type TriggerType = 'schedule' | 'manual'

export interface TaskOptions {
  login_type: string
  enable_comments: boolean
  enable_sub_comments: boolean
  enable_media: boolean
  save_option: string
  headless: boolean
  cookies: string
  max_comments_count: number | null
  /** 账号维度：抓取哪些行为来源的帖子 */
  behaviors?: BehaviorSource[]
  /** 账号维度：账号池账号 ID */
  account_id?: number | null
  /** 账号维度：账号展示名 */
  account_name?: string | null
  /** 账号维度：本次每个行为最多抓多少条新帖（不填用爬虫默认 100） */
  max_notes?: number | null
}

export interface ScheduledTask {
  id: number
  task_code: string
  name: string
  platform: string
  task_type: TaskType
  dimension: TaskDimension
  target: string
  mode: TaskMode
  schedule_type: ScheduleType
  interval_sec: number | null
  cron_expr: string | null
  max_concurrency: number
  timeout_sec: number
  retry_count: number
  retry_delay_sec: number
  rate_interval_sec: number
  options: TaskOptions
  enabled: boolean
  status: string
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
  next_run_at: string | null
  run_count: number
  created_at: string | null
}

export interface ScheduledTaskRun {
  id: number
  task_id: number
  trigger_type: TriggerType
  status: RunStatus
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  new_count: number
  updated_count: number
  skipped_count: number
  attempt_count: number
  error_message: string | null
  log_tail: string[]
  /** 细粒度统计（历史运行记录可能为 null） */
  stats_json: RunStats | null
}

/** 单篇帖子的补抓前后对比 */
export interface RunStatsItem {
  content_id: string
  title: string
  before: RunStatsSnapshot
  after: RunStatsSnapshot
  /** 本轮实际补进来的评论条数 */
  gained: number
}

export interface RunStatsSnapshot {
  fetched: number
  declared: number
  /** 0~1，已抓评论数 / 原帖显示评论量 */
  completeness: number
}

export interface RunStats {
  added: RunStatsItem[]
  updated: RunStatsItem[]
}

export interface SchedulerStatus {
  /** 本进程是否在跑调度（拆分后 API 进程永远是 false，真正在跑的是 worker） */
  started: boolean
  /** 抓取执行位置：worker=独立执行器 / inline=进程内兼容模式 / offline=执行器未运行 */
  mode?: 'worker' | 'inline' | 'offline'
  /** 执行器存活信息 */
  worker_alive?: boolean
  worker?: { alive: boolean; pid: number | null; started_at: string | null }
  /** 排队中（等执行器拾取）的手动触发数 */
  queued_runs?: number
  enabled: boolean
  max_concurrency: number
  tick_sec: number
  total_tasks: number
  enabled_tasks: number
  running_tasks: number
  /** 本轮实时抓取/入库计数（随 status 每 5s 轮询更新） */
  live_stats?: LiveStats
  last_tick_at: string | null
  last_error: string
}

/** GET /scheduler/dimensions：维度元信息 + 账号池可选账号 */
export interface DimensionMeta {
  key: TaskDimension
  task_type: TaskType
  target_kind: 'profile_url' | 'note_url' | 'account_id'
  behaviors?: BehaviorSource[]
}

export interface AccountOption {
  id: number
  account_name: string
  status: string
  health_score: number
  has_cookie: boolean
}

export interface SchedulerDimensions {
  dimensions: DimensionMeta[]
  accounts: AccountOption[]
}

export interface ScheduledTaskPayload {
  name: string
  platform: string
  task_type: TaskType
  target: string
  mode: TaskMode
  schedule_type: ScheduleType
  interval_sec?: number
  cron_expr?: string
  max_concurrency: number
  timeout_sec: number
  retry_count: number
  retry_delay_sec: number
  rate_interval_sec: number
  options: TaskOptions
  enabled: boolean
}
