<template>
  <section class="replay-panel" data-testid="replay-workspace">
    <div class="replay-panel-head">
      <div>
        <h3>页面样本回放协同台</h3>
        <p>保留原样本与历史版本结果；回放单、模板切换、幂等重试和处理顺序都在这里可查。</p>
        <p class="scenario-hint" data-testid="scenario-58-48">
          专用样本 #4：同站旧版 v1 提取原价 58，新版 v2 可提取促销价 48。
        </p>
      </div>
      <label class="sample-select">
        <span>选择样本</span>
        <select data-testid="replay-sample-select" :value="selectedId" @change="changeSample">
          <option v-for="sample in samples" :key="sample.id" :value="sample.id">
            #{{ sample.id }} {{ sample.样本批次 }}｜{{ sample.站点名称 }}
          </option>
        </select>
      </label>
    </div>

    <div v-if="loading" class="replay-muted" data-testid="replay-loading">正在读取服务端受理状态…</div>
    <div v-else-if="errorMessage" class="replay-error" data-testid="replay-error">{{ errorMessage }}</div>
    <div v-if="actionMessage" class="replay-success" data-testid="replay-message">{{ actionMessage }}</div>

    <template v-if="state">
      <div class="replay-grid">
        <article class="replay-card" data-testid="original-result-entry">
          <div class="card-title">
            <span>原解析结果</span>
            <span class="badge ok">永不因回放抹掉</span>
          </div>
          <template v-if="state.original_parse_result">
            <p>
              <button
                class="link strong"
                type="button"
                data-testid="open-original-result"
                @click="openResult(state.original_parse_result)"
              >
                {{ state.original_parse_result.result_no }} ·
                模板 {{ state.original_parse_result.template_version }} ·
                价格 ¥{{ state.original_parse_result.price }}
              </button>
            </p>
            <p class="replay-muted">上一轮价格仍可查；样本状态仍为「{{ state.sample.status }}」，可按原样本与版本复查。</p>
          </template>
          <p v-else class="replay-muted">该样本暂无已完成解析结果。</p>
        </article>

        <article class="replay-card" data-testid="active-replay-entry">
          <div class="card-title">
            <span>正在办理的回放</span>
            <span :class="['badge', activeReplay ? 'warn' : 'muted']">
              {{ activeReplay ? activeReplay.status : '无在办单' }}
            </span>
          </div>
          <template v-if="activeReplay">
            <p>
              <button class="link strong" type="button" data-testid="open-active-replay" @click="openReplay(activeReplay.id)">
                {{ activeReplay.id }} · 第 {{ activeReplay.round }} 轮
              </button>
            </p>
            <p class="stage" data-testid="replay-stage">当前阶段：{{ activeReplay.stage }}</p>
          </template>
          <p v-else class="replay-muted">发起回放后，可在这里打开在办单据并查看阶段。</p>
        </article>
      </div>

      <div class="action-console" data-testid="action-console">
        <label class="fault-toggle">
          <input v-model="simulateDisconnect" data-testid="simulate-disconnect" type="checkbox" />
          下一次继续时模拟“服务端已落库、响应断线丢失”
        </label>
        <div class="action-buttons">
          <button class="btn primary" type="button" data-testid="start-replay"
                  aria-label="回放或接续回放已解析样本" :disabled="busy" @click="startReplay">
            {{ activeReplay ? '发起/查找回放单' : '回放/接续回放已解析样本' }}
          </button>
          <button
            v-if="activeReplay && ['待领取', '失败待重试'].includes(activeReplay.status)"
            class="btn primary"
            type="button"
            :data-testid="activeReplay.status === '失败待重试' ? 'retry-replay-primary' : 'continue-replay'"
            :aria-label="activeReplay.status === '失败待重试' ? '断线重试同一笔回放' : '接续回放或继续回放本笔样本'"
            :disabled="busy"
            @click="activeReplay.status === '失败待重试' ? retryReplay() : continueReplay()"
          >
            {{ activeReplay.status === '失败待重试' ? '断线重试同一笔回放' : '继续回放（接续原单并解析）' }}
          </button>
          <button v-else-if="activeReplay?.status === '解析中'" class="btn" type="button" disabled data-testid="replay-accepting">
            服务端受理进度：解析中…
          </button>
          <button
            v-else-if="activeReplay?.status === '待确认'"
            class="btn primary"
            type="button"
            data-testid="confirm-replay"
            :disabled="busy"
            @click="confirmReplay()"
          >
            确认本轮分轮回执
          </button>
          <button
            v-if="activeReplay?.status === '失败待重试'"
            class="btn"
            type="button"
            data-testid="retry-replay"
            :disabled="busy"
            @click="retryReplay()"
          >
            用原单号/幂等键重试
          </button>
          <button class="btn" type="button" data-testid="query-receipts" :disabled="busy" @click="load">
            查询回执：样本字段与处理顺序
          </button>
          <RouterLink class="btn" data-testid="go-template-switch" :to="`/page_parser?sample=${selectedId}`">
            去模板页切换版本
          </RouterLink>
        </div>
        <ol v-if="activeReplay" class="stage-steps" data-testid="stage-steps">
          <li :class="{ done: true, current: activeReplay.status === '待领取' }">1. 入队待领取</li>
          <li :class="{ done: ['解析中','待确认','已确认'].includes(activeReplay.status), current: activeReplay.status === '解析中' }">
            2. 模板领取/解析
          </li>
          <li :class="{ done: ['待确认','已确认'].includes(activeReplay.status), current: activeReplay.status === '待确认' }">
            3. 分轮回执待确认
          </li>
          <li :class="{ done: activeReplay.status === '已确认', current: activeReplay.status === '已确认' }">4. 已确认归档</li>
        </ol>
      </div>

      <div class="detail-grid">
        <section class="detail-block" data-testid="receipt-list">
          <h4>分轮回执（{{ state.receipts.length }}）</h4>
          <div v-for="receipt in state.receipts" :key="receipt.receipt_no" class="receipt-row"
               :data-testid="`receipt-row-${receipt.receipt_no}`"
               :data-status="receipt.status"
               :data-version="receipt.template_version || ''">
            <strong>{{ receipt.receipt_no }}｜{{ receipt.kind }}｜{{ receipt.status }}</strong>
            <span v-if="receipt.template_version">实际模板版本：{{ receipt.template_version }}；价格：¥{{ receipt.price }}</span>
            <p>{{ receipt.message }}</p>
            <span class="replay-muted">下一步：{{ (receipt.next_actions || []).join('、') }}</span>
          </div>
          <p v-if="!state.receipts.length" class="replay-muted">发起并继续回放后，这里会出现分轮回执。</p>
        </section>

        <section class="detail-block" data-testid="parse-results">
          <h4>按版本留存的结构化价格字段（{{ state.parse_results.length }}）</h4>
          <div v-for="result in state.parse_results" :key="result.result_no" class="result-row"
               :data-testid="`parse-result-${result.result_no}`"
               :data-version="result.template_version"
               :data-price="result.price">
            <button class="link" type="button" :data-testid="`open-result-${result.result_no}`" @click="openResult(result)">
              {{ result.result_no }}｜{{ result.template_version }}｜价格 ¥{{ result.price }}｜{{ result.status }}
            </button>
            <span class="field-chips" :data-testid="`fields-${result.result_no}`">
              <em>原价：{{ result.fields?.original_price ?? result.fields?.原价 ?? '—' }}</em>
              <em>促销价：{{ result.fields?.promo_price ?? result.fields?.促销价 ?? '—' }}</em>
              <em>币种：{{ result.fields?.currency ?? 'CNY' }}</em>
            </span>
            <span class="replay-muted">{{ result.template_name }}</span>
          </div>
        </section>

        <section class="detail-block" data-testid="eligible-templates">
          <h4>该站点当前有资格的模板（{{ state.eligible_templates.length }}）</h4>
          <div v-for="template in state.eligible_templates" :key="template.id" class="receipt-row"
               :data-testid="`eligible-template-${template.id}`">
            <strong>#{{ template.id }} {{ template.version }}｜{{ template.status }}</strong>
            <span>{{ template.template_name }}｜{{ template.site }}</span>
          </div>
          <p v-if="!state.eligible_templates.length" class="replay-error">
            当前没有同站已启用模板；其他站点版本不能代替。
          </p>
          <p class="replay-muted">领取时会再次校验站点与启用状态，跨站版本会返回原因和恢复步骤。</p>
        </section>

        <section class="detail-block" data-testid="switch-receipts">
          <h4>模板切换回执（{{ state.switch_receipts.length }}）</h4>
          <div v-for="switchItem in state.switch_receipts" :key="switchItem.id" class="receipt-row"
               :data-testid="`switch-receipt-${switchItem.id}`">
            <strong>#{{ switchItem.sequence }} {{ switchItem.id }}</strong>
            <span>{{ switchItem.old_version }} → {{ switchItem.new_version }}</span>
            <span :class="['badge', switchItem.confirmed ? 'ok' : 'warn']">
              {{ switchItem.status }}/{{ switchItem.ack_status }}
            </span>
            <p>{{ switchItem.message }}</p>
            <button v-if="!switchItem.confirmed" class="link" type="button"
                    :data-testid="`confirm-switch-${switchItem.id}`"
                    @click="confirmSwitch(switchItem.id)">
              确认切换回执
            </button>
          </div>
          <p v-if="!state.switch_receipts.length" class="replay-muted">尚未提交模板切换。</p>
        </section>

        <section class="detail-block" data-testid="recovery-records">
          <h4>失败与恢复记录（{{ state.recovery_records.length }}）</h4>
          <div v-for="(item, index) in state.recovery_records" :key="`${item.replay_id}-${index}`" class="recovery-row">
            <strong>{{ item.replay_id }}｜{{ item.status }}</strong>
            <p>原因：{{ item.failure_reason }}</p>
            <ul>
              <li v-for="step in item.steps || []" :key="step">{{ step }}</li>
            </ul>
          </div>
          <p v-if="!state.recovery_records.length" class="replay-muted">暂无失败记录。</p>
        </section>

        <section class="detail-block" data-testid="ledger-list">
          <h4>两次操作的实际处理顺序</h4>
          <ol class="ledger">
            <li v-for="entry in state.ledger" :key="entry.sequence" :data-testid="`ledger-${entry.sequence}`">
              <strong>#{{ entry.sequence }} {{ entry.operation }}</strong>
              <span>{{ entry.summary }}</span>
            </li>
          </ol>
        </section>
      </div>
    </template>

    <div v-if="detailResult" class="modal-backdrop" data-testid="parse-result-modal" @click.self="detailResult = null">
      <article class="modal replay-modal">
        <h3>原解析结果 {{ detailResult.result_no }}</h3>
        <p>样本：#{{ detailResult.sample_id }}；模板版本：<strong data-testid="modal-result-version">{{ detailResult.template_version }}</strong></p>
        <p>价格：<strong data-testid="modal-result-price">¥{{ detailResult.price }}</strong>；状态：{{ detailResult.status }}</p>
        <pre data-testid="modal-result-fields">{{ JSON.stringify(detailResult.fields, null, 2) }}</pre>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="detailResult = null">关闭</button>
        </div>
      </article>
    </div>

    <div v-if="detailReplay" class="modal-backdrop" data-testid="replay-modal" @click.self="detailReplay = null">
      <article class="modal replay-modal">
        <h3>正在办理的回放 {{ detailReplay.id }}</h3>
        <p data-testid="modal-replay-stage">当前阶段：{{ detailReplay.stage }}</p>
        <p>状态：<strong>{{ detailReplay.status }}</strong>；实际模板版本：{{ detailReplay.template_version ?? '尚未领取' }}</p>
        <div v-if="detailReplay.recovery" class="replay-error">
          <p>失败原因：{{ detailReplay.recovery.failure_reason }}</p>
          <ul><li v-for="step in detailReplay.recovery.steps" :key="step">{{ step }}</li></ul>
        </div>
        <h4>分轮回执</h4>
        <div v-for="receipt in detailReplay.receipts || []" :key="receipt.receipt_no" class="receipt-row"
             :data-testid="`receipt-${receipt.receipt_no}`">
          <strong>{{ receipt.receipt_no }}｜{{ receipt.kind }}｜{{ receipt.status }}</strong>
          <p>{{ receipt.message }}</p>
        </div>
        <h4>尝试记录</h4>
        <ol>
          <li v-for="attempt in detailReplay.attempts || []" :key="attempt.attempt">
            {{ attempt.state }}：{{ attempt.reason }}
          </li>
        </ol>
        <div class="page-actions">
          <button v-if="['待领取','失败待重试'].includes(detailReplay.status)" class="btn primary" type="button"
                  data-testid="modal-continue-replay" @click="continueReplay(detailReplay.id)">
            继续/重试本笔回放
          </button>
          <button v-if="detailReplay.status === '待确认'" class="btn primary" type="button"
                  data-testid="modal-confirm-replay" @click="confirmReplay(detailReplay.id)">
            确认本轮回执
          </button>
          <button class="btn" type="button" @click="detailReplay = null">关闭</button>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { replayApi, type FlowRecord, type SampleReplayState } from '@/api/replay'
