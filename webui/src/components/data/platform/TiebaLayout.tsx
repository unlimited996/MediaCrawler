import { MessageSquare, ThumbsUp, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, MediaImage, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface TiebaLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 贴吧详情页
 * 帖子标题 + 「楼主」楼层标识 + 正文 + 图片 + 回复/点赞底栏 + 楼层编号评论
 */
export function TiebaLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: TiebaLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="overflow-hidden rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel shadow-sm">
        {/* 标题条 */}
        <div className="flex items-center gap-2 border-b border-cyber-border-subtle px-5 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-bold text-cyber-text-primary">
            {d?.title || '-'}
          </h2>
          <span
            className="flex-shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium text-white"
            style={{ background: theme.accent }}
          >
            {theme.label}
          </span>
        </div>

        <div className="p-5">
          {/* 楼主行 */}
          <div className="flex items-center gap-2.5">
            <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={32} />
            <div className="min-w-0">
              <div className="truncate text-xs font-medium text-cyber-text-primary">
                {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
              </div>
              <div className="text-[11px] text-cyber-text-muted">{fmtTs(d?.publish_ts)}</div>
            </div>
            <span
              className="ml-auto rounded px-1.5 py-0.5 text-[10px]"
              style={{ color: theme.accent, background: theme.accentSoft }}
            >
              {t('manage.lz')}
            </span>
          </div>

          {/* 正文 */}
          {content && (
            <div className="mt-3 whitespace-pre-wrap text-sm leading-7 text-cyber-text-secondary">
              {content}
            </div>
          )}

          {/* 图片 */}
          {media.length > 0 && (
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
              {media.map((m, i) => (
                <a key={i} href={m.url} target="_blank" rel="noopener noreferrer">
                <MediaImage
                  media={m}
                  className="aspect-square w-full rounded border border-cyber-border-subtle object-cover"
                />
                </a>
              ))}
            </div>
          )}

          {/* 标签 */}
          <TopicChips topics={tags} theme={theme} />
        </div>

        {/* 底栏 */}
        <footer className="flex items-center gap-2 border-t border-cyber-border-subtle px-5 py-2.5">
          <span
            className="flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium text-white"
            style={{ background: theme.accent }}
          >
            <MessageSquare className="h-3.5 w-3.5" />
            {t('manage.reply')} {fmtNum(d?.comment_count ?? commentsTotal)}
          </span>
          <span className="flex items-center gap-1.5 rounded border border-cyber-border-subtle px-3 py-1.5 text-xs text-cyber-text-secondary">
            <ThumbsUp className="h-3.5 w-3.5" />
            {fmtNum(d?.like_count)}
          </span>
          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto flex items-center gap-1 text-[11px] text-cyber-text-muted hover:text-cyber-text-primary"
            >
              <ExternalLink className="h-3 w-3" />
              {t('manage.originLink')}
            </a>
          )}
        </footer>
      </article>

      {/* 评论区（楼层） */}
      <section className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
        <h3 className="text-sm font-medium text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-3">
            {comments.map((c: any, idx: number) => (
              <li
                key={c.id}
                className="rounded-lg border border-cyber-border-subtle bg-cyber-bg-tertiary/40 p-3"
              >
                <div className="flex items-center gap-2">
                  <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={24} />
                  <span className="text-xs font-medium text-cyber-text-primary">
                    {authorAnchor(c.author_url, c.author || '-')}
                  </span>
                  <span className="text-[11px] text-cyber-text-muted">{fmtTs(c.publish_ts)}</span>
                  <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  <span className="ml-auto text-[11px] text-cyber-text-muted">
                    {idx + 1}{t('manage.floor')}
                  </span>
                </div>
                <div className="mt-1.5 text-sm leading-6 text-cyber-text-secondary"><RichText content={c.content} /></div>
                <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                  <span className="flex items-center gap-1">
                    <ThumbsUp className="h-3 w-3" />
                    {fmtNum(c.like_count)}
                  </span>
                  <span>{t('manage.reply')}</span>
                </div>
                <CommentReplies
                  replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                  theme={theme}
                  t={t}
                  containerClassName="mt-2 space-y-1.5 border-l-2 border-cyber-border-subtle pl-3"
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
