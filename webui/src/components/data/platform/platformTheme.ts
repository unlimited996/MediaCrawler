/**
 * 平台主题注册表
 *
 * 数据详情页按各平台原生 UI 特点分别渲染：
 * - accent   平台品牌色（小红书红 / 知乎蓝 / 微博橙 / 抖音粉红 / B站粉 / 贴吧蓝 / 快手橙红）
 * - layout   布局变体，决定详情页的结构与组件风格
 * - dark     该平台的详情卡片是否使用深色舞台（抖音为深色播放器 UI）
 */

export type PlatformLayout =
  | 'xhs'
  | 'zhihu'
  | 'weibo'
  | 'douyin'
  | 'bili'
  | 'tieba'
  | 'kuaishou'
  | 'twitter'
  | 'facebook'
  | 'reddit'
  | 'default'

export interface PlatformTheme {
  /** 平台 key，与后端 PLATFORM 一致 */
  key: string
  /** 平台展示名 */
  label: string
  /** 品牌主色 */
  accent: string
  /** 品牌主色 - 柔和底色（用于标签 / 浅色背景） */
  accentSoft: string
  /** 布局变体 */
  layout: PlatformLayout
  /** 详情卡片是否深色舞台 */
  dark?: boolean
}

/** hex → rgba 字符串 */
export function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const r = parseInt(full.slice(0, 2), 16)
  const g = parseInt(full.slice(2, 4), 16)
  const b = parseInt(full.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

function theme(
  key: string,
  label: string,
  accent: string,
  layout: PlatformLayout,
  dark = false,
): PlatformTheme {
  return { key, label, accent, accentSoft: hexToRgba(accent, 0.1), layout, dark }
}

const REGISTRY: Record<string, PlatformTheme> = {
  xhs: theme('xhs', '小红书', '#FF2442', 'xhs'),
  zhihu: theme('zhihu', '知乎', '#0084FF', 'zhihu'),
  wb: theme('wb', '微博', '#FF8200', 'weibo'),
  dy: theme('dy', '抖音', '#FE2C55', 'douyin', true),
  bili: theme('bili', '哔哩哔哩', '#FB7299', 'bili'),
  tieba: theme('tieba', '百度贴吧', '#3388FF', 'tieba'),
  ks: theme('ks', '快手', '#FF4906', 'kuaishou'),
  // 海外平台：目前爬虫未覆盖，但数据一旦入库（导入 / 后续扩展）即按各自原生
  // 视觉规范渲染，避免和国内平台混用同一套卡片
  twitter: theme('twitter', 'X / Twitter', '#1D9BF0', 'twitter'),
  facebook: theme('facebook', 'Facebook', '#1877F2', 'facebook'),
  reddit: theme('reddit', 'Reddit', '#FF4500', 'reddit'),
}

const FALLBACK = theme('default', '内容', '#06B6D4', 'default')

/** 按平台取主题，未知平台走通用样式 */
export function getPlatformTheme(platform?: string | null): PlatformTheme {
  if (!platform) return FALLBACK
  return REGISTRY[platform] || { ...FALLBACK, key: platform, label: platform }
}
