import { fetchJson, postJson } from './client'

export type FlowRecord = Record<string, any>

export type SampleReplayState = {
  ok: boolean
  message?: string
  sample: FlowRecord
  parse_results: FlowRecord[]
  latest_parse_result?: FlowRecord | null
  original_parse_result?: FlowRecord | null
  replays: FlowRecord[]
  active_replay?: FlowRecord | null
  receipts: FlowRecord[]
  switch_receipts: FlowRecord[]
  ledger: FlowRecord[]
  recovery_records: FlowRecord[]
  eligible_templates: FlowRecord[]
}

export const replayApi = {
  sampleState(sampleId: number | string) {
    return fetchJson<SampleReplayState>(`/api/sample_store/${sampleId}/replay-state`)
  },
  start(sampleId: number | string, body: FlowRecord = {}) {
    return postJson(`/api/sample_store/${sampleId}/replays`, body)
  },
  continueSample(sampleId: number | string, body: FlowRecord = {}, idempotencyKey?: string) {
    return postJson(
      `/api/sample_store/${sampleId}/continue`,
      body,
      idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {},
    )
  },
  continue(replayId: string, body: FlowRecord = {}, idempotencyKey?: string) {
    return postJson(
      `/api/replays/${encodeURIComponent(replayId)}/continue`,
      body,
      idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {},
    )
  },
  retry(replayId: string, body: FlowRecord = {}, idempotencyKey?: string) {
    return postJson(
      `/api/replays/${encodeURIComponent(replayId)}/retry`,
      body,
      idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {},
    )
  },
  confirm(replayId: string) {
    return postJson(`/api/replays/${encodeURIComponent(replayId)}/confirm`)
  },
  switchTemplate(templateId: number | string, body: FlowRecord = {}) {
    return postJson(`/api/page_parser/${templateId}/switch`, body)
  },
  switches() {
    return fetchJson<{ items: FlowRecord[] }>('/api/template-switches')
  },
  confirmSwitch(switchId: string) {
    return postJson(`/api/template-switches/${encodeURIComponent(switchId)}/confirm`)
  },
}
