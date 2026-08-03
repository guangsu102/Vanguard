<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

interface Props {
  option: EChartsOption | Record<string, any>
  height?: string
  width?: string
  autoresize?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  height: '300px',
  width: '100%',
  autoresize: true,
})

const chartRef = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null

const initChart = () => {
  if (!chartRef.value) return

  chartInstance = echarts.init(chartRef.value)
  chartInstance.setOption(props.option as EChartsOption)
}

const updateChart = () => {
  if (chartInstance) {
    chartInstance.setOption(props.option as EChartsOption)
  }
}

const resizeChart = () => {
  chartInstance?.resize()
}

onMounted(() => {
  initChart()

  if (props.autoresize) {
    window.addEventListener('resize', resizeChart)
  }
})

watch(() => props.option, updateChart, { deep: true })

onUnmounted(() => {
  if (props.autoresize) {
    window.removeEventListener('resize', resizeChart)
  }
  chartInstance?.dispose()
  chartInstance = null
})

defineExpose({
  resize: resizeChart,
  getInstance: () => chartInstance,
})
</script>

<template>
  <div
    ref="chartRef"
    class="echarts-wrapper"
    :style="{ height, width }"
  />
</template>

<style scoped lang="scss">
.echarts-wrapper {
  width: 100%;
}
</style>
