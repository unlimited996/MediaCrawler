export interface CrawlerConfig {
  platform: string
  login_type: string
  crawler_type: string
  keywords: string
  specified_ids: string  // 详情模式下的帖子/视频ID
  creator_ids: string    // 创作者模式下的创作者ID
  start_page: number
  enable_comments: boolean
  enable_sub_comments: boolean
  enable_media: boolean  // 是否下载媒体（封面/视频/图文图片），支持 xhs/dy/ks/bili/wb
  save_option: string
  cookies: string
  headless: boolean
}

export interface CrawlerStatus {
  status: 'idle' | 'running' | 'stopping' | 'error'
  platform: string | null
  crawler_type: string | null
  started_at: string | null
  error_message: string | null
  queue_pending: number
  queue_running: number
}

export type QueueItemStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

export interface QueueItem {
  id: number
  url: string
  platform: string
  crawler_type: string
  status: QueueItemStatus
  added_at: string | null
  started_at: string | null
  finished_at: string | null
  error: string | null
  batch_id: number | null
}

export interface QueueList {
  items: QueueItem[]
  pending: number
  running: number
  done: number
  failed: number
  cancelled: number
  auto_run: boolean
  batch_size: number
}

export interface QueueAddPayload {
  urls: string[]
  platform: string
  crawler_type: string
  login_type?: string
  start_page?: number
  enable_comments?: boolean
  enable_sub_comments?: boolean
  enable_media?: boolean
  save_option?: string
  cookies?: string
  headless?: boolean
}

export interface LogEntry {
  id: number
  timestamp: string
  level: 'info' | 'warning' | 'error' | 'success' | 'debug'
  message: string
}

/** 本轮实时抓取/入库统计（WS stats 消息 + /scheduler/status 的 live_stats） */
export interface LiveStats {
  type: 'stats'
  /** 抓取动作数（向平台发起的数据请求） */
  fetched: number
  /** 落库条目数（store 写入的帖子/评论） */
  saved: number
  run_id: number | null
  task_id: number | null
  started_at: string | null
  updated_at: string | null
}

export interface DataFile {
  name: string
  path: string
  size: number
  modified_at: number
  record_count: number | null
  type: string
}

export interface Platform {
  value: string
  label: string
  icon: string
}

export interface ConfigOption {
  value: string
  label: string
}
