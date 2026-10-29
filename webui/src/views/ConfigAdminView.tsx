/**
 * 配置管理页（运行时配置，DB 纳管 + 前端可编辑）
 *
 * 后端：api/routers/config_admin.py 暴露 /config/groups（分组查询）+ /config/items/{key}（单项更新，乐观锁）。
 * 数据来源：database/init_v2.py 的 CONFIG_GROUPS / CONFIG_ITEMS 种子；运行中可在本页直接改。
 *
 * 本页提供：
 * - 按分组展示全部配置项（抓取行为 / 代理风控 / 浏览器 / 存储 / 系统 / 小红书账号备份）
 * - int / decimal / bool / enum / string 内联编辑，带范围与正则校验
 * - 高级项开关（默认隐藏 is_advanced=true 的项）
 * - 生效时机徽标：立即生效 / 下次任务 / 需重启
 * - 乐观锁版本冲突提示（被别人改过会自动刷新）
 */

import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, Lock, RefreshCw,
  Settings2, SlidersHorizontal,
} from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { configAdminApi, type ConfigGroup, type ConfigItem } from '@/lib/api'

function GroupIcon({ code }: { code: string }) {
  const map: Record<string, ReactNode> = {
    crawl: <SlidersHorizontal className="h-4 w-4" />,
    risk: <AlertTriangle className="h-4 w-4" />,
    browser: <Settings2 className="h-4 w-4" />,
    storage: <SlidersHorizontal className="h-4 w-4" />,
    system: <Settings2 className="h-4 w-4" />,
    xhs_backup: <SlidersHorizontal className="h-4 w-4" />,
  }
  return map[code] || <Settings2 className="h-4 w-4" />
}

