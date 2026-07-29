<script setup lang="ts">
import ChartComponent from '@/views/chat/component/ChartComponent.vue'
import type { ChatMessage } from '@/api/chat.ts'
import { computed, nextTick, ref } from 'vue'
import type { ChartTypes } from '@/views/chat/component/BaseChart.ts'
import { buildTableChartFromFields } from '@/views/chat/component/fallbackChart.ts'
import { useI18n } from 'vue-i18n'

const props = withDefaults(
  defineProps<{
    id?: number | string
    chartType: ChartTypes
    message: ChatMessage
    data: Array<{ [key: string]: any }>
    loadingData?: boolean
    showLabel?: boolean
    thousandsSeparatorList?: Array<string>
  }>(),
  {
    id: 'default_chat_id',
    thousandsSeparatorList: () => [],
  }
)

const { t } = useI18n()

/** Tên các cột của kết quả SQL. `record.data` khi là chuỗi JSON, khi đã là object nên phải xử lý cả hai. */
const dataFields = computed<Array<string> | undefined>(() => {
  const raw = props.message?.record?.data
  if (!raw) {
    return undefined
  }
  try {
    return (typeof raw === 'string' ? JSON.parse(raw) : raw)?.fields
  } catch {
    return undefined
  }
})

const chartObject = computed<{
  type: ChartTypes
  title: string
  axis: {
    x: { name: string; value: string }
    y: { name: string; value: string } | Array<{ name: string; value: string }>
    series: { name: string; value: string }
    'multi-quota': {
      name: string
      value: Array<string>
    }
  }
  columns: Array<{ name: string; value: string }>
}>(() => {
  if (props.message?.record?.chart) {
    return JSON.parse(props.message.record.chart)
  }
  // Backend đã tắt pha sinh biểu đồ; dựng cấu hình bảng từ tên cột để vẫn vẽ được bảng kết quả.
  return buildTableChartFromFields(dataFields.value) ?? {}
})

const xAxis = computed(() => {
  const axis = chartObject.value?.axis
  if (axis?.x) {
    return [axis.x]
  }
  return []
})
const yAxis = computed(() => {
  const axis = chartObject.value?.axis
  if (!axis?.y) {
    return []
  }

  const y = axis.y
  const multiQuotaValues = axis['multi-quota']?.value || []

  // 统一处理为数组
  const yArray = Array.isArray(y) ? [...y] : [{ ...y }]

  // 标记 multi-quota
  return yArray.map((item) => ({
    ...item,
    'multi-quota': multiQuotaValues.includes(item.value),
  }))
})
const series = computed(() => {
  const axis = chartObject.value?.axis
  if (axis?.series) {
    return [axis.series]
  }
  return []
})

const multiQuotaName = computed(() => {
  return chartObject.value?.axis?.['multi-quota']?.name
})

const chartRef = ref()

function onTypeChange() {
  nextTick(() => {
    chartRef.value?.destroyChart()
    chartRef.value?.renderChart()
  })
}
function getViewInfo() {
  return {
    chart: {
      columns: chartObject.value?.columns,
      type: props.chartType,
      xAxis: xAxis.value,
      yAxis: yAxis.value,
      series: series.value,
      title: chartObject.value.title,
    },
    data: { data: props.data },
  }
}
function getExcelData() {
  return chartRef.value?.getExcelData()
}

defineExpose({
  onTypeChange,
  getViewInfo,
  getExcelData,
  getBaseAxis: () => {
    return chartRef.value?.getBaseAxis() ?? []
  },
})
</script>

<template>
  <div v-if="message.record?.chart || chartObject?.columns?.length" class="chart-base-container">
    <ChartComponent
      v-if="message.record?.id && data?.length > 0"
      :id="id ?? 'default_chat_id'"
      ref="chartRef"
      :type="chartType"
      :columns="chartObject?.columns"
      :x="xAxis"
      :y="yAxis"
      :series="series"
      :data="data"
      :multi-quota-name="multiQuotaName"
      :show-label="showLabel"
      :thousands-separator-list="thousandsSeparatorList"
    />
    <el-empty v-else :description="loadingData ? t('chat.loading_data') : t('chat.no_data')" />
  </div>
</template>

<style scoped lang="less">
.chart-base-container {
  height: 100%;
  width: 100%;
  border-radius: 12px;
  background: rgba(224, 224, 226, 0.29);
}
</style>
