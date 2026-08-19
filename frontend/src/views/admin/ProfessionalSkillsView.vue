<template>
  <AppLayout>
    <div class="mx-auto max-w-7xl space-y-6">
      <div v-if="loading" class="flex items-center justify-center py-12">
        <div class="h-8 w-8 animate-spin rounded-full border-b-2 border-primary-600"></div>
      </div>

      <form v-else class="space-y-6" @submit.prevent="saveSkillsCatalog">
        <div class="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
          <div class="space-y-4">
            <div class="card p-5">
              <div class="flex items-start justify-between gap-4">
                <div>
                  <div class="text-sm font-semibold text-gray-900 dark:text-white">
                    {{ localText("Skills Pack 列表", "Skills Pack List") }}
                  </div>
                  <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                    {{
                      localText(
                        "左侧用于浏览和切换 Pack，右侧编辑当前 Pack 的标签、版本、上传包和包含的 Skills。",
                        "Browse packs on the left and edit the selected pack on the right.",
                      )
                    }}
                  </p>
                </div>
                <button type="button" class="btn btn-secondary btn-sm shrink-0" @click="addPack">
                  {{ localText("新增 Pack", "Add Pack") }}
                </button>
              </div>

              <div class="mt-4 grid grid-cols-2 gap-3">
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("Pack 数量", "Packs") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">{{ packs.length }}</div>
                </div>
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("Skill 数量", "Skills") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">{{ totalSkills }}</div>
                </div>
              </div>

              <div class="mt-4">
                <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                  {{ localText("搜索 Pack", "Search Packs") }}
                </label>
                <input
                  v-model="packSearch"
                  type="text"
                  class="input text-sm"
                  :placeholder="localText('按 Pack 名称、安装包 ID 或标签搜索', 'Search by pack name, install pack ID, or tag')"
                />
              </div>
            </div>

            <div class="card p-3">
              <div v-if="filteredPacks.length === 0" class="rounded-xl border border-dashed border-gray-300 px-4 py-8 text-sm text-gray-500 dark:border-dark-600 dark:text-gray-400">
                {{
                  packs.length === 0
                    ? localText("暂未配置 Skills Pack。点击右上角新增第一个 Pack。", "No packs yet. Add your first pack.")
                    : localText("没有匹配当前筛选条件的 Pack。", "No pack matches the current filter.")
                }}
              </div>

              <div v-else class="space-y-2">
                <button
                  v-for="packEntry in filteredPacks"
                  :key="packEntry.pack.install_pack_id || packEntry.pack.id || `pack-${packEntry.index}`"
                  type="button"
                  class="w-full rounded-2xl border px-4 py-3 text-left transition"
                  :class="
                    selectedPackIndex === packEntry.index
                      ? 'border-primary-500 bg-primary-50/70 shadow-sm dark:border-primary-400 dark:bg-primary-500/10'
                      : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50 dark:border-dark-600 dark:bg-dark-800 dark:hover:border-dark-500 dark:hover:bg-dark-700/60'
                  "
                  @click="selectedPackIndex = packEntry.index"
                >
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                      <div class="truncate text-sm font-semibold text-gray-900 dark:text-white">
                        {{ packEntry.pack.name || localText("未命名 Pack", "Untitled Pack") }}
                      </div>
                      <div class="mt-1 truncate font-mono text-xs text-gray-500 dark:text-gray-400">
                        {{ packEntry.pack.install_pack_id || packEntry.pack.id || localText("待填写安装包 ID", "Install Pack ID required") }}
                      </div>
                    </div>
                    <span class="shrink-0 rounded-full border border-gray-200 px-2 py-1 text-xs text-gray-500 dark:border-dark-500 dark:text-gray-400">
                      {{ packEntry.pack.skills.length }} {{ localText("个 skill", "skills") }}
                    </span>
                  </div>
                  <div class="mt-3 flex flex-wrap gap-2">
                    <span
                      v-for="tag in packEntry.pack.tags.slice(0, 3)"
                      :key="`${packEntry.index}-${tag}`"
                      class="rounded-full bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-dark-700 dark:text-gray-300"
                    >
                      {{ tag }}
                    </span>
                    <span v-if="packEntry.pack.version" class="rounded-full bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-dark-700 dark:text-gray-300">
                      v{{ packEntry.pack.version }}
                    </span>
                  </div>
                </button>
              </div>
            </div>
          </div>

          <div class="space-y-4">
            <div v-if="!selectedPack" class="card p-8 text-center text-sm text-gray-500 dark:text-gray-400">
              {{ localText("请选择左侧的 Pack 进行编辑。", "Select a pack on the left to edit.") }}
            </div>

            <template v-else>
              <div class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div class="min-w-0">
                    <div class="text-lg font-semibold text-gray-900 dark:text-white">
                      {{ selectedPack.name || localText("未命名 Pack", "Untitled Pack") }}
                    </div>
                    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                      {{
                        localText(
                          "这里编辑客户端展示、安装和更新所需的 Pack 元数据。上传 zip 后会自动回填 manifest 地址和部分字段。",
                          "Edit the metadata used by the client for display, install, and update. Uploading a zip can auto-fill the manifest URL and some fields.",
                        )
                      }}
                    </p>
                  </div>
                  <button
                    type="button"
                    class="rounded-lg p-2 text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/20"
                    :title="localText('删除当前 Pack', 'Delete current pack')"
                    @click="removePack(selectedPackIndex)"
                  >
                    <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>

                <div class="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Pack ID</label>
                    <input v-model="selectedPack.id" type="text" class="input text-sm" placeholder="thesis-proposal-pack" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("安装包 ID", "Install Pack ID") }}
                    </label>
                    <input v-model="selectedPack.install_pack_id" type="text" class="input text-sm" placeholder="thesis-proposal" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("Pack 名称", "Pack Name") }}
                    </label>
                    <input v-model="selectedPack.name" type="text" class="input text-sm" placeholder="论文提案 Skill Pack" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("版本", "Version") }}
                    </label>
                    <input v-model="selectedPack.version" type="text" class="input text-sm" placeholder="2026.06.16.01" />
                  </div>
                  <div class="md:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("标签", "Tags") }}
                    </label>
                    <input
                      :value="tagsInputValue(selectedPack)"
                      type="text"
                      class="input text-sm"
                      placeholder="Codex必装, 论文场景"
                      @input="handlePackTagsInput(selectedPack, $event)"
                    />
                  </div>
                </div>

                <div class="mt-4">
                  <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                    {{ localText("Pack 介绍", "Pack Description") }}
                  </label>
                  <textarea
                    v-model="selectedPack.description"
                    rows="3"
                    class="input text-sm"
                    :placeholder="localText('描述这个 pack 面向什么场景、解决什么问题。', 'Describe the pack scenario and value.')"
                  ></textarea>
                </div>
              </div>

              <div class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div>
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">
                      {{ localText("发布与安装资源", "Distribution and Install Assets") }}
                    </div>
                    <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                      {{
                        localText(
                          "标准流程是从独立的 skills 项目产出 zip，然后在这里上传。上传成功后会写入运行中后台的数据目录，不要求随 console 仓库发版。",
                          "Standard flow: build the zip from the standalone skills project, then upload it here. Successful upload writes to the running console data directory and does not require a console repo release.",
                        )
                      }}
                    </p>
                  </div>
                  <label class="btn btn-secondary btn-sm cursor-pointer shrink-0">
                    {{ localText("上传 zip 并生成 manifest", "Upload zip and generate manifest") }}
                    <input
                      class="sr-only"
                      type="file"
                      accept=".zip,application/zip"
                      @change="handleArchiveUpload(selectedPackIndex, $event)"
                    />
                  </label>
                </div>

                <div class="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div class="md:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      Manifest URL
                    </label>
                    <input
                      v-model="selectedPack.manifest_url"
                      type="url"
                      class="input text-sm"
                      placeholder="https://console.example.com/downloads/skills/pack.json"
                    />
                  </div>
                </div>
              </div>

              <div class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div>
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">
                      {{ localText("包含的 Skills", "Included Skills") }}
                    </div>
                    <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                      {{
                        localText(
                          "这里配置客户端在 Pack 内展示的 skill 名称与用途说明。单个 pack 的结构保持在一屏内，避免所有 pack 堆在一页里难以维护。",
                          "Configure the skill names and descriptions shown inside this pack in the client.",
                        )
                      }}
                    </p>
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm shrink-0" @click="addSkill(selectedPackIndex)">
                    {{ localText("新增 Skill", "Add Skill") }}
                  </button>
                </div>

                <div v-if="selectedPack.skills.length === 0" class="mt-4 rounded-xl border border-dashed border-gray-300 px-4 py-8 text-sm text-gray-500 dark:border-dark-600 dark:text-gray-400">
                  {{ localText("当前 Pack 还没有 Skill。", "This pack has no skills yet.") }}
                </div>

                <div v-else class="mt-4 space-y-3">
                  <div
                    v-for="(skill, skillIndex) in selectedPack.skills"
                    :key="`skill-${selectedPackIndex}-${skillIndex}`"
                    class="rounded-2xl border border-gray-200 bg-gray-50/70 p-4 dark:border-dark-600 dark:bg-dark-700/30"
                  >
                    <div class="mb-3 flex items-center justify-between gap-3">
                      <div class="text-sm font-medium text-gray-900 dark:text-white">
                        Skill {{ skillIndex + 1 }}
                      </div>
                      <button
                        type="button"
                        class="rounded p-1 text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/20"
                        :title="localText('删除 Skill', 'Delete Skill')"
                        @click="removeSkill(selectedPackIndex, skillIndex)"
                      >
                        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                          <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>

                    <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
                      <div>
                        <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Skill ID</label>
                        <input v-model="skill.id" type="text" class="input text-sm" placeholder="topic-discovery" />
                      </div>
                      <div>
                        <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                          {{ localText("Skill 名称", "Skill Name") }}
                        </label>
                        <input v-model="skill.name" type="text" class="input text-sm" placeholder="topic-discovery" />
                      </div>
                    </div>

                    <div class="mt-4">
                      <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                        {{ localText("用途说明", "Description") }}
                      </label>
                      <textarea
                        v-model="skill.description"
                        rows="2"
                        class="input text-sm"
                        :placeholder="localText('描述这个 skill 的用途。', 'Describe what the skill does.')"
                      ></textarea>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>

        <div class="sticky bottom-4 flex justify-end">
          <button type="submit" class="btn btn-primary shadow-lg" :disabled="saving">
            <svg
              v-if="saving"
              class="mr-2 h-4 w-4 animate-spin"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ saving ? localText("保存中...", "Saving...") : localText("保存 Skills 配置", "Save Skills Catalog") }}
          </button>
        </div>
      </form>
    </div>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import AppLayout from "@/components/layout/AppLayout.vue";
