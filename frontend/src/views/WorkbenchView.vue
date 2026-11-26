<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <SampleReplayPanel
      v-if="meta.key === 'sample_store'"
      :key="samplePanelKey"
      :samples="rows"
      :sample-id="flowSampleId"
      :initial-replay="initialReplayId"
      :initial-view="initialView"
      @update:sample-id="rememberSample"
    />
    <TemplateSwitchPanel
      v-else-if="meta.key === 'page_parser'"
      :key="templatePanelKey"
      :templates="rows"
      :default-sample-id="flowSampleId"
      :initial-template-id="flowTemplateId"
    />

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import SampleReplayPanel from '@/components/SampleReplayPanel.vue'
import TemplateSwitchPanel from '@/components/TemplateSwitchPanel.vue'
import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const route = useRoute()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const storedSampleId = Number(window.sessionStorage.getItem('sample-flow:last-id'))
const flowSampleId = ref(Number(route.query.sample) || (storedSampleId > 0 ? storedSampleId : 4))
const initialReplayId = ref(typeof route.query.replay === 'string' ? route.query.replay : '')
const initialView = ref(typeof route.query.view === 'string' ? route.query.view : '')
const samplePanelKey = ref(0)
const templatePanelKey = ref(0)
const flowTemplateId = ref(Number(route.query.template) || 5)

function rememberSample(sampleId: number) {
  flowSampleId.value = sampleId
  window.sessionStorage.setItem('sample-flow:last-id', String(sampleId))
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

const SAMPLE_FLOW_ACTIONS = new Set([
  '回放', '发起回放', '接续回放', '继续回放', '重试回放', '确认回执', '查询回执',
])
const TEMPLATE_FLOW_ACTIONS = new Set(['切换模板', '切换版本', '领取回放样本'])

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action) as any
    if (meta.key === 'sample_store' && SAMPLE_FLOW_ACTIONS.has(action)) {
      rememberSample(Number(row.id))
      // 行内动作只刷新协同台并展示主操作，不自动弹模态框，避免遮挡后续按钮；
      // 从模板页带 ?replay=... 返回时才直接打开对应回放。
      samplePanelKey.value += 1
    }
    if (meta.key === 'page_parser' && TEMPLATE_FLOW_ACTIONS.has(action)) {
      flowTemplateId.value = Number(row.id)
      templatePanelKey.value += 1
    }
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    if (meta.key === 'sample_store' && SAMPLE_FLOW_ACTIONS.has(action)) {
      rememberSample(Number(row.id))
      samplePanelKey.value += 1
    }
    if (meta.key === 'page_parser' && TEMPLATE_FLOW_ACTIONS.has(action)) {
      flowTemplateId.value = Number(row.id)
      templatePanelKey.value += 1
    }
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

watch(() => [route.query.sample, route.query.replay, route.query.view], () => {
  const querySample = Number(route.query.sample)
  if (querySample) rememberSample(querySample)
  initialReplayId.value = typeof route.query.replay === 'string' ? route.query.replay : ''
  initialView.value = typeof route.query.view === 'string' ? route.query.view : ''
  samplePanelKey.value += 1
})

onMounted(reload)
</script>
