<template>
  <AppLayout>
    <div class="mx-auto max-w-7xl space-y-6">
      <div v-if="loading" class="flex items-center justify-center py-12">
        <div class="h-8 w-8 animate-spin rounded-full border-b-2 border-primary-600"></div>
      </div>

      <form v-else class="space-y-6" @submit.prevent="saveMarket">
        <div class="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
          <div class="space-y-4">
            <div class="card p-5">
              <div class="flex items-start justify-between gap-4">
                <div>
                  <div class="text-sm font-semibold text-gray-900 dark:text-white">
                    {{ localText("脚本列表", "Scripts") }}
                  </div>
                  <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                    {{
                      localText(
                        "左侧浏览和切换脚本，右侧维护客户端脚本市场展示所需的元数据。上传脚本文件后会自动生成 Script URL 和 SHA256。",
                        "Browse scripts on the left and edit the metadata shown in the client script market. Uploading a script file auto-generates the Script URL and SHA256.",
                      )
                    }}
                  </p>
                </div>
                <button type="button" class="btn btn-secondary btn-sm shrink-0" @click="addScript">
                  {{ localText("新增脚本", "Add Script") }}
                </button>
              </div>

              <div class="mt-4 grid grid-cols-2 gap-3">
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("脚本数量", "Scripts") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">{{ scripts.length }}</div>
                </div>
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("已上传文件", "Uploaded Files") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">{{ uploadedScriptCount }}</div>
                </div>
              </div>

              <div class="mt-4">
                <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                  {{ localText("搜索脚本", "Search Scripts") }}
                </label>
                <input
                  v-model="scriptSearch"
                  type="text"
                  class="input text-sm"
                  :placeholder="localText('按名称、ID 或标签搜索', 'Search by name, ID, or tags')"
                />
              </div>
            </div>

            <div class="card p-3">
              <div v-if="filteredScripts.length === 0" class="rounded-xl border border-dashed border-gray-300 px-4 py-8 text-sm text-gray-500 dark:border-dark-600 dark:text-gray-400">
                {{
                  scripts.length === 0
                    ? localText("暂未配置脚本。点击右上角新增第一个脚本。", "No scripts yet. Add your first script.")
                    : localText("没有匹配当前筛选条件的脚本。", "No script matches the current filter.")
                }}
              </div>

              <div v-else class="space-y-2">
                <button
                  v-for="entry in filteredScripts"
                  :key="entry.script.id || `script-${entry.index}`"
                  type="button"
                  class="w-full rounded-2xl border px-4 py-3 text-left transition"
                  :class="
                    selectedScriptIndex === entry.index
                      ? 'border-primary-500 bg-primary-50/70 shadow-sm dark:border-primary-400 dark:bg-primary-500/10'
                      : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50 dark:border-dark-600 dark:bg-dark-800 dark:hover:border-dark-500 dark:hover:bg-dark-700/60'
                  "
                  @click="selectedScriptIndex = entry.index"
                >
                  <div class="truncate text-sm font-semibold text-gray-900 dark:text-white">
                    {{ entry.script.name || localText("未命名脚本", "Untitled Script") }}
                  </div>
                  <div class="mt-1 truncate font-mono text-xs text-gray-500 dark:text-gray-400">
                    {{ entry.script.id || localText("待填写 ID", "ID required") }}
                  </div>
                  <div class="mt-3 flex flex-wrap gap-2">
                    <span v-for="tag in entry.script.tags.slice(0, 3)" :key="`${entry.index}-${tag}`" class="rounded-full bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-dark-700 dark:text-gray-300">
                      {{ tag }}
                    </span>
                    <span v-if="entry.script.version" class="rounded-full bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-dark-700 dark:text-gray-300">
                      v{{ entry.script.version }}
                    </span>
                  </div>
                </button>
              </div>
            </div>
          </div>

          <div class="space-y-4">
            <div v-if="!selectedScript" class="card p-8 text-center text-sm text-gray-500 dark:text-gray-400">
              {{ localText("请选择左侧脚本进行编辑。", "Select a script on the left to edit.") }}
            </div>

            <template v-else>
              <div class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div>
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">
                      {{ localText("基础信息", "Basic Metadata") }}
                    </div>
                    <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                      {{
                        localText(
                          "这里维护脚本市场展示所需的名称、版本、作者、标签和用途说明。",
                          "Maintain the display name, version, author, tags, and description shown in the client script market.",
                        )
                      }}
                    </p>
                  </div>
                  <button
                    type="button"
                    class="rounded-lg p-2 text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/20"
                    :title="localText('删除当前脚本', 'Delete current script')"
                    @click="removeScript(selectedScriptIndex)"
                  >
                    <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>

                <div class="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">ID</label>
                    <input v-model="selectedScript.id" type="text" class="input text-sm" placeholder="context-ring-restore" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("版本", "Version") }}
                    </label>
                    <input v-model="selectedScript.version" type="text" class="input text-sm" placeholder="1.0.0" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("名称", "Name") }}
                    </label>
                    <input v-model="selectedScript.name" type="text" class="input text-sm" placeholder="Codex Context Ring Restore" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("作者", "Author") }}
                    </label>
                    <input v-model="selectedScript.author" type="text" class="input text-sm" placeholder="China Models" />
                  </div>
                  <div class="md:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("标签", "Tags") }}
                    </label>
                    <input :value="selectedScript.tags.join(', ')" type="text" class="input text-sm" placeholder="内置推荐, 上下文, Codex" @input="handleTagsInput($event)" />
                  </div>
                  <div class="md:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("用途说明", "Description") }}
                    </label>
                    <textarea v-model="selectedScript.description" rows="3" class="input text-sm"></textarea>
                  </div>
                  <div class="md:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Homepage</label>
                    <input v-model="selectedScript.homepage" type="url" class="input text-sm" placeholder="https://china-models.com/" />
                  </div>
                </div>
              </div>

              <div class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div>
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">
                      {{ localText("脚本文件", "Script Asset") }}
                    </div>
                    <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                      {{
                        localText(
                          "上传脚本文件后会自动生成 Script URL 和 SHA256，并写入当前后台运行时 data/public/downloads/scripts/。",
                          "Uploading a script file auto-generates the Script URL and SHA256 and writes the asset into the runtime data/public/downloads/scripts/ directory.",
                        )
                      }}
                    </p>
                  </div>
                  <label class="btn btn-secondary btn-sm cursor-pointer shrink-0">
                    {{ localText("上传脚本文件", "Upload Script File") }}
                    <input class="sr-only" type="file" accept=".js,.user.js,application/javascript,text/javascript" @change="handleUpload($event)" />
                  </label>
                </div>

                <div class="mt-4 space-y-4">
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Script URL</label>
                    <input
                      :value="selectedScript.script_url"
                      type="text"
                      readonly
                      class="input cursor-not-allowed bg-gray-50 text-sm text-gray-500 dark:bg-dark-700/40 dark:text-gray-400"
                      :placeholder="localText('上传脚本文件后自动生成', 'Generated after upload')"
                    />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">SHA256</label>
                    <input
                      :value="selectedScript.sha256"
                      type="text"
                      readonly
                      class="input cursor-not-allowed bg-gray-50 font-mono text-sm text-gray-500 dark:bg-dark-700/40 dark:text-gray-400"
                      :placeholder="localText('上传脚本文件后自动生成', 'Generated after upload')"
                    />
                  </div>
                </div>
              </div>

              <div class="flex justify-end">
                <button type="submit" class="btn btn-primary">
                  {{ localText("保存脚本市场配置", "Save Script Market") }}
                </button>
              </div>
            </template>
          </div>
        </div>
      </form>
    </div>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import AppLayout from "@/components/layout/AppLayout.vue";
