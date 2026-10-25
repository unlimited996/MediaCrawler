import axios from 'axios'
import type { QueueList, QueueAddPayload } from '@/types/crawler'
import type { ScheduledTask, ScheduledTaskRun, SchedulerStatus,
              ScheduledTaskPayload } from '@/types/scheduler'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 把后端返回的具体错误（detail）提升为 error.message，
// 否则所有失败提示都是千篇一律的 "Request failed with status code 400"
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const detail = error?.response?.data?.detail
    if (detail && typeof detail === 'string') {
      error.message = detail
    }
    return Promise.reject(error)
  },
)

// Types
export interface CrawlerConfig {
  platform: string
  login_type: string
  crawler_type: string
  keywords: string
  start_page: number
  enable_comments: boolean
  enable_sub_comments: boolean
  enable_media: boolean
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
}

export interface LogEntry {
  id: number
  timestamp: string
  level: 'info' | 'warning' | 'error' | 'success' | 'debug'
  message: string
}

export interface DataFile {
  name: string
  path: string
  size: number
  modified_at: number
  record_count: number | null
  type: string
}

export interface FilePreviewResponse {
  data: Record<string, unknown>[]
  total: number
  columns?: string[]
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

// API functions
export const crawlerApi = {
  start: (config: CrawlerConfig) => api.post('/crawler/start', config),
  stop: () => api.post('/crawler/stop'),
  getStatus: () => api.get<CrawlerStatus>('/crawler/status'),
  getLogs: (limit = 100) => api.get<{ logs: LogEntry[] }>('/crawler/logs', { params: { limit } }),
  // 扫码登录二维码（后台服务用户看不到浏览器窗口，推到页面上扫）
  getQrcode: () => api.get<{ qr: string | null; at?: string }>('/crawler/qrcode'),
}

/** 抓取队列：爬虫忙时提交的任务排队，前一个跑完自动续跑 */
export const crawlerQueueApi = {
  list: (status?: string) =>
    api.get<QueueList>('/crawler/queue', { params: status ? { status } : {} }),
  add: (payload: QueueAddPayload) =>
    api.post<{ status: string; added: number[]; duplicates: string[]; message: string }>(
      '/crawler/queue/add', payload),
  remove: (id: number) =>
    api.delete<{ status: string; removed: number }>(`/crawler/queue/${id}`),
  clear: (statuses?: string[]) =>
    api.post<{ status: string; removed: number; ids: number[] }>(
      '/crawler/queue/clear', { statuses }),
  retry: (payload: { ids?: number[]; all?: boolean }) =>
    api.post<{ status: string; requeued: number; ids: number[] }>(
      '/crawler/queue/retry', payload),
  resume: () => api.post<{ status: string; message: string }>('/crawler/queue/resume'),
}

export const dataApi = {
  getFiles: (platform?: string, fileType?: string) =>
    api.get<{ files: DataFile[] }>('/data/files', { params: { platform, file_type: fileType } }),
  getFileContent: (path: string, limit = 100) =>
    api.get<FilePreviewResponse>('/data/files/' + path, { params: { preview: true, limit } }),
  getStats: () => api.get('/data/stats'),
  getDownloadUrl: (path: string) => `/api/data/download/${path}`,
  deleteFile: (path: string) => api.delete<{ success: boolean; deleted: string }>(
    '/data/files/' + path),
}

export interface ExportPdfResult {
  success: boolean
  name: string
  size: number
  record_count: number
  image_count: number
  download_url: string
}

export const exportApi = {
  exportPdf: (payload: { file_path: string; limit?: number; embed_images?: boolean }) =>
    api.post<ExportPdfResult>('/export/pdf', payload),
  getDownloadUrl: (name: string) => `/api/export/download/${name}`,
}

export interface HotItem {
  rank_no: number
  title: string
  hot_value: number | null
  hot_value_text: string | null
  content_url: string | null
  tag: string | null
}

export interface HotBoard {
  board_id: number
  platform: string
  board_code: string
  board_name: string
  /** 东八区 ISO 字符串（带 +08:00 偏移） */
  captured_at: string
  /** 东八区展示值：'2026-10-16 16:42' */
  captured_at_local: string
  items: HotItem[]
}

export interface HotSnapshot {
  board_id: number
  platform: string
  board_code: string
  board_name: string
  /** 东八区 ISO 字符串（带 +08:00 偏移） */
  captured_at: string
  /** 东八区展示值：'2026-10-16 16:42' */
  captured_at_local: string
  /** 东八区简短值：'10-16 16:42'，时间轴节点用 */
  captured_at_short: string
  /** 0=今天 1=昨天 2=前天 …（按东八区日历日） */
  day_offset: number
  hour: number
  item_count: number
}

export const discoverApi = {
  boards: () => api.get<{
    hot: { platform: string; board_code: string; board_name: string }[]
    homefeed: string[]
  }>('/discover/boards'),
  // 不传 limit = 取平台返回的全部热搜（最大值）
  capture: (platform: string, limit?: number) =>
    api.post<{ code: number; data?: { board_id: number; item_count: number }; message?: string }>(
      '/discover/hot/capture', null, { params: { platform, limit } }),
  hotBoard: (boardId: number) => api.get<{ code: number; data?: HotBoard }>(`/discover/hot/board/${boardId}`),
  history: (platform?: string, limit = 100) =>
    api.get<{ items: HotSnapshot[] }>('/discover/hot/history', { params: { platform, limit } }),
  compare: (boardA: number, boardB: number) => api.get<{
    code: number
    data?: {
      new: string[]; gone: string[]
      moved: { title: string; from: number; to: number; delta: number }[]
      unchanged_count: number
    }
  }>('/discover/hot/compare', { params: { board_a: boardA, board_b: boardB } }),
  deleteBoard: (boardId: number) =>
    api.delete<{ code: number; data?: { deleted: number }; message?: string }>(
      `/discover/hot/board/${boardId}`),
}

export interface AccountRow {
  id: number
  platform: string
  account_name: string
  status: string
  health_score: number
  fail_count: number
  success_count: number
  cooldown_until: string | null
  has_cookie: boolean
}

export interface StoryRow {
  id: number
  title: string
  summary: string
  cover_url: string | null
  /** 帖子首张图的本地副本（优先展示，外链会过期） */
  cover_local_url: string
  /** 帖子首张图的 S3 备份地址（本地/外链都不可用时兜底） */
  cover_backup_url: string
  /** 正文前 50 字，无图时卡片展示 */
  excerpt: string
  media_count: number
  edit_count: number
  category: string | null
  platform: string | null
  author_id: number | null
  author: string
  author_avatar: string
  author_url: string
  like_count: number
  comment_count: number
  heat_score: number
  publish_ts: number | null
  tags: string[]
  /** S3 备份进度：已备份媒体数 / 媒体总数 */
  backup_total: number
  backup_uploaded: number
  /** 全部图片视频均已备份（卡片右上角小标识） */
  backup_complete: boolean
}

/** 详情页关注开关状态（对应后端 /stories/{id}/watch） */
export interface WatchState {
  enabled: boolean
  task_id: number | null
  /** 该对象是否具备建任务的条件（有原文链接 / 有作者主页） */
  supported: boolean
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
  next_run_at: string | null
  /** 任务因目标失效（帖子被删/无权限）被自动停止 */
  stopped: boolean
  stop_reason: string
}

export const storyApi = {
  list: (params: {
    platform?: string; keyword?: string; tag?: string; sort?: string
    start_ts?: number; end_ts?: number; page?: number; page_size?: number
    /** 发帖人多选：命中任一即展示（后端 author_ids 重复参数） */
    author_ids?: number[]
  }) => api.get<{ items: StoryRow[]; total: number; page: number; page_size: number }>(
    '/stories', { params, paramsSerializer: { indexes: null } }),
  /** 发帖人候选（多选筛选用）：按发帖量倒序，支持平台与昵称关键词收敛 */
  authors: (params: { platform?: string; keyword?: string; limit?: number } = {}) =>
    api.get<{ items: { id: number; name: string; avatar: string; url: string; story_count: number }[];
              total: number }>('/stories/authors/list', { params }),
  detail: (id: number) => api.get<Record<string, any>>(`/stories/${id}`),
  remove: (id: number) =>
    api.delete<{ success: boolean; deleted_id: number }>(`/stories/${id}`),
  /** 批量硬删除（物理删除，含章节/评论/标签/附件与本地媒体文件） */
  removeMany: (ids: number[], purge_files = true) =>
    api.post<{
      success: boolean; deleted_count: number; requested: number
      cascade: Record<string, number>
      failed: { id: number; error: string }[]
    }>('/stories/batch-delete', { ids, purge_files }),
  /** stance：按立场筛选 agree/neutral/oppose；tag：按态度标签筛选（逗号分隔多值） */
  comments: (id: number, sort = 'like', page = 1, page_size = 50, reply_limit = 20,
             stance?: string, sentiment?: string, tag?: string) =>
    api.get<{ items: any[]; total: number; page: number; page_size: number;
              stance_stats?: { agree: number; neutral: number; oppose: number;
                               total: number; agreement: string };
              sentiment_stats?: { positive: number; neutral: number; negative: number;
                                  total: number };
              /** 态度标签分布（库表驱动，含名称/颜色/条数） */
              tag_stats?: { code: string; name: string; color: string | null;
                            polarity: string | null; count: number }[] }>(
      `/stories/${id}/comments`,
      { params: { sort, page, page_size, reply_limit, stance, sentiment, tag } }),
  /** 评论表态可选标签（库表驱动，含颜色与极性） */
  commentStanceTags: () =>
    api.get<{ items: { code: string; name: string; color: string | null;
                       polarity: string; is_fallback: boolean }[] }>(
      '/stories/comments/stance-tags'),
  /** 人工改判一条评论的态度标签 */
  setCommentStance: (commentId: number, tagCode: string) =>
    api.patch<{ id: number; stance: string; stance_tag_code: string;
                stance_tag_name: string | null; stance_tag_color: string | null }>(
      `/stories/comments/${commentId}/stance`, { tag_code: tagCode }),
  /** 单条主评论的全部楼中楼：前端展开回复时按需拉取（默认接口只给前 20 条） */
  commentReplies: (id: number, commentId: number, page = 1, page_size = 500) =>
    api.get<{ items: any[]; total: number; page: number; page_size: number }>(
      `/stories/${id}/comments/${commentId}/replies`, { params: { page, page_size } }),
  tags: (limit = 50) =>
    api.get<{ items: { name: string; count: number }[] }>('/stories/tags/list', { params: { limit } }),
  stats: () => api.get<Record<string, any>>('/stories/stats/overview'),
  /** 关注状态：【关注此贴】(note) / 【关注此人】(author) */
  watch: (id: number) =>
    api.get<{ note: WatchState; author: WatchState }>(`/stories/${id}/watch`),
  /** 开关关注；开启时会立即拉取一次，之后每天一次 */
  setWatch: (id: number, kind: 'note' | 'author', enabled: boolean) =>
    api.post<{ kind: string; note?: WatchState; author?: WatchState }>(
      `/stories/${id}/watch/${kind}`, { enabled }),
  /** 按勾选的 ID 导出 PDF */
  exportSelected: (ids: number[], embed_images = true) =>
    api.post<{ success: boolean; name: string; size: number; record_count: number; download_url: string }>(
      '/export/stories', { ids, embed_images }),
}

export const accountsApi = {
  list: (platform?: string) =>
    api.get<{ code: number; data?: { items: AccountRow[]; total: number } }>(
      '/accounts', { params: { platform } }),
  upsert: (payload: { platform: string; account_name?: string; cookie: string }) =>
    api.post<{ code: number; data?: { id: number } }>('/accounts', payload),
  remove: (id: number) => api.delete<{ code: number }>(`/accounts/${id}`),
}

export const configApi = {
  getPlatforms: () => api.get<{ platforms: Platform[] }>('/config/platforms'),
  getOptions: () =>
    api.get<{
      login_types: ConfigOption[]
      crawler_types: ConfigOption[]
      save_options: ConfigOption[]
      /** 当前不可用而被隐藏的存储方式（仅作提示，不出现在下拉里） */
      save_options_hidden?: ConfigOption[]
    }>('/config/options'),
}

export interface EnvCheckResult {
  success: boolean
  message: string
  output?: string
  error?: string
}

export const envApi = {
  check: () => api.get<EnvCheckResult>('/env/check'),
}

export default api

// ---------------- 定时拉取任务 ----------------
export const schedulerApi = {
  status: () => api.get<SchedulerStatus>('/scheduler/status'),
  listTasks: () => api.get<ScheduledTask[]>('/scheduler/tasks'),
  getTask: (id: number) => api.get<ScheduledTask>(`/scheduler/tasks/${id}`),
  createTask: (payload: ScheduledTaskPayload) =>
    api.post<ScheduledTask>('/scheduler/tasks', payload),
  updateTask: (id: number, payload: Partial<ScheduledTaskPayload>) =>
    api.put<ScheduledTask>(`/scheduler/tasks/${id}`, payload),
  deleteTask: (id: number) =>
    api.delete<{ status: string; deleted: number }>(`/scheduler/tasks/${id}`),
  toggleTask: (id: number, enabled: boolean) =>
    api.post<ScheduledTask>(`/scheduler/tasks/${id}/toggle`, { enabled }),
  runTask: (id: number) =>
    api.post<{ status: string; reason?: string; message?: string; new_count?: number;
                updated_count?: number; run_id?: number; error?: string }>(
      `/scheduler/tasks/${id}/run`),
  listRuns: (taskId?: number, limit = 50) =>
    api.get<ScheduledTaskRun[]>('/scheduler/runs', {
      params: { task_id: taskId, limit },
    }),
  listTaskRuns: (taskId: number, limit = 50) =>
    api.get<ScheduledTaskRun[]>(`/scheduler/tasks/${taskId}/runs`, { params: { limit } }),
}

// ---------------- S3 媒体备份 ----------------
export interface S3ConfigRow {
  id: number
  name: string
  endpoint: string
  access_key_id: string
  /** 掩码后的 Secret（明文永不下发） */
  secret_masked: string
  has_secret: boolean
  region: string
  bucket: string
  addressing_style: string
  enabled: boolean
  last_check_at: string | null
  last_check_ok: boolean | null
  last_error: string | null
  created_at: string | null
}

export interface S3BackupItem {
  id: number
  media_id: number
  story_id: number | null
  platform: string | null
  content_id: string | null
  media_type: string
  object_key: string
  object_url: string
  source_url: string | null
  mime_type: string | null
  size_bytes: number | null
  status: 'pending' | 'uploading' | 'uploaded' | 'failed' | 'orphaned'
  retry_count: number
  max_retries: number
  next_retry_at: string | null
  last_error: string | null
  uploaded_at: string | null
  created_at: string | null
}

export interface S3BackupStats {
  configured: boolean
  config_name?: string
  by_status: Record<string, { count: number; bytes: number }>
  pending: number
  uploading: number
  uploaded: number
  failed: number
  /** 孤立备份：原帖已删但 S3 对象删除失败，待重试/手动清理 */
  orphaned: number
  uploaded_bytes: number
  last_error: string | null
  last_error_at: string | null
}

export const s3BackupApi = {
  // 配置
  listConfigs: () =>
    api.get<{ items: S3ConfigRow[] }>('/s3-backup/configs'),
  createConfig: (p: Partial<S3ConfigRow> & { secret_key: string }) =>
    api.post<S3ConfigRow>('/s3-backup/configs', p),
  updateConfig: (id: number, p: Partial<S3ConfigRow> & { secret_key?: string }) =>
    api.put<S3ConfigRow>(`/s3-backup/configs/${id}`, p),
  deleteConfig: (id: number) =>
    api.delete<{ success: boolean; deleted_id: number }>(`/s3-backup/configs/${id}`),
  toggleConfig: (id: number, enabled: boolean) =>
    api.post<{ success: boolean; id: number; enabled: boolean }>(
      `/s3-backup/configs/${id}/toggle`, { enabled }),
  testSaved: (id: number) =>
    api.post<{ ok: boolean; latency_ms?: number; error?: string }>(
      `/s3-backup/configs/${id}/test`),
  testUnsaved: (p: Record<string, string | number>) =>
    api.post<{ ok: boolean; latency_ms?: number; error?: string }>(
      '/s3-backup/configs/test', p),
  // 备份任务
  stats: () => api.get<S3BackupStats>('/s3-backup/stats'),
  run: (limit = 20) =>
    api.post<{ started: boolean; limit?: number; note?: string }>(
      '/s3-backup/run', {}, { params: { limit } }),
  retryFailed: (ids?: number[]) =>
    api.post<{ reset: number }>('/s3-backup/retry', { ids }),
  enqueueMissing: () =>
    api.post<{ started: boolean }>('/s3-backup/enqueue-missing'),
  listItems: (params: { status?: string; platform?: string; story_id?: number;
                        page?: number; page_size?: number } = {}) =>
    api.get<{ items: S3BackupItem[]; total: number; page: number; page_size: number }>(
      '/s3-backup/items', { params }),
  retryItem: (id: number) =>
    api.post<{ reset: number }>(`/s3-backup/items/${id}/retry`),
  deleteItem: (id: number) =>
    api.delete<{ success: boolean; object_deleted: boolean | null;
                 kept_for_refs: boolean }>(`/s3-backup/items/${id}`),
  /** 手动清理单条备份：删 S3 对象 + 删记录（orphaned 兜底） */
  purgeItem: (id: number) =>
    api.post<{ success: boolean; object_deleted?: boolean | null;
                kept_for_refs?: boolean; error?: string | null }>(
      `/s3-backup/items/${id}/purge`),
  /** 清理全部孤立备份 */
  purgeOrphaned: () =>
    api.post<{ picked: number; purged: number; still: number }>(
      '/s3-backup/purge-orphaned'),
  backupStory: (storyId: number, run = true) =>
    api.post<{ queued: number; media_total: number }>(
      `/s3-backup/stories/${storyId}/backup`, {}, { params: { run } }),
  storyStatus: (storyId: number) =>
    api.get<{ total: number; uploaded: number; complete: boolean }>(
      `/s3-backup/stories/${storyId}/status`),
}
