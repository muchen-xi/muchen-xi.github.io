# C:\Users\m2008\Desktop\personal-site — 晨曦的宇宙 · 个人主页

> 上次更新: 2026-06-09 | Git: 9 commits | 托管: GitHub Pages (chenxiuniverse.top)

## 项目速览

单页个人主页 + 独立文章页，暗色 cosmic 主题。展示编程作品、数学建模、音乐品味、未来方向。

## 文件结构

```
personal-site/
├── index.html              ★ 主页（Hero + 轮播 + 天气 + 成就 + 未来方向）
├── topic.html              ★ 文章页模板（?t=ai-ml 切换专题，侧边目录+正文）
├── stars-bg.webp            Hero 星空背景
├── q-glow.webp              Q版形象
├── bgm.mp3                  背景音乐
├── stargaze.png             观星配图
├── q.png / q_blur.jpg       Q版形象副本
├── CNAME                    chenxiuniverse.top
├── .claude/memory.md        ★ 本文件
├── content/
│   ├── ai-ml/               index.json + 3 篇 .md
│   ├── iot/                 index.json + 3 篇 .md
│   ├── game-dev/            index.json + 3 篇 .md
│   └── content-creation/    index.json + 3 篇 .md
└── .gitignore
```

## 技术栈

- 纯手写 HTML/CSS/JS，无框架
- Canvas 星空粒子动画（600 颗星 + 轨道拖尾）
- 天文月相计算（儒略日）
- Open-Meteo 天气 API + 多源 IP 定位
- marked.js CDN 渲染 markdown
- Giscus 评论系统（GitHub Discussions 后端）
- Git + GitHub Pages + 自定义域名

## 内容维护方式

新增文章只需 2 步：
1. 在 `content/{专题}/` 下放入 `04-xxx.md`
2. 在 `content/{专题}/index.json` 的 articles 数组添加条目

## 图片优化

| 文件 | 格式 | 大小 |
|------|------|------|
| stars-bg.webp | WebP | 340 KB |
| q-glow.webp | WebP | 368 KB |
| bgm.mp3 | MP3 | 6.4 MB ⚠️ 待优化 |

## 用户偏好

- **不要擅自改代码:** 先告诉怎么改，用户动手或明确授权后再改
- **不要乱删文件**
- **语言:** 中文交互
- **版本管理:** 每次改动都要 git commit + push

## GitHub

- 仓库: muchen-xi/muchen-xi.github.io
- 域名: chenxiuniverse.top
- Giscus: 已安装，分类 Comments

## 下次要做

1. bgm.mp3 压缩或延迟加载（6.4MB 太大）
2. 其他 PNG 图片（stargaze.png / q.png / q_blur.jpg）转 WebP
3. 旧 bg.png 清理（已不再使用）
4. Lunr.js 站内搜索
