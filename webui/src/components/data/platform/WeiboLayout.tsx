import { Repeat2, MessageSquare, Heart } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface WeiboLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 微博详情页
 * 橙色昵称 + 头像圆环 + 正文 + 九宫格图片 + 转发/评论/点赞三分栏 + 楼层式评论
 */
export function WeiboLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: WeiboLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5 shadow-sm">
        {/* 作者行：橙色昵称是微博的标识 */}
        <header className="flex items-center gap-3">
          <span className="rounded-full p-0.5" style={{ background: theme.accentSoft }}>
            <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={38} />
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-medium" style={{ color: theme.accent }}>
              {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
            </div>
            <div className="text-[11px] text-cyber-text-muted">
              {fmtTs(d?.publish_ts)}
              {sourceUrl && ` · ${t('manage.from')} ${theme.label}`}
            </div>
          </div>
        </header>

        {/* 正文 */}
        {content && (
          <div className="mt-3 whitespace-pre-wrap text-[15px] leading-7 text-cyber-text-primary">
            {content}
          </div>
        )}

        {/* 九宫格图片 */}
        {media.length > 0 && (
          <div className="mt-3 grid grid-cols-3 gap-1">
            {media.map((m, i) => (
              <a key={i} href={m.url} target="_blank" rel="noopener noreferrer">
                <img
                  src={m.url}
                  alt=""
                  loading="lazy"
                  className="aspect-square w-full rounded border border-cyber-border-subtle object-cover"
                />
              </a>
            ))}
          </div>
        )}

        {/* 话题 */}
        <TopicChips topics={tags} theme={theme} />

        {/* 三分栏操作条 */}
        <footer className="mt-4 flex items-center border-t border-cyber-border-subtle pt-2">
          {[
            { icon: Repeat2, label: t('manage.replay'), value: d?.share_count },
            { icon: MessageSquare, label: t('manage.comment'), value: d?.comment_count ?? commentsTotal },
            { icon: Heart, label: t('manage.likes'), value: d?.like_count },
          ].map(({ icon: Icon, label, value }, i) => (
            <div
              key={label}
              className={`flex flex-1 items-center justify-center gap-1.5 py-2 text-xs text-cyber-text-secondary ${
                i > 0 ? 'border-l border-cyber-border-subtle' : ''
              }`}
            >
              <Icon className="h-4 w-4" />
              <span className="font-medium">{value != null ? fmtNum(value) : label}</span>
              {value != null && <span className="hidden sm:inline">{label}</span>}
            </div>
          ))}
        </footer>
      </article>

      {/* 评论区 */}
      <section className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
        <h3 className="text-sm font-medium text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-4">
            {comments.map((c: any, idx: number) => (
              <li key={c.id} className="flex gap-2.5">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={30} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="font-medium" style={{ color: theme.accent }}>
                      {authorAnchor(c.author_url, c.author || '-')}
                    </span>
                    <span className="text-cyber-text-muted">{fmtTs(c.publish_ts)}</span>
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                    <span className="ml-auto text-[11px] text-cyber-text-muted">
                      {idx + 1}{t('manage.floor')}
                    </span>
                  </div>
                  <div className="mt-1 text-sm leading-6 text-cyber-text-primary"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                    <span>{t('manage.reply')}</span>
                  </div>
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme}
                    t={t}
                    replyToPrefix={`${t('manage.replyTo')} @`}
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
