<template>
  <div class="min-h-screen bg-[#fcf9f8] text-[#1c1b1b]">
    <nav
      class="fixed left-0 right-0 top-0 z-40 border-b border-[#c3c5d9] bg-[#fcf9f8]/85 backdrop-blur-md transition-all duration-300"
      :class="showNav ? 'translate-y-0 opacity-100' : 'pointer-events-none -translate-y-full opacity-0'"
    >
      <div class="mx-auto flex h-16 max-w-7xl items-center justify-between px-6 lg:px-12">
        <div class="flex items-center gap-2">
          <div class="flex h-7 w-7 items-center justify-center rounded-lg bg-[#0041c8]">
            <svg viewBox="0 0 24 24" class="h-4 w-4 text-white" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4Z" />
            </svg>
          </div>
          <span class="text-base font-bold tracking-[-0.02em] text-[#0041c8]">AI论文写作指南</span>
        </div>

        <div class="hidden items-center gap-8 sm:flex">
          <button type="button" @click="scrollToSection('framework')" class="text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
            核心框架
          </button>
          <button type="button" @click="scrollToSection('problem')" class="text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
            使用场景
          </button>
          <button type="button" @click="scrollToSection('cases')" class="text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
            学科案例
          </button>
        </div>

        <button
          type="button"
          @click="mobileNavOpen = !mobileNavOpen"
          class="flex h-10 w-10 items-center justify-center rounded-lg text-[#434656] transition-colors hover:text-[#0041c8] sm:hidden"
        >
          <svg v-if="mobileNavOpen" viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M18 6 6 18" />
            <path d="m6 6 12 12" />
          </svg>
          <svg v-else viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M3 12h18" />
            <path d="M3 6h18" />
            <path d="M3 18h18" />
          </svg>
        </button>
      </div>

      <div v-if="mobileNavOpen" class="mx-auto max-w-7xl space-y-3 px-6 pb-4 lg:px-12 sm:hidden">
        <button type="button" @click="scrollToSection('framework')" class="block w-full py-2 text-left text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
          核心框架
        </button>
        <button type="button" @click="scrollToSection('problem')" class="block w-full py-2 text-left text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
          使用场景
        </button>
        <button type="button" @click="scrollToSection('cases')" class="block w-full py-2 text-left text-sm text-[#434656] transition-colors hover:text-[#0041c8]">
          学科案例
        </button>
      </div>
    </nav>

    <div class="relative overflow-hidden">
      <div class="pointer-events-none absolute inset-0 overflow-hidden rounded-2xl">
        <div class="absolute -left-[200px] -top-[100px] h-[500px] w-[500px] rounded-full bg-[#f2dff1]/35 blur-[80px]" />
        <div class="absolute right-[10%] top-[-200px] h-[600px] w-[600px] rounded-full bg-[#dce1ff]/70 blur-[100px]" />
        <div class="absolute left-[40%] top-[60%] h-[300px] w-[300px] rounded-full bg-[#dce1ff]/40 blur-[80px]" />
      </div>

      <div class="pointer-events-none absolute inset-0 opacity-[0.03]" :style="dotPatternStyle" />

      <section class="relative overflow-hidden bg-[linear-gradient(135deg,#fcf9f8_0%,#f0eded_45%,#dce1ff_100%)]">
        <div class="mx-auto max-w-7xl px-6 py-16 lg:px-12 lg:py-24">
          <div class="flex flex-col gap-12 lg:flex-row lg:items-center lg:gap-20">
            <div class="max-w-xl flex-1">
              <div class="mb-6 inline-flex items-center gap-2 rounded-full border border-[#c3c5d9] bg-white/70 px-4 py-1.5">
                <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 text-[#0041c8]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                </svg>
                <span class="text-[13px] font-medium text-[#0041c8]">告别无效对话，掌握AI论文写作方法论</span>
              </div>

              <h1 class="text-3xl font-semibold leading-[1.06] tracking-[-0.03em] text-[#1c1b1b] sm:text-4xl md:text-5xl lg:text-[58px]">
                简单的对话，永远写不出好论文
              </h1>

              <p class="mt-6 max-w-md text-base leading-7 text-[#434656] sm:mt-8 sm:text-lg">
                你把AI当成搜索引擎，它只能给你百科词条；你把AI当成学术助手，它才能帮你构建严谨的研究。差别不在工具，在于方法。
              </p>

              <div class="mt-10 flex flex-col gap-3 sm:flex-row sm:gap-4">
                <button
                  type="button"
                  @click="scrollToSection('framework')"
                  class="group inline-flex items-center justify-center gap-2 rounded-lg bg-[#0041c8] px-6 py-3 text-sm font-semibold text-white transition-all hover:bg-[#0055ff] hover:shadow-xl hover:shadow-[#0041c8]/25"
                >
                  查看核心框架
                  <svg viewBox="0 0 24 24" class="h-4 w-4 transition-transform group-hover:translate-x-0.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                    <path d="M5 12h14" />
                    <path d="m12 5 7 7-7 7" />
                  </svg>
                </button>
                <button
                  type="button"
                  @click="scrollToSection('problem')"
                  class="inline-flex items-center justify-center rounded-lg border border-[#737688] bg-transparent px-6 py-3 text-sm font-semibold text-[#1c1b1b] transition-colors hover:bg-[#f6f3f2]"
                >
                  查看使用场景
                </button>
              </div>
            </div>

            <div class="w-full max-w-md flex-shrink-0 lg:max-w-lg">
              <div class="relative">
                <div class="pointer-events-none absolute -left-6 -top-6 z-10 flex h-12 w-12 animate-[float_6s_ease-in-out_infinite] items-center justify-center rounded-full bg-[#e4d5ff] shadow-lg shadow-[#e4d5ff]/20">
                  <svg viewBox="0 0 24 24" class="h-5 w-5 text-foreground" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                  </svg>
                </div>
                <div class="pointer-events-none absolute -right-4 -top-4 z-10 flex h-10 w-10 animate-[float_5s_ease-in-out_1s_infinite] items-center justify-center rounded-full bg-[oklch(0.65_0.20_350)] shadow-lg shadow-[oklch(0.65_0.20_350_/_0.20)]">
                  <svg viewBox="0 0 24 24" class="h-4 w-4 text-foreground" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 2a3 3 0 0 0-3 3c0 1.2.7 2.3 1.7 2.8A4 4 0 0 0 8 11.5V12a4 4 0 0 0 8 0v-.5a4 4 0 0 0-2.7-3.7A3 3 0 0 0 15 5a3 3 0 0 0-3-3Z" />
                    <path d="M9 18h6" />
                    <path d="M10 22h4" />
                  </svg>
                </div>
                <div class="pointer-events-none absolute -bottom-5 -right-5 z-10 flex h-11 w-11 animate-[float_7s_ease-in-out_2s_infinite] items-center justify-center rounded-full bg-foreground shadow-lg shadow-foreground/20">
                  <svg viewBox="0 0 24 24" class="h-[18px] w-[18px] text-background" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16l4-3 4 3 4-3 4 3V8Z" />
                    <path d="M14 2v6h6" />
                  </svg>
                </div>

                <div class="relative w-full">
                  <div class="overflow-hidden rounded-2xl border border-[#c3c5d9] bg-white shadow-2xl shadow-[#0041c8]/10">
                    <div class="space-y-3 bg-white p-4">
                      <div class="mb-2">
                        <p class="text-[13px] font-semibold text-foreground">常见错误 vs 正确做法</p>
                        <p class="text-[10px] text-[#596176]">同样的需求，不同的提示词，天差地别的结果</p>
                      </div>

                      <div
                        v-for="card in promptCards"
                        :key="card.prompt"
                        class="flex flex-col gap-2 rounded-2xl border p-4"
                        :class="card.isBad ? 'border-red-200 bg-red-50/50' : 'border-green-200 bg-green-50/30'"
                      >
                        <div class="flex items-center gap-2">
                          <div
                            class="flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold"
                            :class="card.isBad ? 'bg-red-100 text-red-600' : 'bg-green-100 text-green-600'"
                          >
                            {{ card.isBad ? '✗' : '✓' }}
                          </div>
                          <span class="text-[11px] font-semibold uppercase tracking-wide text-[#5d6478]">
                            {{ card.isBad ? '错误示范' : '正确示范' }}
                          </span>
                        </div>
                        <div class="rounded-lg border border-[#9aa4ba] bg-[#f3f5fa] px-3 py-2">
                          <p class="text-[12px] font-medium text-[#3d4458]">{{ card.prompt }}</p>
                        </div>
                        <p class="text-[12px] leading-[1.6] text-[#4b5266]">{{ card.response }}</p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="framework" class="bg-[#f6f3f2] py-20">
        <div class="mx-auto max-w-7xl px-6 lg:px-12">
          <div class="rounded-2xl border border-[#c3c5d9] bg-white p-8 shadow-2xl shadow-[#0041c8]/5 sm:p-12">
            <div class="mb-14 max-w-2xl">
              <p class="mb-3 text-xs font-semibold uppercase tracking-[0.22em] text-[#0041c8]">核心框架</p>
              <h2 class="text-3xl font-semibold tracking-[-0.03em] text-[#1c1b1b] sm:text-4xl">开题阶段，更需要一套稳定的方法</h2>
              <p class="mt-4 text-base leading-7 text-[#434656]">
                论文开题最容易卡住的，不是“不会写一句话”，而是不知道先做什么、后做什么。我们把这个过程拆成几个更清晰的环节，让 AI 先帮你理方向、理文献、理结构，再进入起草和检查。
              </p>
            </div>

            <div class="grid grid-cols-1 gap-6 sm:grid-cols-2 xl:grid-cols-4">
              <div v-for="skill in skills" :key="skill.num" class="group rounded-2xl border border-[#c3c5d9] bg-[#fcf9f8] p-6 transition-all duration-300 hover:-translate-y-1 hover:border-[#0041c8]/30 hover:shadow-xl hover:shadow-[#0041c8]/10">
                <div class="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl" :class="skill.darkIconClass">
                  <component :is="skill.icon" class="h-5 w-5" />
                </div>
                <p class="text-[11px] font-bold uppercase tracking-[0.22em] text-[#737688]">{{ skill.num }}</p>
                <h3 class="mt-2 text-lg font-semibold tracking-[-0.02em] text-[#1c1b1b]">{{ skill.title }}</h3>
                <p class="mt-3 text-sm leading-7 text-[#434656]">{{ skill.description }}</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="cases" class="border-t border-[#c3c5d9] bg-white py-20 sm:py-28">
        <div class="mx-auto max-w-7xl px-6 lg:px-12">
          <div class="flex flex-col gap-12 lg:flex-row lg:gap-20">
            <div class="lg:w-80 lg:flex-shrink-0">
              <div class="lg:sticky lg:top-8">
                <p class="mb-3 text-xs font-semibold uppercase tracking-[0.22em] text-[#0041c8]">学科案例</p>
                <h2 class="text-3xl font-semibold tracking-[-0.03em] text-[#1c1b1b] sm:text-4xl">中医、计算机、教育三个专业使用示例</h2>
                <p class="mt-4 text-base leading-7 text-[#434656]">
                  目标：先把题目和材料理顺，再形成一个能继续和导师讨论的开题版本。
                </p>
              </div>
            </div>

            <div class="flex-1 space-y-6">
              <div
                v-for="item in cases"
                :key="item.title"
                class="group rounded-2xl border border-[#c3c5d9] bg-[#fcf9f8] p-7 transition-all duration-300 hover:-translate-y-1 hover:border-[#0041c8]/25 hover:shadow-2xl hover:shadow-[#0041c8]/10 sm:p-8"
              >
                <div class="flex flex-col gap-6 lg:flex-row lg:items-start lg:gap-10">
                  <div class="lg:w-64 lg:flex-shrink-0">
                    <div class="mb-2 flex items-center gap-2">
                      <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-[#dce1ff]">
                        <component :is="item.icon" class="h-4 w-4 text-[#0041c8]" />
                      </div>
                      <span class="text-xs font-semibold uppercase tracking-[0.22em] text-[#0041c8]">{{ item.discipline }}</span>
                    </div>
                    <h3 class="text-lg font-semibold tracking-[-0.02em] text-[#1c1b1b]">{{ item.title }}</h3>
                    <p class="mt-2 text-sm leading-7 text-[#434656]">{{ item.description }}</p>
                  </div>
                  <div class="flex-1">
                    <p class="mb-3 text-xs font-semibold uppercase tracking-[0.22em] text-[#737688]">操作流程</p>
                    <div class="space-y-2">
                      <div v-for="step in item.steps" :key="step" class="flex items-center gap-3 rounded-lg bg-white px-4 py-3">
                        <svg viewBox="0 0 24 24" class="h-4 w-4 flex-shrink-0 text-[#0041c8]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                          <path d="m9 18 6-6-6-6" />
                        </svg>
                        <span class="text-sm text-[#1f2230]">{{ step }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <footer class="border-t border-[#c3c5d9] bg-[#fcf9f8]">
        <div class="mx-auto flex max-w-7xl items-center justify-between px-6 py-10 lg:px-12">
          <div class="flex items-center gap-2">
            <div class="flex h-6 w-6 items-center justify-center rounded-lg bg-[#0041c8]">
              <svg viewBox="0 0 24 24" class="h-3 w-3 text-white" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4Z" />
              </svg>
            </div>
            <span class="text-sm font-bold tracking-[-0.02em] text-[#0041c8]">AI论文写作指南</span>
          </div>
          <p class="text-xs text-[#434656]">© {{ currentYear }}. 仅供学习参考。</p>
        </div>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'

const mobileNavOpen = ref(false)
const showNav = ref(false)

const dotPatternStyle = computed(() => ({
  backgroundImage: 'radial-gradient(circle, var(--foreground) 1px, transparent 1px)',
  backgroundSize: '24px 24px',
}))

const promptCards = [
  {
    prompt: '帮我写一篇关于人工智能的论文',
    response: '得到泛泛而谈的科普文，缺乏学术深度，没有引用文献，论点松散，导师一看就知道是AI生成的。',
    isBad: true,
  },
  {
    prompt: '我需要撰写一篇关于Transformer架构在NLP领域演进的文献综述，要求：1）梳理2017-2024年的关键论文 2）按时间线组织技术演进脉络 3）对比不同变体的性能差异 4）使用学术规范引用格式',
    response: 'AI会生成结构清晰、逻辑严谨的综述框架，包含具体论文引用、技术对比表格，可以直接作为写作基础进行深化。',
    isBad: false,
  },
]

const SearchIcon = defineComponent({
  name: 'SearchIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round' }, [
      h('circle', { cx: '11', cy: '11', r: '7' }),
      h('path', { d: 'm20 20-3.5-3.5' }),
    ])
  },
})