import { getSettings, updateSettings, uploadSkillPackArchive } from "@/api/admin/settings";
import { useAppStore } from "@/stores";

interface SkillFormItem {
  id: string;
  name: string;
  description: string;
}

interface SkillPackFormItem {
  id: string;
  name: string;
  description: string;
  tags: string[];
  install_pack_id: string;
  version: string;
  manifest_url: string;
  skills: SkillFormItem[];
}

const { locale } = useI18n();
const appStore = useAppStore();

const loading = ref(true);
const saving = ref(false);
const packs = ref<SkillPackFormItem[]>([]);
const selectedPackIndex = ref<number>(0);
const packSearch = ref("");

const totalSkills = computed(() => packs.value.reduce((sum, pack) => sum + pack.skills.length, 0));

const filteredPacks = computed(() => {
  const keyword = packSearch.value.trim().toLowerCase();
  return packs.value
    .map((pack, index) => ({ pack, index }))
    .filter(({ pack }) => {
      if (!keyword) return true;
      return [
        pack.name,
        pack.id,
        pack.install_pack_id,
        pack.description,
        ...pack.tags,
      ]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    });
});

const selectedPack = computed(() => packs.value[selectedPackIndex.value] ?? null);

watch(
  () => packs.value.length,
  (length) => {
    if (length === 0) {
      selectedPackIndex.value = 0;
      return;
    }
    if (selectedPackIndex.value > length - 1) {
      selectedPackIndex.value = length - 1;
    }
  },
);