import type { EntryRow } from '@/data/types'

const props = defineProps<{
  samples: EntryRow[]
  sampleId: number
  refreshKey?: number
  initialReplay?: string
  initialView?: string
}>()
const emit = defineEmits<{ (event: 'update:sampleId', sampleId: number): void }>()

const selectedId = ref<number>(props.sampleId)
const state = ref<SampleReplayState | null>(null)
const loading = ref(false)
const busy = ref(false)
const errorMessage = ref('')
const actionMessage = ref('')
const simulateDisconnect = ref(false)
const detailResult = ref<FlowRecord | null>(null)
const detailReplayId = ref<string>('')

const activeReplay = computed(() => state.value?.active_replay ?? null)
const detailReplay = computed<FlowRecord | null>(() => {
  if (!state.value || !detailReplayId.value) return null
  return state.value.replays.find((item) => item.id === detailReplayId.value) ?? null
})

watch(() => props.sampleId, (value) => {
  selectedId.value = value
  void load()
})
watch(() => props.refreshKey, () => void load())
watch(() => props.initialReplay, (value) => {
  if (value) {
    detailReplayId.value = value
    void load().then(() => openReplay(value))
  }
})

function changeSample(event: Event) {
  selectedId.value = Number((event.target as HTMLSelectElement).value)
  detailReplayId.value = ''
  emit('update:sampleId', selectedId.value)
  void load()
}

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    state.value = await replayApi.sampleState(selectedId.value)
    if (props.initialReplay) detailReplayId.value = props.initialReplay
    if (props.initialView === 'result' && state.value.original_parse_result) {
      detailResult.value = state.value.original_parse_result
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回放状态读取失败'
  } finally {
    loading.value = false
  }
}

