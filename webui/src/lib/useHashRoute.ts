/**
 * Hash 路由：让浏览器地址栏跟随当前菜单变化（#/crawl、#/data/xhs、#/guest…）。
 *
 * 好处：
 * - 刷新 / 分享链接后仍停留在对应视图（内部工具当演示页用时尤其重要）
 * - 无需服务端配合：hash 不参与请求路径，StaticFiles 托管零改动
 * - 访客页（#/guest）可以直接当独立链接分发
 *
 * 路由格式：#/{view}[/{platform}][/story/{id}]
 *   view    = crawl | discover | data | browser | tasks | stance | backup | guest
 *   platform  仅 data / guest 使用（'' = 全部平台）
 *   story/{id} 帖子详情：打开某条帖子时写进 URL，刷新/分享链接能直接恢复到该帖子
 */
import { useCallback, useEffect, useState } from 'react'

export interface RouteState {
  view: string
  platform: string
  /** 帖子详情 ID；仅 data / guest 视图使用，刷新后可恢复到该帖子 */
  storyId?: number | null
}

const VIEWS = [
  'crawl', 'discover', 'data', 'browser', 'tasks', 'stance', 'backup', 'config', 'guest',
]

function parseHash(): RouteState {
  const raw = window.location.hash.replace(/^#\/?/, '')
  const seg = raw.split('?')[0].split('/').filter(Boolean)
  const view = seg[0] && VIEWS.includes(seg[0]) ? seg[0] : 'crawl'
  let platform = ''
  let storyId: number | null = null
  if (view === 'data' || view === 'guest') {
    // 形如：
    //   #/data/{platform}/story/{id}   指定平台下的详情
    //   #/data/story/{id}              全部平台下的详情（platform 为空，用 story 作哨兵段）
    if (seg[1] === 'story') {
      const n = Number.parseInt(seg[2] || '', 10)
      if (n > 0) storyId = n
    } else {
      platform = seg[1] || ''
      if (seg[2] === 'story' && seg[3]) {
        const n = Number.parseInt(seg[3], 10)
        if (n > 0) storyId = n
      }
    }
  }
  return { view, platform, storyId }
}

function toHash({ view, platform, storyId }: RouteState): string {
  let h = `#/${view}`
  if ((view === 'data' || view === 'guest') && platform) h += `/${platform}`
  if (storyId) h += `/story/${storyId}`
  return h
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