const TIMING_META: Record<string, { label: string; cls: string }> = {
  immediate: { label: '立即生效', cls: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' },
  next_task: { label: '下次任务生效', cls: 'border-cyan-500/40 bg-cyan-500/15 text-cyan-300' },
  restart: { label: '需重启', cls: 'border-amber-500/40 bg-amber-500/15 text-amber-300' },
}

function toStr(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

/** 校验用户输入，返回错误信息（空串=合法）。 */
function validate(item: ConfigItem, raw: string): string {
  const vt = item.value_type
  if (vt === 'int' || vt === 'decimal') {
    const num = vt === 'int' ? Number.parseInt(raw, 10) : Number.parseFloat(raw)
    if (Number.isNaN(num)) return vt === 'int' ? '必须为整数' : '必须为数字'
    if (vt === 'int' && !Number.isInteger(num)) return '必须为整数'
    if (item.min_value !== null && num < Number(item.min_value)) return `不能小于 ${item.min_value}`
    if (item.max_value !== null && num > Number(item.max_value)) return `不能大于 ${item.max_value}`
  } else if (vt === 'string' || vt === 'list' || vt === 'json') {
    if (item.min_value !== null && raw.length < Number(item.min_value)) return `长度不能小于 ${item.min_value}`
    if (item.max_value !== null && raw.length > Number(item.max_value)) return `长度不能大于 ${item.max_value}`
    if (item.regex_rule && !new RegExp(item.regex_rule).test(raw)) return '格式不正确'
    if (vt === 'json') {
      try { JSON.parse(raw) } catch { return 'JSON 格式非法' }
    }
  } else if (vt === 'enum') {
    const ok = (item.enum_options || []).some((o) => o.value === raw)
    if (!ok) return '枚举值非法'
  }
  return ''
}

export function ConfigAdminView() {
  const qc = useQueryClient()
  const [includeAdvanced, setIncludeAdvanced] = useState(false)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [savingKey, setSavingKey] = useState<string | null>(null)

  const groupsQ = useQuery({
    queryKey: ['configGroups', includeAdvanced],
    queryFn: async () => (await configAdminApi.getGroups(includeAdvanced)),
    refetchInterval: 15000,
  })

  const groups = groupsQ.data?.groups || []
  const totalItems = useMemo(() => groups.reduce((n, g) => n + g.items.length, 0), [groups])

  const saveM = useMutation({
    mutationFn: async ({ item, raw }: { item: ConfigItem; raw: string }) => {
      const vt = item.value_type
      let payload: unknown = raw
      if (vt === 'bool') payload = raw === 'true'
      else if (vt === 'int' || vt === 'decimal') payload = Number(raw)
      return configAdminApi.updateItem(item.key, payload, item.version)
    },
    onSuccess: (_d, v) => {
      toast.success(`已保存：${v.item.label}`)
      setSavingKey(null)
      qc.invalidateQueries({ queryKey: ['configGroups'] })
    },
    onError: (e: any) => {
      setSavingKey(null)
      const detail = e?.response?.data
      const code = detail?.code
      if (code === 40901) {
        toast.error('配置已被其他操作修改，已为你刷新，请重新保存')
        qc.invalidateQueries({ queryKey: ['configGroups'] })
      } else if (code === 40001 || code === 40002 || code === 40003 || code === 40004 || code === 40005) {
        toast.error(`保存失败：${detail?.message || '校验未通过'}`)
      } else {
        toast.error(`保存失败：${detail?.message || e?.message || '未知错误'}`)
      }
    },
  })

  const originalStr = (item: ConfigItem) => toStr(item.value === '***' ? item.default_value : item.value)

  const setDraft = (key: string, raw: string, item: ConfigItem) => {
    setDrafts((d) => ({ ...d, [key]: raw }))
    setErrors((e) => ({ ...e, [key]: validate(item, raw) }))
  }

  const onSave = (item: ConfigItem) => {
    const raw = drafts[item.key] ?? originalStr(item)
    const err = validate(item, raw)
    if (err) { setErrors((e) => ({ ...e, [item.key]: err })); return }
    setSavingKey(item.key)
    saveM.mutate({ item, raw })
  }

  const allCollapsed = groups.every((g) => collapsed[g.code])
  const toggleAll = () => {
    const next = !allCollapsed
    const map: Record<string, boolean> = {}
    groups.forEach((g) => { map[g.code] = next })
    setCollapsed(map)
  }

  return (
    <div className="flex h-full flex-col gap-4 overflow-hidden">
      {/* 头部 */}
      <div className="flex flex-shrink-0 flex-wrap items-center gap-2 glass-panel rounded-lg px-4 py-3">
        <SlidersHorizontal className="h-5 w-5 text-cyber-neon-cyan" />
        <div className="mr-auto">
          <h2 className="font-mono text-sm font-bold text-cyber-text-primary">配置管理</h2>
          <p className="text-[11px] text-cyber-text-muted">
            运行时配置（DB 纳管，可热改）；共 {totalItems} 项 · 修改后按各项「生效时机」落地
          </p>
        </div>
        <label className="flex cursor-pointer items-center gap-1.5 text-[11px] font-mono text-cyber-text-secondary">
          <input
            type="checkbox"
            checked={includeAdvanced}
            onChange={(e) => setIncludeAdvanced(e.target.checked)}
            className="h-3.5 w-3.5 accent-cyber-neon-cyan"
          />
          显示高级项
        </label>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={toggleAll} disabled={!groups.length}>
          {allCollapsed ? <ChevronRight className="mr-1 h-3.5 w-3.5" /> : <ChevronDown className="mr-1 h-3.5 w-3.5" />}
          {allCollapsed ? '展开全部' : '折叠全部'}
        </Button>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={() => groupsQ.refetch()} disabled={groupsQ.isFetching}>
          <RefreshCw className={`mr-1 h-3.5 w-3.5 ${groupsQ.isFetching ? 'animate-spin' : ''}`} />刷新
        </Button>
      </div>

      {/* 分组列表 */}
      <div className="flex-1 space-y-4 overflow-auto pr-1">
        {groupsQ.isLoading && (
          <div className="p-8 text-center font-mono text-xs text-cyber-text-muted">加载中…</div>
        )}
        {groups.length === 0 && !groupsQ.isLoading && (
          <div className="p-8 text-center font-mono text-xs text-cyber-text-muted">
            暂无配置项（请确认配置库可访问）
          </div>
        )}

        {groups.map((g: ConfigGroup) => {
          const isCollapsed = collapsed[g.code]
          const advancedCount = g.items.filter((i) => i.is_advanced).length
          return (
            <div key={g.code} className="glass-panel rounded-lg overflow-hidden">
              <button
                type="button"
                onClick={() => setCollapsed((c) => ({ ...c, [g.code]: !isCollapsed }))}
                className="flex w-full items-center gap-2 border-b border-cyber-border-subtle px-4 py-2.5 text-left transition-colors hover:bg-cyber-bg-tertiary/40"
              >
                <span className="text-cyber-neon-cyan">{GroupIcon({ code: g.code })}</span>
                <span className="font-mono text-xs font-bold text-cyber-text-primary">{g.name}</span>
                <span className="text-[11px] text-cyber-text-muted">{g.description}</span>
                {advancedCount > 0 && (
                  <Badge className="border-cyber-border-subtle bg-cyber-bg-tertiary text-[10px] text-cyber-text-muted">
                    {advancedCount} 高级
                  </Badge>
                )}
                <span className="ml-auto text-[11px] text-cyber-text-muted">{g.items.length} 项</span>
                {isCollapsed ? <ChevronRight className="h-4 w-4 text-cyber-text-secondary" /> : <ChevronDown className="h-4 w-4 text-cyber-text-secondary" />}
              </button>

              {!isCollapsed && (
                <div className="divide-y divide-cyber-border-subtle/50">
                  {g.items.map((item) => {
                    const draft = drafts[item.key] ?? originalStr(item)
                    const err = errors[item.key]
                    const dirty = draft !== originalStr(item)
                    const timing = TIMING_META[item.apply_timing] || { label: item.apply_timing, cls: 'border-cyber-border-subtle bg-cyber-bg-tertiary text-cyber-text-muted' }
                    return (
                      <div key={item.key} className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-[1fr_auto]">
                        {/* 左：名称 + 描述 + 默认值 */}
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-xs font-bold text-cyber-text-primary">{item.label}</span>
                            <span className="rounded border px-1.5 py-0.5 font-mono text-[10px] text-cyber-text-muted">
                              {item.key}
                            </span>
                            <Badge className={`border px-1.5 py-0.5 text-[10px] ${timing.cls}`}>{timing.label}</Badge>
                            {item.is_advanced && (
                              <Badge className="border-cyber-border-subtle bg-cyber-bg-tertiary text-[10px] text-cyber-text-muted">高级</Badge>
                            )}
                            {item.masked && (
                              <Badge className="border-cyber-border-subtle bg-cyber-bg-tertiary text-[10px] text-cyber-text-muted">
                                <Lock className="mr-0.5 h-2.5 w-2.5" />掩码
                              </Badge>
                            )}
                          </div>
                          {item.description && (
                            <p className="text-[11px] leading-snug text-cyber-text-secondary">{item.description}</p>
                          )}
                          <p className="font-mono text-[10px] text-cyber-text-muted">
                            默认：{toStr(item.default_value)}{item.unit ? ` ${item.unit}` : ''}
                            {(item.min_value !== null || item.max_value !== null) && (
                              <span> · 范围 {item.min_value ?? '−∞'} ~ {item.max_value ?? '+∞'}{item.unit ? ` ${item.unit}` : ''}</span>
                            )}
                          </p>
                        </div>

                        {/* 右：编辑器 + 保存 */}
                        <div className="flex flex-col gap-1 lg:w-72">
                          <div className="flex items-center gap-2">
                            {item.value_type === 'bool' ? (
                              <label className="flex cursor-pointer items-center gap-2 text-xs font-mono text-cyber-text-primary">
                                <input
                                  type="checkbox"
                                  checked={draft === 'true'}
                                  onChange={(e) => setDraft(item.key, e.target.checked ? 'true' : 'false', item)}
                                  className="h-4 w-4 accent-cyber-neon-cyan"
                                />
                                {draft === 'true' ? '开启' : '关闭'}
                              </label>
                            ) : item.value_type === 'enum' ? (
                              <select
                                value={draft}
                                onChange={(e) => setDraft(item.key, e.target.value, item)}
                                className="h-9 w-full rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-2 font-mono text-xs text-cyber-text-primary focus-visible:outline-none focus-visible:border-cyber-neon-cyan/50"
                              >
                                {(item.enum_options || []).map((o) => (
                                  <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                              </select>
                            ) : (
                              <Input
                                type={item.masked ? 'password' : (item.value_type === 'int' || item.value_type === 'decimal' ? 'number' : 'text')}
                                value={item.masked && draft === '' ? '' : draft}
                                placeholder={item.masked ? '留空=不变' : originalStr(item)}
                                onChange={(e) => setDraft(item.key, e.target.value, item)}
                                className={`h-9 font-mono text-xs ${err ? 'border-rose-500/60' : ''}`}
                              />
                            )}

                            <Button
                              size="sm"
                              variant="outline"
                              className="h-9 flex-shrink-0 font-mono text-[11px]"
                              disabled={!dirty || !!err || savingKey === item.key}
                              onClick={() => onSave(item)}
                            >
                              {savingKey === item.key ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                              保存
                            </Button>
                          </div>
                          {err && <p className="text-[10px] font-mono text-rose-300">{err}</p>}
                          {!err && dirty && (
                            <p className="text-[10px] font-mono text-cyber-neon-cyan">● 已修改，未保存</p>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