async function runFlow(task: () => Promise<FlowRecord>, successFallback: string) {
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await task()
    if (result.ok === false) {
      errorMessage.value = result.message || successFallback
    } else {
      actionMessage.value = result.message || successFallback
    }
    await load()
    // 只有回执弹窗已经打开时才跟随响应刷新；普通按钮操作不自动弹模态框，避免遮挡后续流程。
    if (detailReplayId.value) {
      const replayId = result.replay?.id ?? result.active_replay?.id
      if (replayId) detailReplayId.value = replayId
    }
    return result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : successFallback
    await load()
    return null
  } finally {
    busy.value = false
  }
}

function startReplay() {
  return runFlow(() => replayApi.start(selectedId.value, { actor: '值班员' }), '回放已入队')
}

function currentReplayId(explicit?: string) {
  return explicit || detailReplayId.value || activeReplay.value?.id || ''
}

function continueReplay(explicit?: string) {
  const replayId = currentReplayId(explicit)
  if (!replayId) return startReplay()
  const target = state.value?.replays.find((item) => item.id === replayId)
  const key = target?.idempotency_key || activeReplay.value?.idempotency_key
  // 故障开关只作用于“第一次继续”，避免值班员点重试时再次人为注入断线。
  const fault = simulateDisconnect.value && target?.status === '待领取' ? 'response_lost' : undefined
  if (fault) simulateDisconnect.value = false
  return runFlow(
    () => replayApi.continue(replayId, {
      actor: '值班员',
      ...(fault ? { fault } : {}),
    }, key),
    '回放已继续',
  )
}

