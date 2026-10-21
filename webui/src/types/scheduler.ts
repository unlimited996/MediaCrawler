// 定时拉取任务相关类型（与后端 api/schemas/scheduler.py 对应）

export type TaskType = 'profile' | 'note'
export type TaskMode = 'full' | 'incremental'
export type ScheduleType = 'interval' | 'cron'
export type RunStatus = 'running' | 'success' | 'failed' | 'timeout' | 'skipped'
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
}

export interface ScheduledTask {
  id: number
  task_code: string
  name: string
  platform: string
  task_type: TaskType
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
}

export interface SchedulerStatus {
  started: boolean
  enabled: boolean
  max_concurrency: number
  tick_sec: number
  total_tasks: number
  enabled_tasks: number
  running_tasks: number
  last_tick_at: string | null
  last_error: string
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
