/**
 * 关键词输入框的链接识别
 *
 * 用户在关键词框粘贴帖子/视频链接时：
 * 1. 先判断"是否长得像链接"（避免把普通关键词误判）
 * 2. 再按域名判断归属平台
 * 3. 识别成功 → 调用方自动切换平台 + 切到详情模式并把链接移入 specified_ids
 * 4. 是链接但域名不认识 → 调用方抛出错提示
 */

export interface DetectedPlatform {
  platform: string
  label: string
}

/** 平台 → 可识别的域名（含短链域名） */
const PLATFORM_DOMAINS: Record<string, { domains: string[]; label: string }> = {
  xhs: { domains: ['xiaohongshu.com', 'xhslink.com'], label: '小红书' },
  dy: { domains: ['douyin.com', 'iesdouyin.com'], label: '抖音' },
  ks: { domains: ['kuaishou.com', 'v.kuaishou.com', 'gifshow.com'], label: '快手' },
  bili: { domains: ['bilibili.com', 'b23.tv'], label: '哔哩哔哩' },
  wb: { domains: ['weibo.com', 'weibo.cn'], label: '微博' },
  tieba: { domains: ['tieba.baidu.com', 'baidu.com'], label: '百度贴吧' },
  zhihu: { domains: ['zhihu.com', 'zhuanlan.zhihu.com'], label: '知乎' },
}

/** 是否"长得像链接"：带协议，或形如 www.xxx.com/... 的域名 */
export function looksLikeUrl(input: string): boolean {
  const v = input.trim()
  if (!v) return false
  if (/^https?:\/\//i.test(v)) return true
  // 不带协议但明显是域名：至少包含一个点，且顶级域是字母
  return /^[\w-]+(\.[\w-]+)+(\/[^\s]*)?$/i.test(v) && /\.[a-z]{2,}(\/|$|\?)/i.test(v)
}

/** 从链接中识别平台；无法识别返回 null */
export function detectPlatformFromUrl(input: string): DetectedPlatform | null {
  const v = input.trim()
  let host = ''
  try {
    const withProto = /^https?:\/\//i.test(v) ? v : `https://${v}`
    host = new URL(withProto).hostname.toLowerCase()
  } catch {
    return null
  }
  if (!host) return null
  // 逐级去掉子域名试匹配：www.xiaohongshu.com → xiaohongshu.com
  const bare = host.replace(/^www\./, '')
  for (const [platform, conf] of Object.entries(PLATFORM_DOMAINS)) {
    if (conf.domains.some((d) => bare === d || bare.endsWith(`.${d}`))) {
      return { platform, label: conf.label }
    }
  }
  return null
}
