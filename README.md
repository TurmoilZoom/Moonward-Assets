# Moonward-Assets

[Moonward](https://github.com/TurmoilZoom/Moonward) 使用的静态资源。资源由脚本生成，经 GitHub Actions 定期更新，同时发布到两个站点。

## 发布地址

| 站点 | 根地址 | 用途 |
|---|---|---|
| Cloudflare Pages | `https://moonward-assets.pages.dev/` | 主源 |
| GitHub Pages | `https://turmoilzoom.github.io/Moonward-Assets/` | 主源不可用时兜底 |

两个站点内容相同，都只发布 `public/` 目录，访问路径即文件相对 `public/` 的路径。

## 目录约定

```
public/                 发布目录
  <游戏>/<资源>/         每类资源一个目录：数据文件 + 图片子目录
  _headers              Cloudflare Pages 的缓存规则
data/                   生成脚本的状态文件（图片来源、是否已压缩等），不发布
scripts/                生成脚本，每类资源一个
.github/workflows/      自动更新与发布
```

数据文件里引用的图片使用相对数据文件所在目录的路径，客户端从哪个站点取到数据，就从同一站点取图片。

## 更新流程

工作流 `update.yml` 在以下情况运行：每周一 03:00（UTC）定时、在 Actions 页面手动运行、推送到 `main`。

1. 运行生成脚本：下载上游数据，补齐缺失的图片，统一缩放与压缩；
2. `public/` 或 `data/` 有变化时，由 `github-actions[bot]` 提交并推送到 `main`；
3. 发布到 GitHub Pages。定时运行且没有变化时跳过这一步：GitHub Pages 的 ETag 随发布时间变化，重新发布会让客户端重复下载；
4. Cloudflare Pages 连接本仓库，`main` 上的每个新提交（包括机器人提交）都会自动部署，ETag 按内容生成。

所有来源都找不到的图片会列在本次运行的摘要里，引用它的数据项图片字段留空。

## 图片处理

- 统一为不超过 256×256 的 PNG；
- CI 中用 pngquant 有损压缩，画质下限 80，达不到下限时保留无损；
- 每张图片的来源与压缩状态记录在 `data/` 的清单里，已处理过的不会重复下载或压缩；
- 图片在 Cloudflare Pages 上缓存 30 天（见 `public/_headers`），GitHub Pages 统一缓存 10 分钟。

## 新增一类资源

1. 在 `scripts/` 新建生成脚本，输出到 `public/<游戏>/<资源>/`，状态文件写到 `data/`；
2. 在 `update.yml` 的「生成资源」步骤里调用这个脚本；
3. 需要长期缓存的图片目录，在 `public/_headers` 里加一条规则；
4. 在 [NOTICE.md](NOTICE.md) 补充数据来源与许可证。

## 本地运行

```bash
pip install -r scripts/requirements.txt
python scripts/<脚本>.py --out <输出目录> --state <状态目录>
```

不带参数时直接写入 `public/` 与 `data/`。本地没有 pngquant 时图片保持无损，下次在 CI 中运行时会补做压缩。

## 数据来源

见 [NOTICE.md](NOTICE.md)。
