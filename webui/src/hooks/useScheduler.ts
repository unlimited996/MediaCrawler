import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { schedulerApi } from '@/lib/api'
import type { ScheduledTaskPayload } from '@/types/scheduler'

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

export function useSchedulerRuns(limit = 50) {
  return useQuery({
    queryKey: [...RUNS_QK, limit],
    queryFn: async () => (await schedulerApi.listRuns(undefined, limit)).data,
    refetchInterval: 5000,
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

export function useRunTask() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (id: number) => schedulerApi.runTask(id),
    onSuccess: (res) => {
      const d = res.data
      if (d.status === 'skipped') {
        toast.info(d.message || '该任务正在执行中，已跳过本次触发（幂等保护）')
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
