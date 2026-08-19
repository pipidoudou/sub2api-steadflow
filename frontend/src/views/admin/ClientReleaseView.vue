<template>
  <AppLayout>
    <div class="mx-auto max-w-7xl space-y-6">
      <div v-if="loading" class="flex items-center justify-center py-12">
        <div class="h-8 w-8 animate-spin rounded-full border-b-2 border-primary-600"></div>
      </div>

      <form v-else class="space-y-6" @submit.prevent="saveConfig">
        <div class="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
          <div class="space-y-4">
            <div class="card p-5">
              <div class="text-sm font-semibold text-gray-900 dark:text-white">
                {{ localText("当前发布概览", "Current Release Overview") }}
              </div>
              <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                {{
                  localText(
                    "左侧查看当前正式版本和安装包状态，右侧维护版本信息、上传安装包并生成运行中的 latest.json。",
                    "Review the current formal release on the left and edit the release metadata on the right.",
                  )
                }}
              </p>

              <div class="mt-4 grid grid-cols-2 gap-3">
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("当前版本", "Current Version") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">
                    {{ publishedVersion }}
                  </div>
                </div>
                <div class="rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 dark:border-dark-600 dark:bg-dark-700/40">
                  <div class="text-xs uppercase tracking-[0.18em] text-gray-500 dark:text-gray-400">
                    {{ localText("已配置平台", "Configured Platforms") }}
                  </div>
                  <div class="mt-2 text-2xl font-semibold text-gray-900 dark:text-white">
                    {{ configuredAssetCount }}
                  </div>
                </div>
              </div>

              <div class="mt-4 space-y-3">
                <div
                  v-for="platform in platforms"
                  :key="platform.key"
                  class="rounded-2xl border border-gray-200 bg-white px-4 py-4 dark:border-dark-600 dark:bg-dark-800"
                >
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                      <div class="text-sm font-semibold text-gray-900 dark:text-white">
                        {{ platform.title }}
                      </div>
                      <div class="mt-1 truncate text-xs text-gray-500 dark:text-gray-400">
                        {{ platform.asset.name || localText("尚未上传安装包", "No installer uploaded yet") }}
                      </div>
                    </div>
                    <span
                      class="shrink-0 rounded-full px-2 py-1 text-xs font-medium"
                      :class="
                        platform.asset.url
                          ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                          : 'bg-gray-100 text-gray-500 dark:bg-dark-700 dark:text-gray-400'
                      "
                    >
                      {{ platform.asset.url ? localText("已上传", "Uploaded") : localText("未配置", "Missing") }}
                    </span>
                  </div>
                  <div v-if="platform.asset.url" class="mt-3 break-all font-mono text-xs text-gray-500 dark:text-gray-400">
                    {{ platform.asset.url }}
                  </div>
                </div>
              </div>
            </div>

            <div class="card p-5">
              <div class="text-sm font-semibold text-gray-900 dark:text-white">
                {{ localText("运行中输出", "Runtime Output") }}
              </div>
              <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                {{
                  localText(
                    "保存后会直接更新当前后台运行时 data/public/downloads/latest.json，不需要手工登录 VPS 复制文件。",
                    "Saving updates the runtime latest.json directly without manual VPS file uploads.",
                  )
                }}
              </p>
              <div class="mt-4 rounded-xl border border-gray-200 bg-gray-50 px-3 py-3 text-xs text-gray-600 dark:border-dark-600 dark:bg-dark-700/40 dark:text-gray-300">
                <div class="font-medium">{{ localText("当前 latest.json", "Current latest.json") }}</div>
                <div class="mt-2 break-all font-mono">{{ latestJsonPreview }}</div>
              </div>
            </div>
          </div>

          <div class="space-y-6">
            <div class="card p-6">
              <div class="flex items-start justify-between gap-4">
                <div>
                  <div class="text-sm font-semibold text-gray-900 dark:text-white">
                    {{ localText("版本信息", "Release Metadata") }}
                  </div>
                  <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                    {{
                      localText(
                        "维护客户端检查更新时使用的版本号、详情页链接和更新说明。",
                        "Maintain the version, release page URL, and notes used by the desktop client update checker.",
                      )
                    }}
                  </p>
                </div>
                <button type="submit" class="btn btn-primary btn-sm">
                  {{ localText("保存发布配置", "Save Release Config") }}
                </button>
              </div>

              <div class="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
                <div>
                  <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                    {{ localText("版本号", "Version") }}
                  </label>
                  <input v-model="form.version" type="text" class="input text-sm" placeholder="v0.5.0" />
                </div>
                <div>
                  <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                    {{ localText("详情页链接", "Release URL") }}
                  </label>
                  <input
                    v-model="form.url"
                    type="url"
                    class="input text-sm"
                    placeholder="https://china-models.com/downloads/"
                  />
                </div>
              </div>

              <div class="mt-4">
                <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                  {{ localText("更新说明", "Release Notes") }}
                </label>
                <textarea
                  v-model="form.body"
                  rows="5"
                  class="input text-sm"
                  :placeholder="localText('填写客户端本次更新说明，会写入 latest.json 的 body 字段。', 'Write the release notes that will be stored in latest.json body.')"
                ></textarea>
              </div>
            </div>

            <div class="grid gap-6 md:grid-cols-2">
              <div v-for="platform in platforms" :key="platform.key" class="card p-6">
                <div class="flex items-start justify-between gap-4">
                  <div>
                    <div class="text-sm font-semibold text-gray-900 dark:text-white">
                      {{ platform.title }}
                    </div>
                    <p class="mt-1 text-xs leading-6 text-gray-500 dark:text-gray-400">
                      {{ platform.detail }}
                    </p>
                  </div>
                  <label class="btn btn-secondary btn-sm cursor-pointer shrink-0">
                    {{ localText("上传安装包", "Upload Installer") }}
                    <input class="sr-only" type="file" :accept="platform.accept" @change="handleUpload(platform.key, $event)" />
                  </label>
                </div>

                <div class="mt-4 space-y-3">
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("安装包名称", "Asset Name") }}
                    </label>
                    <input v-model="platform.asset.name" type="text" class="input text-sm" :placeholder="platform.placeholderName" />
                  </div>
                  <div>
                    <label class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                      {{ localText("下载链接", "Asset URL") }}
                    </label>
                    <input
                      :value="platform.asset.url"
                      type="text"
                      readonly
                      class="input cursor-not-allowed bg-gray-50 text-sm text-gray-500 dark:bg-dark-700/40 dark:text-gray-400"
                      :placeholder="localText('上传安装包后自动生成', 'Generated after upload')"
                    />
                  </div>
                  <div v-if="platform.checksum" class="rounded-xl bg-gray-50 px-3 py-3 text-xs text-gray-500 dark:bg-dark-700/40 dark:text-gray-400">
                    <div class="font-medium text-gray-700 dark:text-gray-300">SHA256</div>
                    <div class="mt-1 break-all font-mono">{{ platform.checksum }}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </form>
    </div>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import AppLayout from "@/components/layout/AppLayout.vue";
