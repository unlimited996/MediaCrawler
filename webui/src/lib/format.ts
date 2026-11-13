/** 字节数人类可读格式化：后端 original_size_bytes / stored_size_bytes 共用口径。
 *  1024 进制、B/KB/MB/GB，保留 1~2 位小数，避免 "12345678" 这种不可读数字。
 */
export function formatBytes(n: number | null | undefined): string {
  const v = Number(n || 0)
  if (!Number.isFinite(v) || v <= 0) return '0 B'
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  if (v < 1024 * 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`
  return `${(v / 1024 / 1024 / 1024).toFixed(2)} GB`
}
