/**
 * WebSocket Store
 * 
 * Manages WebSocket connection and real-time data updates
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import wsClient, { type WebSocketStatus } from '@/utils/websocket'

export interface RealtimeUpdate {
  type: string
  data: unknown
  timestamp: number
}

export const useWebSocketStore = defineStore('websocket', () => {
  const status = ref<WebSocketStatus>('disconnected')
  const connected = computed(() => status.value === 'connected')
  
  const updates = ref<RealtimeUpdate[]>([])
  const maxUpdates = 100

  const statsUpdate = ref<unknown>(null)
  const accountStatusUpdate = ref<unknown>(null)
  const violationUpdate = ref<unknown>(null)
  const messageUpdate = ref<unknown>(null)

  const clientId = ref<number>(0)

  function setupHandlers(): void {
    wsClient.on('connected', () => {
      status.value = 'connected'
    })

    wsClient.on('disconnected', () => {
      status.value = 'disconnected'
    })

    wsClient.on('error', () => {
      status.value = 'error'
    })

    wsClient.on('stats:update', (data) => {
      statsUpdate.value = data
      pushUpdate('stats:update', data)
    })

    wsClient.on('account:status', (data) => {
      accountStatusUpdate.value = data
      pushUpdate('account:status', data)
    })

    wsClient.on('violation:new', (data) => {
      violationUpdate.value = data
      pushUpdate('violation:new', data)
    })

    wsClient.on('message:new', (data) => {
      messageUpdate.value = data
      pushUpdate('message:new', data)
    })
  }

  function pushUpdate(type: string, data: unknown): void {
    updates.value.push({
      type,
      data,
      timestamp: Date.now(),
    })
    if (updates.value.length > maxUpdates) {
      updates.value.shift()
    }
  }

  async function connect(baseUrl: string): Promise<void> {
    const token = localStorage.getItem('token')
    if (!token) {
      throw new Error('Missing authentication token')
    }

    clientId.value = Date.now()
    setupHandlers()
    await wsClient.connect(baseUrl, clientId.value, token)
    status.value = 'connected'
  }

  function disconnect(): void {
    wsClient.disconnect()
    status.value = 'disconnected'
  }

  function subscribe(channel: string): void {
    wsClient.subscribe(channel)
  }

  function unsubscribe(channel: string): void {
    wsClient.unsubscribe(channel)
  }

  return {
    status,
    connected,
    updates,
    statsUpdate,
    accountStatusUpdate,
    violationUpdate,
    messageUpdate,
    clientId,
    connect,
    disconnect,
    subscribe,
    unsubscribe,
  }
})
