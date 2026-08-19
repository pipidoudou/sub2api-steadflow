# Console Runtime Data

这个目录是 `console` 的本地运行期数据目录。

用途：

- 本地 Docker 运行时挂载目录
- 本地 `go run` / 本地后端调试时的 `/app/data` 对应目录
- 本地客户端下载联调用的临时公开下载目录

约束：

- 这里的内容默认不是正式发布资产
- 这里的内容默认不会提交到仓库，除非显式纳入 Git 跟踪
- 这里的内容不会因为重建 `console` 镜像而自动出现在生产环境

常见内容：

- `public/downloads/latest.json`
  - 本地客户端更新链路联调样例
- `public/downloads/scripts/`
  - 本地脚本市场联调样例

如果你要修改正式环境对外可访问的静态资源：

- 官网脚本市场与正式下载索引：去 `../site/public/downloads/`
- Skills Pack：优先通过管理后台上传 zip 并保存配置
- VPS 运行中 `console` 的数据目录：去服务器上的挂载目录，而不是这里

