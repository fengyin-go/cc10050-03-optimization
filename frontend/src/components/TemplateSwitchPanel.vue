<template>
  <section class="switch-panel" data-testid="template-switch-workspace">
    <div class="switch-head">
      <div>
        <h3>模板切换与回放队列受理</h3>
        <p>切换只允许发生在同站点版本之间；切换回执记录顺序，样本页可继续同一笔回放。</p>
      </div>
      <label class="template-select">
        <span>要启用的新版本</span>
        <select v-model.number="templateId" data-testid="switch-template-select">
          <option v-for="template in siteTemplates" :key="template.id" :value="Number(template.id)">
            #{{ template.id }} {{ template.模板名称 }}（{{ template.解析版本 }} / {{ template.status }}）
          </option>
        </select>
      </label>
    </div>

    <div class="switch-actions">
      <button class="btn primary" type="button" data-testid="submit-template-switch" :disabled="busy" @click="submitSwitch">
        切换模板：废弃同站旧版并启用所选新版
      </button>
      <button class="btn" type="button" data-testid="claim-next-sample" :disabled="busy" @click="claimNext">
        用所选模板从回放队列领取样本
      </button>
      <RouterLink class="btn" data-testid="back-to-sample-result" :to="resultLink">
        回样本页打开原解析结果
      </RouterLink>
      <RouterLink v-if="continueLink" class="btn primary" data-testid="back-to-active-replay" :to="continueLink">
        回样本页继续正在办理的回放
      </RouterLink>
    </div>

    <p v-if="message" class="switch-success" data-testid="switch-message">{{ message }}</p>
    <p v-if="errorMessage" class="switch-error" data-testid="switch-error">{{ errorMessage }}</p>

    <div class="template-grid">
      <article v-for="template in siteTemplates" :key="template.id" class="template-card"
               :data-testid="`template-card-${template.id}`"
               :data-version="template.解析版本"
               :data-status="template.status">
        <strong>{{ template.模板名称 }}</strong>
        <span>版本：{{ template.解析版本 }}</span>
        <span>站点：{{ template.适用站点 }}</span>
        <span :class="['badge', template.status === '已启用' ? 'ok' : template.status === '草稿' ? 'warn' : 'muted']">
          {{ template.status }}
        </span>
      </article>
    </div>

    <section class="switch-list" data-testid="template-switch-list">
      <h4>模板切换回执</h4>
      <article v-for="item in switches" :key="item.id" class="switch-receipt"
               :data-testid="`template-switch-${item.id}`"
               :data-sequence="item.sequence">
        <div class="receipt-head">
          <strong>#{{ item.sequence }} {{ item.id }}｜{{ item.old_version }} → {{ item.new_version }}</strong>
          <span :class="['badge', item.confirmed ? 'ok' : 'warn']">{{ item.status }}/{{ item.ack_status }}</span>
        </div>
        <p>{{ item.message }}</p>
        <p class="muted">站点：{{ item.site }}；服务端生效状态：{{ item.effective_status }}</p>
        <div class="receipt-actions">
          <button v-if="!item.confirmed" class="link" type="button"
                  :data-testid="`ack-switch-${item.id}`" @click="acknowledge(item.id)">
            确认切换回执
          </button>
          <RouterLink class="link" :to="`/sample_store?sample=${item.sample_id || defaultSampleId}&view=result`">
            查看该样本原解析结果
          </RouterLink>
          <RouterLink v-if="activeReplayId" class="link"
                      :to="`/sample_store?sample=${item.sample_id || defaultSampleId}&replay=${activeReplayId}`">
            打开正在办理的回放 {{ activeReplayId }}
          </RouterLink>
        </div>
      </article>
      <p v-if="!switches.length" class="muted">还没有切换回执。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { postJson } from '@/api/client'
import { replayApi, type FlowRecord } from '@/api/replay'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ templates: EntryRow[]; defaultSampleId?: number; initialTemplateId?: number }>()
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')
const switches = ref<FlowRecord[]>([])
const templateId = ref<number>(props.initialTemplateId ?? 5)
const activeReplayId = ref('')

const defaultSampleId = props.defaultSampleId ?? 4
const replayQuerySample = defaultSampleId
const resultLink = `/sample_store?sample=${replayQuerySample}&view=result`
const continueLink = computed(() => activeReplayId.value
  ? `/sample_store?sample=${replayQuerySample}&replay=${encodeURIComponent(activeReplayId.value)}`
  : '')

const siteTemplates = computed(() => {
  const site = props.templates.find((item) => Number(item.id) === templateId.value)?.['适用站点']
    ?? '云选数码官方店'
  return props.templates.filter((item) => item['适用站点'] === site)
})

async function loadSwitches() {
  try {
    const payload = await replayApi.switches()
    switches.value = payload.items
    const state = await replayApi.sampleState(defaultSampleId)
    activeReplayId.value = state.active_replay?.id ?? ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换回执读取失败'
  }
}

async function submitSwitch() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await replayApi.switchTemplate(templateId.value, {
      actor: '值班员',
      sample_id: defaultSampleId,
    })
    if (result.ok === false) {
      errorMessage.value = result.message
    } else {
      message.value = result.message
    }
    await loadSwitches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模板切换失败'
  } finally {
    busy.value = false
  }
}

async function acknowledge(switchId: string) {
  const result = await replayApi.confirmSwitch(switchId)
  message.value = result.message
  await loadSwitches()
}

async function claimNext() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await postJson(`/api/page_parser/${templateId.value}/claim-next`, { actor: '页面解析模板' })
    if (result.ok === false) errorMessage.value = result.message
    else message.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取队列样本失败'
  } finally {
    busy.value = false
  }
}

onMounted(loadSwitches)
</script>

<style scoped>
.switch-panel { background: #f8fafc; border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin: 12px 0 16px; }
.switch-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.switch-head h3 { margin: 0 0 4px; font-size: 17px; }
.switch-head p { margin: 0; color: var(--muted); font-size: 13px; }
.template-select { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 280px; }
.template-select select { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; }
.switch-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.switch-actions .btn { text-decoration: none; display: inline-flex; align-items: center; }
.switch-success { background: #ecfdf3; border: 1px solid #abefc6; color: #027a48; border-radius: 8px; padding: 8px 10px; font-size: 13px; }
.switch-error { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; border-radius: 8px; padding: 8px 10px; font-size: 13px; }
.template-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 12px 0; }
.template-card { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 5px; font-size: 13px; }
.badge { align-self: flex-start; border-radius: 999px; padding: 2px 8px; font-size: 12px; border: 1px solid var(--border); }
.badge.ok { background: #ecfdf3; color: #027a48; border-color: #abefc6; }
.badge.warn { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.badge.muted { background: #f2f4f7; color: #475467; }
.switch-list h4 { margin-bottom: 8px; }
.switch-receipt { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 10px; margin-bottom: 8px; }
.receipt-head { display: flex; justify-content: space-between; gap: 8px; }
.switch-receipt p { margin: 6px 0; font-size: 13px; }
.muted { color: var(--muted); font-size: 12px; }
.receipt-actions { display: flex; gap: 12px; flex-wrap: wrap; }
@media (max-width: 980px) { .switch-head { flex-direction: column; } .template-grid { grid-template-columns: 1fr; } }
</style>
