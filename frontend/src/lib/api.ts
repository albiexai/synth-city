import { assertBrowserNativeRequest } from '@/features/native-settings/surfaceRegistry'
import { getOperatorToken } from '@/features/agent-teams/operatorAuth'
import { API_BASE_URL } from './constants'

const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

// #1173: pages that show server data read it again at once after a write the user made. A write that succeeds
// calls notifyApiWrite. The shared reads register one listener. Every fetch that writes outside this file calls it too.
const writeListeners = new Set<() => void>()
export function onApiWrite(listener: () => void) {
  writeListeners.add(listener)
  return () => { writeListeners.delete(listener) }
}
/** Call after a write request succeeded. A read (GET, HEAD, OPTIONS) does nothing. */
export function notifyApiWrite(method?: string) {
  if (READ_METHODS.has((method ?? 'GET').toUpperCase())) return
  writeListeners.forEach((listener) => listener())
}

/**
 * The stored Director key for a write request, so settings writes pass the
 * backend's guard when a key is configured. Reads never carry it.
 */
export function operatorWriteHeaders(method = 'GET'): Record<string, string> {
  if (READ_METHODS.has(method.toUpperCase())) return {}
  const token = getOperatorToken()
  return token ? { 'X-Synth-City-Operator-Token': token } : {}
}

/**
 * The OS user whose global scope a Harnesses page shows. The choice lives in the page URL
 * (?scope_user=) and goes to the backend as a header, so no page has to pass it on.
 * Only /harnesses pages send it; the backend ignores it elsewhere.
 */
export function scopeUserHeaders(): Record<string, string> {
  if (typeof window === 'undefined' || !window.location.pathname.startsWith('/harnesses')) return {}
  const user = new URLSearchParams(window.location.search).get('scope_user')
  return user ? { 'X-Harness-Scope-User': user } : {}
}

/**
 * Build an API endpoint URL with optional query parameters.
 * Filters out undefined values automatically.
 */
export function buildEndpoint(
  endpoint: string,
  params?: Record<string, string | number | boolean | undefined>
): string {
  if (!params) return endpoint;

  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined) {
      searchParams.append(key, String(value));
    }
  });

  const queryString = searchParams.toString();
  return queryString ? `${endpoint}?${queryString}` : endpoint;
}

export interface ApiError {
  message?: string
  detail?: string | { message?: string; block_code?: string; code?: string; msg?: string; proven_non_write?: boolean } | Array<{ msg?: string }>
}

const operatorErrorMessages: Record<string, string> = {
  operator_token_required: 'Enter the Director key to continue.',
  operator_token_invalid: 'The Director key was rejected. Clear it and enter a valid key.',
  operator_token_unconfigured: 'The Director key is not configured on the backend.',
  scope_identity_in_use: 'This repo has active work. Change its identity or base ref only after that work is finished.',
}

function apiErrorMessage(error: ApiError, fallback = 'An error occurred'): string {
  if (typeof error.detail === 'string') return operatorErrorMessages[error.detail] ?? error.detail
  if (Array.isArray(error.detail)) {
    const messages = error.detail.map((item) => item.msg).filter(Boolean)
    if (messages.length > 0) return messages.join(', ')
  }
  if (error.detail && !Array.isArray(error.detail) && typeof error.detail === 'object') {
    if (error.detail.message) return error.detail.message
    if (error.detail.msg) return error.detail.msg
  }
  return error.message || fallback
}

export class ApiHttpError extends Error {
  readonly status: number
  readonly blockCode?: string
  readonly code?: string
  readonly provenNonWrite?: boolean

  constructor(message: string, status: number, blockCode?: string, code?: string, provenNonWrite?: boolean) {
    super(message)
    this.name = 'ApiHttpError'
    this.status = status
    this.blockCode = blockCode
    this.code = code
    this.provenNonWrite = provenNonWrite
  }
}

function httpError(response: Response, body: ApiError): ApiHttpError {
  const detail = body.detail
  const blockCode = detail && !Array.isArray(detail) && typeof detail === 'object'
    ? detail.block_code
    : undefined
  return new ApiHttpError(apiErrorMessage(body), response.status, blockCode, detail && !Array.isArray(detail) && typeof detail === 'object' ? detail.code : undefined,
    detail && !Array.isArray(detail) && typeof detail === 'object' ? detail.proven_non_write : undefined)
}

export class ApiClient {
  private async request<T>(
    endpoint: string,
    options?: RequestInit
  ): Promise<T> {
    assertBrowserNativeRequest(endpoint, options?.method ?? 'GET')
    const url = `${API_BASE_URL}${endpoint}`

    try {
      const response = await fetch(url, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...operatorWriteHeaders(options?.method),
          ...scopeUserHeaders(),
          ...options?.headers,
        },
      })

      if (!response.ok) {
        const error: ApiError = await response.json().catch(() => ({
          message: `HTTP ${response.status}: ${response.statusText}`,
        }))
        throw httpError(response, error)
      }

      notifyApiWrite(options?.method)
      return response.json()
    } catch (error) {
      if (error instanceof Error) {
        throw error
      }
      throw new Error('An unknown error occurred', { cause: error })
    }
  }

  async get<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: 'GET' })
  }

  async post<T>(endpoint: string, data?: unknown): Promise<T> {
    return this.request<T>(endpoint, {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async put<T>(endpoint: string, data?: unknown): Promise<T> {
    return this.request<T>(endpoint, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  async delete<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: 'DELETE' })
  }
}

export const api = new ApiClient()

// Helper function for simpler API calls
export async function apiClient<T>(endpoint: string, options?: RequestInit): Promise<T> {
  assertBrowserNativeRequest(endpoint, options?.method ?? 'GET')
    const url = `${API_BASE_URL}${endpoint}`

  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...operatorWriteHeaders(options?.method),
          ...scopeUserHeaders(),
        ...options?.headers,
      },
    })

    if (response.ok) notifyApiWrite(options?.method)

    // Handle 204 No Content responses
    if (response.status === 204) {
      return {} as T
    }

    if (!response.ok) {
      const error: ApiError = await response.json().catch(() => ({
        message: `HTTP ${response.status}: ${response.statusText}`,
      }))
      throw httpError(response, error)
    }

    return response.json()
  } catch (error) {
    if (error instanceof Error) {
      throw error
    }
    throw new Error('An unknown error occurred', { cause: error })
  }
}