const LayoutTemplateIcon = defineComponent({
  name: 'LayoutTemplateIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round' }, [
      h('rect', { x: '3', y: '4', width: '18', height: '16', rx: '2' }),
      h('path', { d: 'M3 10h18' }),
      h('path', { d: 'M8 20V10' }),
    ])
  },
})

const RefreshIcon = defineComponent({
  name: 'RefreshIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, [
      h('path', { d: 'M3 12a9 9 0 0 1 15.55-6.36L21 8' }),
      h('path', { d: 'M21 3v5h-5' }),
      h('path', { d: 'M21 12a9 9 0 0 1-15.55 6.36L3 16' }),
      h('path', { d: 'M8 16H3v5' }),
    ])
  },
})

const BookIcon = defineComponent({
  name: 'BookIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, [
      h('path', { d: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20' }),
      h('path', { d: 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z' }),
    ])
  },
})

const BrainIcon = defineComponent({
  name: 'BrainIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, [
      h('path', { d: 'M12 2a3 3 0 0 0-3 3c0 1.2.7 2.3 1.7 2.8A4 4 0 0 0 8 11.5V12a4 4 0 0 0 8 0v-.5a4 4 0 0 0-2.7-3.7A3 3 0 0 0 15 5a3 3 0 0 0-3-3Z' }),
      h('path', { d: 'M9 18h6' }),
      h('path', { d: 'M10 22h4' }),
    ])
  },
})

