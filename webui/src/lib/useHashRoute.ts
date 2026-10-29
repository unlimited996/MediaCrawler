/**
 * Hash 路由：让浏览器地址栏跟随当前菜单变化（#/crawl、#/data/xhs、#/guest…）。
 *
 * 好处：
 * - 刷新 / 分享链接后仍停留在对应视图（内部工具当演示页用时尤其重要）
 * - 无需服务端配合：hash 不参与请求路径，StaticFiles 托管零改动
 * - 访客页（#/guest）可以直接当独立链接分发
 *
 * 路由格式：#/{view}[/{platform}]
 *   view    = crawl | discover | data | browser | tasks | stance | backup | guest
 *   platform 仅 data / guest 使用（'' = 全部平台）
 */
import { useCallback, useEffect, useState } from 'react'

export interface RouteState {
  view: string
  platform: string
}

const VIEWS = [
  'crawl', 'discover', 'data', 'browser', 'tasks', 'stance', 'backup', 'config', 'guest',
]

function parseHash(): RouteState {
  const raw = window.location.hash.replace(/^#\/?/, '')
  const seg = raw.split('?')[0].split('/').filter(Boolean)
  const view = seg[0] && VIEWS.includes(seg[0]) ? seg[0] : 'crawl'
  const platform = (view === 'data' || view === 'guest') ? (seg[1] || '') : ''
  return { view, platform }
}

function toHash({ view, platform }: RouteState): string {
  const withPlatform = platform && (view === 'data' || view === 'guest')
  return `#/${view}${withPlatform ? `/${platform}` : ''}`
}

export function useHashRoute(): [RouteState, (next: RouteState) => void] {
  const [state, setState] = useState<RouteState>(parseHash)

  useEffect(() => {
    const onHashChange = () => setState(parseHash())
    window.addEventListener('hashchange', onHashChange)
    // 首屏把默认视图写进地址栏（replaceState 不污染历史栈）
    if (!window.location.hash) {
      window.history.replaceState(null, '', toHash(parseHash()))
    }
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback((next: RouteState) => {
    const target = toHash(next)
    if (window.location.hash === target) {
      setState(next) // 同一 hash 不触发 hashchange，手动同步
      return
    }
    window.location.hash = target
  }, [])

  return [state, navigate]
}
