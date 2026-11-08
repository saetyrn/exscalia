export function formatMinutes(total: number | null): string {
  if (total === null) return ''
  if (total < 60) return `${total} min`
  const h = Math.floor(total / 60)
  const m = total % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

export function formatTimer(seconds: number): string {
  if (seconds < 60) return `${seconds} sec`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}