const GraduationCapIcon = defineComponent({
  name: 'GraduationCapIcon',
  setup() {
    return () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, [
      h('path', { d: 'm2 10 10-5 10 5-10 5-10-5Z' }),
      h('path', { d: 'M6 12v5c3 2 9 2 12 0v-5' }),
      h('path', { d: 'M22 10v6' }),
    ])
  },
})

const skills = [
  {
    num: '01',
    icon: SearchIcon,
    iconClass: 'bg-[#e4d5ff] text-foreground',
    darkIconClass: 'bg-[#e4d5ff]/20 text-[#e4d5ff]',
    title: '先确定课题方向',
    description: '把宽泛想法通过历史文献检索整理成几个更可写的题目方向，综合创新、范围和难度，确定高质量课题方向。',
  },
  {
    num: '02',
    icon: LayoutTemplateIcon,
    iconClass: 'bg-[oklch(0.85_0.12_155)] text-foreground',
    darkIconClass: 'bg-[oklch(0.85_0.12_155_/_0.18)] text-[oklch(0.85_0.12_155)]',
    title: '文献先理顺',
    description: '建立可用的文献池，把背景、方法、案例和综述分开看，避免没有可引用文献。',
  },
  {
    num: '03',
    icon: RefreshIcon,
    iconClass: 'bg-[oklch(0.85_0.10_55)] text-foreground',
    darkIconClass: 'bg-[oklch(0.85_0.10_55_/_0.18)] text-[oklch(0.85_0.10_55)]',
    title: '结构先搭起来',
    description: '题目和文献相对稳定后，再研究背景、意义、现状、问题和方法整理成一个规范的开题框架。',
  },
  {
    num: '04',
    icon: BookIcon,
    iconClass: 'bg-[oklch(0.80_0.08_250)] text-foreground',
    darkIconClass: 'bg-[oklch(0.80_0.08_250_/_0.18)] text-[oklch(0.80_0.08_250)]',
    title: '最后再起草和检查',
    description: '最后再按照标准格式或指定模板起草，并做一轮规范性检查。',
  },
]