import { useAppStore } from "@/stores";
import {
  getScriptMarketConfig,
  updateScriptMarketConfig,
  uploadScriptMarketAsset,
  type ScriptMarketIndexConfig,
  type ScriptMarketScriptConfig,
} from "@/api/admin/settings";

const appStore = useAppStore();
const loading = ref(true);
const scriptSearch = ref("");
const scripts = ref<ScriptMarketScriptConfig[]>([]);
const selectedScriptIndex = ref(0);

function localText(zh: string, _en: string) {
  return zh;
}

function emptyScript(): ScriptMarketScriptConfig {
  return {
    id: "",
    name: "",
    description: "",
    version: "",
    author: "China Models",
    tags: [],
    homepage: "",
    script_url: "",
    sha256: "",
  };
}

const filteredScripts = computed(() => {
  const keyword = scriptSearch.value.trim().toLowerCase();
  return scripts.value
    .map((script, index) => ({ script, index }))
    .filter(({ script }) => {
      if (!keyword) return true;
      return [script.id, script.name, script.description, script.tags.join(" ")]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    });
});

const selectedScript = computed(() => scripts.value[selectedScriptIndex.value] ?? null);

const uploadedScriptCount = computed(() =>
  scripts.value.filter((script) => script.script_url.trim()).length,
);