import { useAppStore } from "@/stores";
import {
  getClientReleaseConfig,
  updateClientReleaseConfig,
  uploadClientReleaseAsset,
  type ClientReleaseAssetConfig,
  type ClientReleaseConfig,
} from "@/api/admin/settings";

type PlatformKey = "macos" | "windows";

const appStore = useAppStore();
const loading = ref(true);

const form = reactive<ClientReleaseConfig>({
  version: "",
  url: "",
  body: "",
  assets: [],
});

const uploadChecksums = reactive<Record<PlatformKey, string>>({
  macos: "",
  windows: "",
});

const macAsset = reactive<ClientReleaseAssetConfig>({ name: "", url: "" });
const windowsAsset = reactive<ClientReleaseAssetConfig>({ name: "", url: "" });

const platforms = computed(() => [
  {
    key: "macos" as PlatformKey,
    title: localText("macOS 安装包", "macOS Installer"),
    detail: localText("建议上传正式 DMG。下载链接会自动生成。", "Upload the formal DMG. The download URL is generated automatically."),
    accept: ".dmg,application/x-apple-diskimage",
    placeholderName: "steadflow-codex-enhanced-client-0.5.0-arm64.dmg",
    asset: macAsset,
    checksum: uploadChecksums.macos,
  },
  {
    key: "windows" as PlatformKey,
    title: localText("Windows 安装包", "Windows Installer"),
    detail: localText("建议上传正式 setup.exe。下载链接会自动生成。", "Upload the formal setup.exe. The download URL is generated automatically."),
    accept: ".exe,application/x-msdownload",
    placeholderName: "steadflow-codex-enhanced-client-0.5.0-windows-x64-setup.exe",
    asset: windowsAsset,
    checksum: uploadChecksums.windows,
  },
]);

