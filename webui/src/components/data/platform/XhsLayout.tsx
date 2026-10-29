import { useState } from 'react'
import type { ReactNode } from 'react'
import {
  Heart, Star, MessageSquare, Repeat2, Plus,
} from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import { storyApi } from '@/lib/api'
import {buildContent, pickMedia, MediaImage, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, CommentImages, StanceBadge} from './shared'

export interface LayoutProps {
  d: any
  comments: any[]
  commentsTotal: number
  /** 评论区头部工具条（统计 + 筛选），由详情页注入 */
  commentsToolbar?: ReactNode
  t: (key: string, opts?: any) => string
}

interface XhsLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 小红书详情页
 * 白底圆角笔记卡 → 作者行 → 标题 → 正文 → 图片网格 → 话题标签 → 互动栏
 * → 独立评论卡片（评论输入框 → 评论列表）
 *
 * 排版量值取自真实小红书桌面端详情页样式：正文 16px / 行高 1.5（.desc），
 * 作者昵称 16px、评论昵称三级灰（rgba(51,51,51,.6)）、评论正文 14px，
 * 楼中楼缩进 36px，主色 #FF2442，边框 rgba(0,0,0,.08)，选中态 rgba(0,0,0,.03)。
 * 图片与文字为上下堆叠（不做左右分栏），窄屏自适应靠 padding 与网格列数切换。
 */