function retryReplay(explicit?: string) {
  const replayId = currentReplayId(explicit)
  if (!replayId) return Promise.resolve(null)
  const key = state.value?.replays.find((item) => item.id === replayId)?.idempotency_key
  return runFlow(() => replayApi.retry(replayId, { actor: '值班员' }, key), '重试完成')
}

function confirmReplay(explicit?: string) {
  const replayId = currentReplayId(explicit)
  if (!replayId) return Promise.resolve(null)
  return runFlow(() => replayApi.confirm(replayId), '回执已确认')
}

function confirmSwitch(switchId: string) {
  return runFlow(() => replayApi.confirmSwitch(switchId), '切换回执已确认')
}

function openResult(result: FlowRecord) {
  detailResult.value = result
}

function openReplay(replayId: string) {
  detailReplayId.value = replayId
}

void load().then(() => {
  if (props.initialReplay) detailReplayId.value = props.initialReplay
  if (props.initialView === 'result' && state.value?.original_parse_result) {
    detailResult.value = state.value.original_parse_result
  }
})
</script>

<style scoped>
.replay-panel { background: #f8fafc; border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin: 12px 0 16px; }
.replay-panel-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.replay-panel-head h3 { margin: 0 0 4px; font-size: 17px; }
.replay-panel-head p { margin: 0; color: var(--muted); font-size: 13px; }
.scenario-hint { margin-top: 4px !important; color: #175cd3 !important; font-weight: 600; }
.sample-select { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 4px; min-width: 260px; }
.sample-select select { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; }
.replay-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0; }
.replay-card, .detail-block, .action-console { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
.card-title { display: flex; justify-content: space-between; align-items: center; font-weight: 700; margin-bottom: 8px; }
.badge { display: inline-flex; border-radius: 999px; padding: 2px 8px; font-size: 12px; border: 1px solid var(--border); }
.badge.ok { background: #ecfdf3; color: #027a48; border-color: #abefc6; }
.badge.warn { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.badge.muted { background: #f2f4f7; color: #475467; }
.strong { font-weight: 700; font-size: 14px; }
.stage { margin: 6px 0 0; color: #1d4ed8; font-size: 13px; }
.action-console { margin-bottom: 12px; }
.fault-toggle { display: flex; gap: 6px; align-items: center; font-size: 13px; margin-bottom: 10px; }
.action-buttons { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.action-buttons .btn { text-decoration: none; display: inline-flex; align-items: center; }
.stage-steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; padding: 0; margin: 12px 0 0; list-style: none; }
.stage-steps li { background: #f2f4f7; border-radius: 6px; padding: 7px; font-size: 12px; color: #667085; }
.stage-steps li.done { background: #eff8ff; color: #175cd3; }
.stage-steps li.current { outline: 2px solid #84caff; font-weight: 700; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.detail-block h4 { margin: 0 0 10px; }
.result-row, .receipt-row, .recovery-row { border-top: 1px solid #eef2f6; padding: 8px 0; display: flex; flex-direction: column; gap: 3px; font-size: 13px; }
.receipt-row span, .replay-muted { color: var(--muted); font-size: 12px; }
.field-chips { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; }
.field-chips em { font-style: normal; background: #eff8ff; color: #175cd3; border-radius: 999px; padding: 2px 8px; }
.recovery-row { border-left: 3px solid #f79009; padding-left: 8px; }
.ledger { margin: 0; padding-left: 20px; max-height: 260px; overflow: auto; }
.ledger li { margin-bottom: 8px; font-size: 13px; display: flex; flex-direction: column; }
.replay-error { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; border-radius: 8px; padding: 8px 10px; font-size: 13px; white-space: pre-wrap; }
.replay-success { color: #027a48; background: #ecfdf3; border: 1px solid #abefc6; border-radius: 8px; padding: 8px 10px; font-size: 13px; margin: 8px 0; }
.replay-modal { width: min(720px, 100%); }
.replay-modal pre { background: #101828; color: #d0d5dd; padding: 12px; border-radius: 8px; overflow: auto; font-size: 12px; }
@media (max-width: 980px) { .replay-grid, .detail-grid { grid-template-columns: 1fr; } .stage-steps { grid-template-columns: 1fr 1fr; } }
</style>
