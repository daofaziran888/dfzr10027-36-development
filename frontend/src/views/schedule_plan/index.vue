<template>
  <section class="page" data-module="schedule_plan">
    <header class="page-head">
      <div>
        <h2>调度编排管理</h2>
        <p class="page-desc">维护分析任务的定时调度计划、依赖检查和失败通知机制。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记调度计划</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出调度编排清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`run-open-${row.id}`"
              @click="openRun(row)"
            >
              本轮执行
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无调度编排数据，可先登记调度计划</td>
        </tr>
      </tbody>
    </table>

    <section v-if="runPlan" class="run-panel" data-testid="run-detail">
      <header class="run-head">
        <div>
          <h3>本轮执行详情 · {{ runPlan["计划编号"] }}</h3>
          <p class="page-desc">
            轮次 {{ detail?.轮次 ?? '—' }} · 窗口 {{ detail?.窗口序号 ?? '—' }} ·
            可并行 {{ detail?.并发上限 ?? runPlan["并发上限"] }} · 依赖检查 {{ detail?.依赖检查 ? '已开启' : '未开启' }}
          </p>
        </div>
        <div class="page-actions">
          <button class="btn primary" data-testid="run-start" type="button" :disabled="busy" @click="startRun">
            启动本轮
          </button>
          <button
            class="btn"
            data-testid="run-resume"
            type="button"
            :disabled="busy || !detail || detail.状态 === '已完成'"
            @click="resumeRun"
          >
            恢复执行
          </button>
          <button class="btn ghost" data-testid="run-close" type="button" @click="closeRun">收起</button>
        </div>
      </header>

      <div class="stat-row">
        <article v-for="item in runStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value" :data-testid="`run-stat-${item.key}`">{{ item.value }}</strong>
        </article>
      </div>

      <p v-if="!detail" class="empty-state" data-testid="run-empty">该计划还没有本轮执行流水，点「启动本轮」开始。</p>

      <template v-else>
        <table class="data-table run-task-table">
          <thead>
            <tr>
              <th>聚合任务</th>
              <th>目标指标</th>
              <th>依赖上游</th>
              <th>状态</th>
              <th>产物版本</th>
              <th>取数依据</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="task in detail.任务"
              :key="task.聚合任务"
              :data-testid="`run-task-${task.聚合任务}`"
              :data-task-status="task.状态"
            >
              <td>{{ task.聚合任务 }}</td>
              <td>{{ task.目标指标 ?? '—' }}</td>
              <td>{{ task.依赖上游.length ? task.依赖上游.join('、') : '—' }}</td>
              <td>
                <span class="task-badge" :class="badgeClass(task.状态)">{{ task.状态 }}</span>
              </td>
              <td>{{ task.产物版本 ?? '—' }}</td>
              <td>{{ snapshotText(task.依赖上游, detail.任务) }}</td>
              <td>{{ task.阻断原因 ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4 class="run-subtitle">本轮派生产物</h4>
        <table class="data-table" data-testid="run-products">
          <thead>
            <tr><th>产物编号</th><th>产物版本</th><th>聚合任务</th><th>目标指标</th><th>指标结果</th><th>产出时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.派生产物" :key="item.产物编号" :data-testid="`run-product-${item.聚合任务}`">
              <td>{{ item.产物编号 }}</td>
              <td>{{ item.产物版本 }}</td>
              <td>{{ item.聚合任务 }}</td>
              <td>{{ item.目标指标 }}</td>
              <td>{{ item.指标结果 }} {{ item.指标单位 }}</td>
              <td>{{ item.产出时间 }}</td>
            </tr>
            <tr v-if="!detail.派生产物.length">
              <td colspan="6" class="empty-state">本轮还没有产生产物</td>
            </tr>
          </tbody>
        </table>

        <h4 class="run-subtitle">执行流水</h4>
        <ul class="run-log" data-testid="run-log">
          <li v-for="(item, index) in detail.阶段记录" :key="index">
            <span class="run-log-time">{{ item.时间 }}</span>
            <span>第 {{ item.窗口 }} 窗口 · {{ item.动作 }} · {{ item.说明 }}</span>
          </li>
        </ul>
      </template>
      <p v-if="runMessage" class="page-foot" data-testid="run-message">{{ runMessage }}</p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记调度计划</h3>
        <label v-for="field in columns" :key="field" class="create-field">
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
      <span>共 {{ total }} 条调度编排记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type RunTask = {
  聚合任务: string
  目标指标: string | null
  依赖上游: string[]
  状态: string
  产物版本: string | null
  产物编号: string | null
  取数来源: string[]
  阻断原因: string | null
}

type RunProduct = {
  产物编号: string
  产物版本: string
  聚合任务: string
  目标指标: string | null
  指标结果: number | string | null
  指标单位: string | null
  产出时间: string | null
  上游快照: Record<string, string | null>
}

type RunDetail = {
  计划id: number
  计划编号: string
  轮次: number
  状态: string
  窗口序号: number
  并发上限: number
  窗口名额: number
  剩余名额: number
  依赖检查: boolean
  任务: RunTask[]
  派生产物: RunProduct[]
  已交付: string[]
  待计算: string[]
  依赖阻塞: string[]
  阶段记录: { 时间: string; 窗口: number; 动作: string; 说明: string }[]
}

const ENDPOINT = '/api/schedule_plan'
const columns = ["计划编号", "关联任务", "Cron表达式", "时区设置", "失败通知", "并发上限", "依赖检查", "下次执行"]
const actions = ["激活计划", "立即执行", "停用计划"]
const statuses = ["待激活", "运行中", "已完成", "已停用"]
const metricNames = ["计划总数", "运行中计划", "待激活计划"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

const runPlan = ref<Row | null>(null)
const detail = ref<RunDetail | null>(null)
const runMessage = ref('')
const busy = ref(false)

const runStats = computed(() => [
  { key: 'status', label: '本轮状态', value: detail.value?.状态 ?? '—' },
  { key: 'delivered', label: '已交付', value: detail.value ? detail.value.已交付.length : 0 },
  { key: 'pending', label: '待计算', value: detail.value ? detail.value.待计算.length : 0 },
  { key: 'blocked', label: '依赖阻塞', value: detail.value ? detail.value.依赖阻塞.length : 0 },
  { key: 'quota', label: '剩余名额', value: detail.value?.剩余名额 ?? '—' },
])

function badgeClass(status: string) {
  return {
    已交付: 'badge-delivered',
    等待名额: 'badge-waiting',
    依赖阻塞: 'badge-blocked',
    待计算: 'badge-pending',
  }[status] ?? 'badge-pending'
}

function snapshotText(deps: string[], tasks: RunTask[]) {
  if (!deps.length) return '—'
  return deps
    .map((dep) => {
      const upstream = tasks.find((task) => task.聚合任务 === dep)
      const version = upstream ? upstream.产物版本 : `${dep}·本轮校验`
      return `${dep}@${version ?? '未交付'}`
    })
    .join('；')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'schedule_plan.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('调度编排动作未生效，请稍后重试')
    }
    const payload = await response.json()
    await reload()
    if (action === '立即执行' && runPlan.value?.id === row.id) {
      await loadRun()
      runMessage.value = payload.message ?? ''
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度编排操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('调度计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度编排列表读取失败'
  }
}

async function openRun(row: Row) {
  runPlan.value = row
  runMessage.value = ''
  await loadRun()
}

function closeRun() {
  runPlan.value = null
  detail.value = null
  runMessage.value = ''
}

async function loadRun() {
  if (!runPlan.value) return
  try {
    const response = await request(`${ENDPOINT}/${runPlan.value.id}/runs/current`)
    if (response.status === 404) {
      detail.value = null
      return
    }
    if (!response.ok) throw new Error('本轮执行详情读取失败')
    detail.value = (await response.json()) as RunDetail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '本轮执行详情读取失败'
  }
}

async function startRun() {
  if (!runPlan.value) return
  busy.value = true
  runMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${runPlan.value.id}/runs`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '启动本轮失败')
    detail.value = payload.entry as RunDetail
    runMessage.value = payload.message
  } catch (error) {
    runMessage.value = error instanceof Error ? error.message : '启动本轮失败'
  } finally {
    busy.value = false
  }
}

async function resumeRun() {
  if (!runPlan.value) return
  busy.value = true
  runMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${runPlan.value.id}/runs/resume`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '恢复执行失败')
    detail.value = payload.entry as RunDetail
    runMessage.value = payload.message
  } catch (error) {
    runMessage.value = error instanceof Error ? error.message : '恢复执行失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>
