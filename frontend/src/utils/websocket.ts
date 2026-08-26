/**
 * WebSocket client for real-time communication
 */

import { ref, readonly } from 'vue'

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

export interface WebSocketMessage {
  type: string
  data?: unknown
  [key: string]: unknown
}

export type MessageHandler = (data: unknown) => void

class WebSocketClient {
  private ws: WebSocket | null = null
  private baseUrl: string = ''
  private url: string = ''
  private clientId: number = 0
  private token: string = ''
  private handlers: Map<string, Set<MessageHandler>> = new Map()
  private subscriptions: Set<string> = new Set(['stats:update'])
  private reconnectAttempts: number = 0
  private maxReconnectAttempts: number = 5
  private reconnectDelay: number = 3000
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null

  private _status = ref<WebSocketStatus>('disconnected')
  public status = readonly(this._status)

  private _lastMessage = ref<WebSocketMessage | null>(null)
  public lastMessage = readonly(this._lastMessage)

  constructor() {
    this.handlers.set('*', new Set())
  }

  connect(baseUrl: string, clientId: number, token: string): Promise<void> {
    return new Promise((resolve, reject) => {
      if (!token) {
        reject(new Error('Missing authentication token'))
        return
      }

      this.baseUrl = baseUrl
      this.clientId = clientId
      this.token = token
      const params = new URLSearchParams({
        client_id: String(clientId),
        token,
      })
      this.url = `${this.toWebSocketBaseUrl(baseUrl)}/ws/connect?${params.toString()}`
      this._status.value = 'connecting'

      try {
        this.ws = new WebSocket(this.url)

        this.ws.onopen = () => {
          console.log('[WebSocket] Connected')
          this._status.value = 'connected'
          this.reconnectAttempts = 0
          this.startPing()
          this.subscriptions.forEach((channel) => {
            this.send({ type: 'subscribe', channel })
          })
          resolve()
        }

        this.ws.onmessage = (event) => {
          try {
            const message: WebSocketMessage = JSON.parse(event.data)
            this._lastMessage.value = message
            this.handleMessage(message)
          } catch (e) {
            console.error('[WebSocket] Failed to parse message:', e)
          }
        }

        this.ws.onerror = (error) => {
          console.error('[WebSocket] Error:', error)
          this._status.value = 'error'
          reject(error)
        }

        this.ws.onclose = () => {
          console.log('[WebSocket] Disconnected')
          this._status.value = 'disconnected'
          this.stopPing()
          this.scheduleReconnect()
        }
      } catch (error) {
        this._status.value = 'error'
        reject(error)
      }
    })
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.stopPing()
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this._status.value = 'disconnected'
  }

  private handleMessage(message: WebSocketMessage): void {
    const { type, data } = message

    const typeHandlers = this.handlers.get(type)
    if (typeHandlers) {
      typeHandlers.forEach((handler) => {
        try {
          handler(data)
        } catch (e) {
          console.error(`[WebSocket] Handler error for type ${type}:`, e)
        }
      })
    }

    const wildcardHandlers = this.handlers.get('*')
    if (wildcardHandlers) {
      wildcardHandlers.forEach((handler) => {
        try {
          handler(message)
        } catch (e) {
          console.error('[WebSocket] Wildcard handler error:', e)
        }
      })
    }
  }

  on(type: string, handler: MessageHandler): void {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, new Set())
    }
    this.handlers.get(type)!.add(handler)
  }

  off(type: string, handler?: MessageHandler): void {
    if (!this.handlers.has(type)) return

    if (handler) {
      this.handlers.get(type)!.delete(handler)
    } else {
      this.handlers.get(type)!.clear()
    }
  }

  private send(message: WebSocketMessage): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message))
      return true
    }
    return false
  }

  subscribe(channel: string): boolean {
    this.subscriptions.add(channel)
    return this.send({ type: 'subscribe', channel })
  }

  unsubscribe(channel: string): boolean {
    this.subscriptions.delete(channel)
    return this.send({ type: 'unsubscribe', channel })
  }

  ping(): boolean {
    return this.send({ type: 'ping' })
  }

  private startPing(): void {
    this.pingTimer = setInterval(() => {
      this.ping()
    }, 30000)
  }

  private stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log('[WebSocket] Max reconnect attempts reached')
      return
    }

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
    }

    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts)
    console.log(`[WebSocket] Scheduling reconnect in ${delay}ms (attempt ${this.reconnectAttempts + 1})`)

    this.reconnectTimer = setTimeout(() => {
      this.reconnectAttempts++
      this.connect(this.baseUrl, this.clientId, this.token).catch(() => {
        this.scheduleReconnect()
      })
    }, delay)
  }

  private toWebSocketBaseUrl(baseUrl: string): string {
    if (baseUrl.startsWith('http://') || baseUrl.startsWith('https://')) {
      return baseUrl.replace('http://', 'ws://').replace('https://', 'wss://').replace(/\/$/, '')
    }

    const normalizedBaseUrl = baseUrl.startsWith('/') ? baseUrl : `/${baseUrl}`
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}${normalizedBaseUrl}`.replace(/\/$/, '')
  }

  isConnected(): boolean {
    return this._status.value === 'connected'
  }
}

export const wsClient = new WebSocketClient()

export default wsClient