function normalizeScript(input: ScriptMarketScriptConfig): ScriptMarketScriptConfig {
  return {
    id: input.id.trim(),
    name: input.name.trim(),
    description: input.description.trim(),
    version: input.version.trim(),
    author: input.author.trim(),
    tags: input.tags.map((item) => item.trim()).filter(Boolean),
    homepage: input.homepage.trim(),
    script_url: input.script_url.trim(),
    sha256: input.sha256.trim(),
  };
}

async function loadMarket() {
  loading.value = true;
  try {
    const config = await getScriptMarketConfig();
    scripts.value = Array.isArray(config.scripts) ? config.scripts.map(normalizeScript) : [];
    selectedScriptIndex.value = scripts.value.length > 0 ? 0 : -1;
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("加载脚本市场配置失败", "Failed to load script market config"));
  } finally {
    loading.value = false;
  }
}

function addScript() {
  scripts.value.push(emptyScript());
  selectedScriptIndex.value = scripts.value.length - 1;
}

function removeScript(index: number) {
  scripts.value.splice(index, 1);
  if (scripts.value.length === 0) {
    selectedScriptIndex.value = -1;
    return;
  }
  selectedScriptIndex.value = Math.max(0, Math.min(index, scripts.value.length - 1));
}

function handleTagsInput(event: Event) {
  if (!selectedScript.value) return;
  const value = (event.target as HTMLInputElement).value;
  selectedScript.value.tags = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

async function handleUpload(event: Event) {
  if (!selectedScript.value) return;
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  try {
    const result = await uploadScriptMarketAsset({ script: file });
    if (!selectedScript.value.name.trim()) {
      selectedScript.value.name = result.file_name.replace(/\.user\.js$|\.js$/i, "");
    }
    if (!selectedScript.value.id.trim()) {
      selectedScript.value.id = result.file_name.replace(/\.user\.js$|\.js$/i, "").replace(/[^a-zA-Z0-9_-]+/g, "-");
    }
    selectedScript.value.script_url = result.script_url;
    selectedScript.value.sha256 = result.checksum_sha256;
    appStore.showSuccess(localText("脚本文件上传成功", "Script file uploaded"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("上传脚本文件失败", "Failed to upload script file"));
  }
}

async function saveMarket() {
  try {
    const payload: ScriptMarketIndexConfig = {
      version: 1,
      updated_at: "",
      scripts: scripts.value.map(normalizeScript),
    };
    const saved = await updateScriptMarketConfig(payload);
    scripts.value = saved.scripts.map(normalizeScript);
    if (scripts.value.length === 0) {
      selectedScriptIndex.value = -1;
    } else if (selectedScriptIndex.value < 0) {
      selectedScriptIndex.value = 0;
    }
    appStore.showSuccess(localText("脚本市场配置已保存", "Script market saved"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("保存脚本市场配置失败", "Failed to save script market"));
  }
}

onMounted(() => {
  void loadMarket();
});
</script>
