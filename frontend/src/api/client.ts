/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const headers = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  }
  return fetch(url, {
    ...init,
    headers,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export async function postJson<T = any>(
  path: string,
  body: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
    headers,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = data?.message ?? `接口返回 ${response.status}`
    throw new Error(message)
  }
  return data as T
}