watch(filteredPacks, (items) => {
  if (items.length === 0) return;
  const stillVisible = items.some((item) => item.index === selectedPackIndex.value);
  if (!stillVisible) {
    selectedPackIndex.value = items[0].index;
  }
});

function localText(zh: string, en: string): string {
  return locale.value.startsWith("zh") ? zh : en;
}

function parseCatalog(raw: string | null | undefined): SkillPackFormItem[] {
  const normalized = String(raw || "").trim();
  if (!normalized) return [];
  try {
    const parsed = JSON.parse(normalized) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => normalizePack(item))
      .filter((item): item is SkillPackFormItem => item !== null);
  } catch {
    return [];
  }
}

function normalizePack(value: unknown): SkillPackFormItem | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  const name = typeof item.name === "string" ? item.name.trim() : "";
  const installPackId =
    typeof item.install_pack_id === "string" ? item.install_pack_id.trim() : "";
  if (!id || !name || !installPackId) return null;
  return {
    id,
    name,
    description: typeof item.description === "string" ? item.description.trim() : "",
    tags: Array.isArray(item.tags)
      ? item.tags.map((tag) => (typeof tag === "string" ? tag.trim() : "")).filter(Boolean)
      : [],
    install_pack_id: installPackId,
    version: typeof item.version === "string" ? item.version.trim() : "",
    manifest_url: typeof item.manifest_url === "string" ? item.manifest_url.trim() : "",
    skills: Array.isArray(item.skills)
      ? item.skills.map((skill) => normalizeSkill(skill)).filter((skill): skill is SkillFormItem => skill !== null)
      : [],
  };
}

