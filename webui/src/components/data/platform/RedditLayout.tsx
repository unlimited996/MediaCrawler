import { ArrowBigUp, ArrowBigDown, MessageSquare, Share, Bookmark, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {
  buildContent, pickMedia, MediaImage, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink,
  authorAnchor, TopicChips, CommentReplies, StanceBadge,
} from './shared'
import type { LayoutProps } from './XhsLayout'

interface RedditLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * Reddit 详情页
 *
 * 对齐 Reddit 原生帖子：左侧竖排投票列（上/下箭头 + karma 数字），
 * 正文区顶部是 `r/xxx · u/author · 时间` 的元信息行，底部是灰底 pill 操作条；
 * 评论区最显著的特征是**左侧贯通竖线 + 逐层缩进**（thread line），
 * 与国内平台的卡片流一眼可辨。
 */
export function RedditLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: RedditLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const subreddit = d?.category || d?.source_site || 'all'
  const author = d?.author || t('manage.author')
  const karma = d?.like_count ?? 0

  /** Reddit 的灰色 pill 操作条 */
  const actionPill =
    'flex items-center gap-1.5 rounded-full bg-cyber-bg-tertiary px-2.5 py-1 text-[12px] font-bold text-cyber-text-muted'

  return (
    <div className="mx-auto max-w-3xl space-y-3">
      {/* 帖子卡：投票列 + 内容列 */}
      <article className="flex overflow-hidden rounded-md border border-cyber-border-subtle bg-cyber-bg-panel">
        {/* 左侧投票列 */}
        <div className="flex w-10 flex-shrink-0 flex-col items-center gap-0.5 bg-cyber-bg-tertiary/50 py-2">
          <ArrowBigUp className="h-5 w-5 cursor-pointer" style={{ color: theme.accent }} />
          <span className="text-[12px] font-bold" style={{ color: theme.accent }}>{fmtNum(karma)}</span>
          <ArrowBigDown className="h-5 w-5 text-cyber-text-muted" />
        </div>

        <div className="min-w-0 flex-1 px-3 py-2">
          {/* 元信息行：r/xxx · u/author · 时间 */}
          <div className="flex flex-wrap items-center gap-1 text-[12px] text-cyber-text-muted">
            <span className="font-bold text-cyber-text-primary">r/{subreddit}</span>
            <span>·</span>
            <span>u/{author}</span>
            <span>·</span>
            <span>{fmtTs(d?.publish_ts)}</span>
            <span
              className="ml-auto rounded-full px-2 py-[1px] text-[11px] font-medium"
              style={{ color: theme.accent, background: theme.accentSoft }}
            >
              {theme.label}
            </span>
          </div>

          {d?.title && (
            <h2 className="mt-1 text-[18px] font-bold leading-snug text-cyber-text-primary">
              {d.title}
            </h2>
          )}

          {content && (
            <div className="mt-1.5 whitespace-pre-wrap text-[14px] leading-7 text-cyber-text-secondary">
              {content}
            </div>
          )}

          {/* 媒体：Reddit 正文图多为整幅 */}
          {media.length > 0 && (
            <div className="mt-2 overflow-hidden rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/40">
              {media.slice(0, 4).map((m, i) => (
                <a key={i} href={m.url} target="_blank" rel="noopener noreferrer" className="block">
                  <MediaImage media={m} className="max-h-[460px] w-full object-contain" />
                </a>
              ))}
            </div>
          )}

          <TopicChips topics={tags} theme={theme} className="mt-2" />

          {/* 操作条 */}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className={actionPill}>
              <MessageSquare className="h-3.5 w-3.5" />
              {fmtNum(d?.comment_count ?? commentsTotal)} {t('manage.comments')}
            </span>
            <span className={actionPill}>
              <Share className="h-3.5 w-3.5" />
              {t('manage.share')}
            </span>
            <span className={actionPill}>
              <Bookmark className="h-3.5 w-3.5" />
              {t('manage.collect')}
            </span>
            {sourceUrl && (
              <a
                href={sourceUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="ml-auto flex items-center gap-1 text-[12px] text-cyber-text-muted hover:text-cyber-text-primary"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                {t('manage.originLink')}
              </a>
            )}
          </div>
        </div>
      </article>

      {/* 评论区：左侧贯通竖线 + 缩进，Reddit 的 threading 视觉 */}
      <section className="rounded-md border border-cyber-border-subtle bg-cyber-bg-panel px-4 py-3">
        <h3 className="text-[14px] font-bold text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-3 text-[13px] text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-2 space-y-3">
            {comments.map((c: any) => (
              <li key={c.id} className="relative">
                {/* thread line：Reddit 每条评论左侧那条可折叠的竖线 */}
                <span
                  aria-hidden
                  className="absolute left-[13px] top-7 bottom-0 w-px bg-cyber-border-subtle"
                />
                <div className="flex gap-2">
                  <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={24} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-cyber-text-muted">
                      <span className="font-bold text-cyber-text-primary">
                        u/{authorAnchor(c.author_url, c.author || '-')}
                      </span>
                      <span>·</span>
                      <span>{fmtNum(c.like_count)} {t('manage.likes')}</span>
                      <span>·</span>
                      <span>{fmtTs(c.publish_ts)}</span>
                      <StanceBadge stance={c.stance} t={t} size="xs" tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                    </div>
                    <div className="mt-1 text-[14px] leading-6 text-cyber-text-primary">
                      <RichText content={c.content} />
                    </div>
                    <div className="mt-1 flex items-center gap-3 text-[12px] font-bold text-cyber-text-muted">
                      <ArrowBigUp className="h-3.5 w-3.5" />
                      <ArrowBigDown className="h-3.5 w-3.5" />
                      <span>{t('manage.reply')}</span>
                    </div>
                    {/* 回复缩进一层，Reddit 的嵌套靠连续竖线表达 */}
                    <CommentReplies
                      replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                      theme={theme} t={t}
                      containerClassName="relative mt-2 space-y-2.5 border-l border-cyber-border-subtle pl-3"
                    />
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