const cases = [
  {
    icon: BookIcon,
    discipline: '中医',
    title: '中医方向：先把宽泛题目收成可讨论的开题',
    description: '如果你现在只有“针灸干预失眠”这类大方向，更合适的做法不是直接出整篇，而是先把题目收窄，再判断文献支撑够不够。',
    steps: ['请使用 literature-search，先帮我找“针灸干预失眠”相关的近年论文和常用检索词', '请使用 paper-triage，帮我筛出真正适合进入开题文献池的材料', '请使用 topic-discovery，基于现有文献给我几个更可写的题目方向', '请使用 proposal-structure，把主推方向整理成一版开题结构'],
  },
  {
    icon: BrainIcon,
    discipline: '计算机科学',
    title: '计算机方向：把技术兴趣整理成能落地的题目',
    description: '计算机开题最容易出现“方向很新，但题目太散”。这类场景更适合先把研究对象、任务场景和实现边界划清楚。',
    steps: ['请使用 topic-discovery，先帮我把研究对象、任务场景和边界收清楚', '请使用 literature-search，围绕这个方向建立第一轮 paper pool', '请使用 paper-triage，区分哪些更适合做综述、方法和实验支撑', '请使用 evidence-first-draft，在现有文献基础上帮我起草一版开题内容'],
  },
  {
    icon: GraduationCapIcon,
    discipline: '教育',
    title: '教育方向：从教学问题走到研究问题',
    description: '教育学开题最关键的是把“想研究什么”说清楚。这里更需要先把对象、场景、变量和研究问题之间的关系理顺。',
    steps: ['请使用 topic-discovery，先帮我把研究对象、应用场景和限制条件说清楚', '请使用 literature-search，建立教育学相关的第一轮文献池', '请使用 proposal-structure，整理出研究背景、意义、现状、问题和方法提纲', '请使用 citation-risk-check，检查哪里证据还不够、哪里表达太泛'],
  },
]

const currentYear = new Date().getFullYear()

const handleScroll = () => {
  showNav.value = window.scrollY > 80
}

const scrollToSection = (id: string) => {
  mobileNavOpen.value = false
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
}

onMounted(() => {
  window.addEventListener('scroll', handleScroll, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', handleScroll)
})
</script>