function normalizeSkill(value: unknown): SkillFormItem | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  const name = typeof item.name === "string" ? item.name.trim() : "";
  if (!id || !name) return null;
  return {
    id,
    name,
    description: typeof item.description === "string" ? item.description.trim() : "",
  };
}

function buildCatalogJSON(): string {
  return JSON.stringify(
    packs.value.map((pack) => ({
      id: pack.id.trim(),
      name: pack.name.trim(),
      description: pack.description.trim(),
      tags: pack.tags.map((tag) => tag.trim()).filter(Boolean),
      install_pack_id: pack.install_pack_id.trim(),
      version: pack.version.trim(),
      manifest_url: pack.manifest_url.trim(),
      skills: pack.skills.map((skill) => ({
        id: skill.id.trim(),
        name: skill.name.trim(),
        description: skill.description.trim(),
      })),
    })),
  );
}

function createEmptyPack(): SkillPackFormItem {
  return {
    id: "",
    name: "",
    description: "",
    tags: ["Codex必装"],
    install_pack_id: "",
    version: "",
    manifest_url: "",
    skills: [],
  };
}

function addPack() {
  packs.value.push(createEmptyPack());
  selectedPackIndex.value = packs.value.length - 1;
}

function removePack(index: number) {
  packs.value.splice(index, 1);
}

function addSkill(packIndex: number) {
  packs.value[packIndex]?.skills.push({
    id: "",
    name: "",
    description: "",
  });
}

function removeSkill(packIndex: number, skillIndex: number) {
  packs.value[packIndex]?.skills.splice(skillIndex, 1);
}

function tagsInputValue(pack: SkillPackFormItem): string {
  return pack.tags.join(", ");
}

function handlePackTagsInput(pack: SkillPackFormItem, event: Event) {
  const target = event.target as HTMLInputElement | null;
  pack.tags = (target?.value || "")
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

async function handleArchiveUpload(packIndex: number, event: Event) {
  const target = event.target as HTMLInputElement | null;
  const file = target?.files?.[0];
  const pack = packs.value[packIndex];
  if (!file || !pack) {
    return;
  }
  if (!pack.install_pack_id.trim()) {
    appStore.showError(localText("请先填写安装包 ID", "Install Pack ID is required before upload"));
    target.value = "";
    return;
  }
  if (!pack.version.trim()) {
    appStore.showError(localText("请先填写版本号", "Version is required before upload"));
    target.value = "";
    return;
  }
  try {
    const result = await uploadSkillPackArchive({
      install_pack_id: pack.install_pack_id.trim(),
      version: pack.version.trim(),
      entry_dir: pack.install_pack_id.trim(),
      archive: file,
    });
    pack.manifest_url = result.manifest_url;
    pack.version = result.version;
    if (!pack.name.trim() && result.pack_name?.trim()) {
      pack.name = result.pack_name.trim();
    }
    if (!pack.description.trim() && result.pack_summary?.trim()) {
      pack.description = result.pack_summary.trim();
    }
    if (!pack.id.trim()) {
      pack.id = `${pack.install_pack_id.trim()}-pack`;
    }
    if (pack.skills.length === 0 && result.root_skill_name?.trim()) {
      pack.skills.push({
        id: result.root_skill_name.trim(),
        name: result.root_skill_name.trim(),
        description: result.root_skill_description?.trim() || "",
      });
    }
    appStore.showSuccess(localText("Skill 包已上传并生成 manifest", "Skill pack uploaded and manifest generated"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("上传 Skill 包失败", "Failed to upload skill pack"));
  } finally {
    target.value = "";
  }
}

async function loadCatalog() {
  loading.value = true;
  try {
    const settings = await getSettings();
    packs.value = parseCatalog(settings.codex_client_skills_catalog_json);
    selectedPackIndex.value = 0;
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("加载 Skills 配置失败", "Failed to load Skills catalog"));
  } finally {
    loading.value = false;
  }
}

async function saveSkillsCatalog() {
  saving.value = true;
  try {
    await updateSettings({
      codex_client_skills_catalog_json: buildCatalogJSON(),
    });
    appStore.showSuccess(localText("Skills 配置已保存", "Skills catalog saved"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("保存 Skills 配置失败", "Failed to save Skills catalog"));
  } finally {
    saving.value = false;
  }
}

onMounted(loadCatalog);
</script>
