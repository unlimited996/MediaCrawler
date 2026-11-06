import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { schedulerApi } from '@/lib/api'
import type { ScheduledTaskPayload, ScheduledTaskRun } from '@/types/scheduler'

const QK = ['schedulerTasks']
const RUNS_QK = ['schedulerRuns']
const STATUS_QK = ['schedulerStatus']

/** 任务列表：5s 轮询，执行状态（running/idle）和下次执行时间能及时刷新 */
export function useSchedulerTasks(refetchInterval: number | false = 5000) {
  return useQuery({
    queryKey: QK,
    queryFn: async () => (await schedulerApi.listTasks()).data,
    refetchInterval,
  })
}

export function useSchedulerStatus() {
  return useQuery({
    queryKey: STATUS_QK,
    queryFn: async () => (await schedulerApi.status()).data,
    refetchInterval: 5000,
  })
}

/** 维度元信息 + 账号池可选账号（新建任务面板用，账号列表不常变，60s 轮询） */
export function useSchedulerDimensions() {
  return useQuery({
    queryKey: ['schedulerDimensions'],
    queryFn: async () => (await schedulerApi.dimensions()).data,
    refetchInterval: 60000,
  })
}

/** 执行记录列表：只返回轻量字段（无 stats_json / log_tail）。
 *
 * 轮询间隔按「是否有任务在跑」动态调整：
 *   * 有 running/queued -> 4s（看实时进度）
 *   * 全部已结束       -> 20s（记录不会变，没必要 5s 打一次）
 * 明细走 useSchedulerRunDetail，仅在展开时请求。
 */
export function useSchedulerRuns(limit = 50) {
  return useQuery({
    queryKey: [...RUNS_QK, limit],
    queryFn: async () => (await schedulerApi.listRuns(undefined, limit)).data,
    refetchInterval: (query) => {
      const rows = query.state.data as ScheduledTaskRun[] | undefined
      const busy = rows?.some((r) => r.status === 'running' || r.status === 'queued')
      return busy ? 4000 : 20000
    },
  })
}

/** 某条执行记录的明细：展开时才拉，已结束的记录不会变所以缓存久一点 */
export function useSchedulerRunDetail(runId: number | null) {
  return useQuery({
    queryKey: ['schedulerRun', runId],
    queryFn: async () => (await schedulerApi.getRun(runId as number)).data,
    enabled: runId != null,
    staleTime: 60_000,
    gcTime: 300_000,
  })
}

function useInvalidate() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: QK })
    qc.invalidateQueries({ queryKey: STATUS_QK })
    qc.invalidateQueries({ queryKey: RUNS_QK })
  }
}

export function useCreateTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (payload: ScheduledTaskPayload) => schedulerApi.createTask(payload),
    onSuccess: () => {
      toast.success('任务已创建')
      invalidate()
    },
    onError: (e: Error) => toast.error(`创建失败: ${e.message}`),
  })
}

export function useUpdateTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Partial<ScheduledTaskPayload> }) =>
      schedulerApi.updateTask(id, payload),
    onSuccess: () => {
      toast.success('任务已更新')
      invalidate()
    },
    onError: (e: Error) => toast.error(`更新失败: ${e.message}`),
  })
}

export function useDeleteTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (id: number) => schedulerApi.deleteTask(id),
    onSuccess: () => {
      toast.success('任务已删除')
      invalidate()
    },
    onError: (e: Error) => toast.error(`删除失败: ${e.message}`),
  })
}

export function useToggleTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      schedulerApi.toggleTask(id, enabled),
    onSuccess: () => {
      toast.success(enabledLabel())
      invalidate()
    },
    onError: (e: Error) => toast.error(`操作失败: ${e.message}`),
  })
}

function enabledLabel() {
  return '任务状态已更新'
}

export function useStopScheduler() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: () => schedulerApi.stop(),
    onSuccess: (res) => {
      const d = res.data
      if (d.stopped) toast.success(d.message || '已停止当前抓取')
      else toast.info(d.message || '当前没有正在执行的抓取')
      invalidate()
    },
    onError: (e: Error) => toast.error(`停止失败: ${e.message}`),
  })
}

export function useRunTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (id: number) => schedulerApi.runTask(id),
    onSuccess: (res) => {
      const d = res.data
      if (d.status === 'skipped') {
        // 跳过的原因可能是「通道被占用超时」也可能是「本任务正在执行」，
        // 后端把真实原因放在 error 里，优先展示它，别一律说成幂等保护
        toast.info(d.error || d.message || '本次触发已跳过')
      } else if (d.status === 'success') {
        toast.success(`执行成功：新增 ${d.new_count ?? 0}，刷新 ${d.updated_count ?? 0}`)
      } else if (d.status === 'failed' || d.status === 'timeout') {
        toast.error(`执行失败：${d.error || d.message || '未知错误'}`)
      } else {
        toast.info(d.message || '已开始执行')
      }
      invalidate()
    },
    onError: (e: Error) => toast.error(`触发失败: ${e.message}`),
  })
}