const publishedVersion = computed(() => form.version.trim() || localText("未发布", "Not Published"));

const configuredAssetCount = computed(() =>
  normalizedAssets().length,
);

const latestJsonPreview = computed(() =>
  JSON.stringify(
    {
      version: form.version,
      url: form.url,
      body: form.body,
      assets: normalizedAssets(),
    },
    null,
    2,
  ),
);

function localText(zh: string, _en: string) {
  return zh;
}

function pickPlatformAsset(name: string) {
  const lower = name.toLowerCase();
  if (lower.endsWith(".dmg")) return macAsset;
  if (lower.endsWith(".exe")) return windowsAsset;
  return null;
}

function normalizedAssets(): ClientReleaseAssetConfig[] {
  return [macAsset, windowsAsset]
    .map((asset) => ({ name: asset.name.trim(), url: asset.url.trim() }))
    .filter((asset) => asset.name && asset.url);
}

function applyConfig(config: ClientReleaseConfig) {
  form.version = config.version || "";
  form.url = config.url || "";
  form.body = config.body || "";
  form.assets = Array.isArray(config.assets) ? config.assets : [];
  macAsset.name = "";
  macAsset.url = "";
  windowsAsset.name = "";
  windowsAsset.url = "";
  uploadChecksums.macos = "";
  uploadChecksums.windows = "";
  for (const asset of form.assets) {
    const target = pickPlatformAsset(asset.name);
    if (!target) continue;
    target.name = asset.name;
    target.url = asset.url;
  }
}

async function loadConfig() {
  loading.value = true;
  try {
    const config = await getClientReleaseConfig();
    applyConfig(config);
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("加载客户端发布配置失败", "Failed to load client release config"));
  } finally {
    loading.value = false;
  }
}

async function handleUpload(platform: PlatformKey, event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  try {
    const result = await uploadClientReleaseAsset({ asset: file });
    const target = platform === "macos" ? macAsset : windowsAsset;
    target.name = result.file_name;
    target.url = result.url;
    uploadChecksums[platform] = result.checksum_sha256;
    appStore.showSuccess(localText("安装包上传成功", "Installer uploaded"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("上传安装包失败", "Failed to upload installer"));
  }
}

async function saveConfig() {
  try {
    const saved = await updateClientReleaseConfig({
      version: form.version.trim(),
      url: form.url.trim(),
      body: form.body.trim(),
      assets: normalizedAssets(),
    });
    applyConfig(saved);
    appStore.showSuccess(localText("客户端发布配置已保存", "Client release config saved"));
  } catch (error: any) {
    appStore.showError(error?.response?.data?.detail || localText("保存客户端发布配置失败", "Failed to save client release config"));
  }
}

onMounted(() => {
  void loadConfig();
});
</script>