export function XhsLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: XhsLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  // 自定义标签：点标签栏的「+」就地输入，回车保存。已存的会写进 story_tag，
  // 重新打开详情时由后端带回来，这里只是先本地即时显示。
  const [tagDraft, setTagDraft] = useState('')
  const [addingTag, setAddingTag] = useState(false)
  const [tagSaving, setTagSaving] = useState(false)
  const [extraTags, setExtraTags] = useState<string[]>([])
  const allTags = Array.from(new Set([...tags, ...extraTags]))

  async function submitTag() {
    const name = tagDraft.trim()
    if (!name || !d?.id) return
    setTagSaving(true)
    try {
      await storyApi.addStoryTag(Number(d.id), name)
      setExtraTags((prev) => (prev.includes(name) ? prev : [...prev, name]))
      setTagDraft('')
      setAddingTag(false)
    } catch (e) {
      // 失败时保留输入内容，方便改了重加
      console.error('add tag failed', e)
    } finally {
      setTagSaving(false)
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4">
      {/* 笔记卡片 */}
      <article className="rounded-2xl border border-black/[0.08] bg-white p-6">
        {/* 作者行 */}
        <header className="flex items-center gap-3">
          <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={40} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-base font-medium text-[#333]">
              {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
            </div>
            <div className="text-xs text-[#333]/60">{fmtTs(d?.publish_ts)}</div>
          </div>
          {/* 右上角：查看原文（描边胶囊，跳转平台原帖） */}
          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              title={t('manage.originLink')}
              className="shrink-0 rounded-full border px-3.5 py-1 text-xs font-medium transition-colors hover:opacity-80"
              style={{ color: theme.accent, borderColor: theme.accent }}
            >
              {t('manage.originLink')}
            </a>
          )}
        </header>

        {/* 标题 */}
        {d?.title && (
          <h2 className="mt-4 text-lg font-bold leading-snug text-[#333] sm:text-xl">
            {d.title}
          </h2>
        )}

        {/* 正文：16px / 行高 1.5（对齐小红书 .desc） */}
        {content && (
          <div className="mt-2 whitespace-pre-wrap break-words text-base leading-[1.5] text-[#333]">
            {content}
          </div>
        )}

        {/* 图片网格：单图独占一行，≤4 两列，>4 三列 */}
        {media.length > 0 && (
          <div
            className={`mt-3 grid gap-1.5 ${
              media.length === 1 ? 'grid-cols-1' : media.length <= 4 ? 'grid-cols-2' : 'grid-cols-3'
            }`}
          >
            {media.map((m, i) => (
              <a key={i} href={m.url} target="_blank" rel="noopener noreferrer" className="block">
                <MediaImage
                  media={m}
                  className={`w-full rounded-lg object-cover ${
                    media.length === 1 ? 'max-h-[420px]' : 'aspect-square'
                  }`}
                />
              </a>
            ))}
          </div>
        )}

        {/* 话题标签 + 自定义标签入口（右上角小「+」，内联输入回车即存） */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <TopicChips topics={allTags} theme={theme} />
          {!addingTag ? (
            <button
              type="button"
              title="添加自定义标签"
              onClick={() => setAddingTag(true)}
              className="flex h-5 w-5 items-center justify-center rounded-full border border-dashed border-[#ddd] text-[#999] transition-colors hover:border-[#FF2442] hover:text-[#FF2442]"
            >
              <Plus className="h-3 w-3" />
            </button>
          ) : (
            <span className="flex items-center gap-1">
              <input
                autoFocus
                value={tagDraft}
                onChange={(e) => setTagDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') submitTag()
                  if (e.key === 'Escape') { setAddingTag(false); setTagDraft('') }
                }}
                placeholder="标签名"
                className="h-5 w-20 rounded-full border border-[#eee] px-2 text-[11px] font-mono text-[#333] outline-none"
              />
              <button
                type="button"
                onClick={submitTag}
                disabled={tagSaving}
                className="rounded-full px-2 text-[11px] font-mono text-white disabled:opacity-50"
                style={{ background: theme.accent }}
              >
                加
              </button>
            </span>
          )}
        </div>

        {/* 互动栏：图标 18px + 13px 数字，顶部细分隔线（对齐小红书 interactions） */}
        <footer className="mt-4 flex items-center gap-4 border-t border-black/[0.08] pt-3 text-[13px] text-[#333]/80">
          <span className="flex items-center gap-1">
            <Heart className="h-[18px] w-[18px]" style={{ color: theme.accent }} />
            {fmtNum(d?.like_count)}
          </span>
          {d?.collect_count != null && (
            <span className="flex items-center gap-1">
              <Star className="h-[18px] w-[18px] text-[#FFB800]" />
              {fmtNum(d?.collect_count)}
            </span>
          )}
          <span className="flex items-center gap-1">
            <MessageSquare className="h-[18px] w-[18px]" />
            {fmtNum(d?.comment_count ?? commentsTotal)}
          </span>
          {d?.share_count != null && (
            <span className="flex items-center gap-1">
              <Repeat2 className="h-[18px] w-[18px]" />
              {fmtNum(d?.share_count)}
            </span>
          )}
        </footer>
      </article>

      {/* 评论区：独立白卡（对齐小红书 comments-container padding:16px） */}
      <section className="rounded-2xl border border-black/[0.08] bg-white p-4 sm:p-5">
        {/* 评论输入占位：原站为浅灰胶囊 */}
        <div className="rounded-full bg-[#F5F5F5] px-3.5 py-2 text-xs text-[#999]">
          {t('manage.saySomething') || '说点什么…'}
        </div>

        <h3 className="mt-3 text-[13px] text-[#333]/60">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-[#999]">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-3">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-2.5">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={24} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5 text-[14px] text-[#333]/60">
                    {authorAnchor(c.author_url, c.author || '-')}
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  </div>
                  {/* 评论正文 14px / 行高 1.5（对齐小红书评论正文） */}
                  <div className="mt-0.5 text-[14px] leading-[1.5] text-[#333]">
                    <RichText content={c.content} />
                  </div>
                  <CommentImages pictures={c.pictures} />
                  <div className="mt-1 flex items-center gap-3 text-xs text-[#333]/60">
                    <span>{fmtTs(c.publish_ts)}</span>
                    {c.ip_location ? <span>· {c.ip_location}</span> : null}
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                  </div>
                  {/* 楼中楼：缩进约 36px，对齐父评论正文起点 */}
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme}
                    t={t}
                    containerClassName="mt-2 space-y-2 pl-9"
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
