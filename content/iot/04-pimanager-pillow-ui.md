# PiManager v2：从 CustomTkinter 到自建 Pillow UI 组件库

> 一个 SSH 管理器，2000 行 UI 重写，13 个自研组件，37 个渲染 bug——最后开源了

---

## 起因：CustomTkinter 太"重"

PiManager 最初用 CustomTkinter 写桌面 SSH 客户端，界面暗色主题、侧边栏导航、四个功能页（状态监控/文件管理/终端/设置）。用着还行，但有两个问题：

1. **CustomTkinter 的渲染管线和天气站的 Pillow 图层叠加风格不一致**——同一套代码仓里跑两种 UI 范式，格格不入
2. **CTk 的定制能力有限**——想加个 hover 渐变、做个圆角卡片阴影，要么 hack CTk 源码，要么放弃

于是决定：**把全部 CTk 渲染层换成 Pillow + tkinter Canvas，自建组件库**。

---

## pillui：13 个组件，从零画起

所有组件继承 `BaseComponent`，核心方法就四个：

```python
def draw(self, cw, ch, font_scale=1.0)  # 返回 PIL RGBA 图层
def hit_test(self, mx, my)              # 鼠标在组件上吗？
def on_click(self), on_drag(self)       # 事件回调
```

渲染管线：每个组件画一层 RGBA → `Image.alpha_composite()` 叠加 → `ImageTk.PhotoImage` → 一次性贴到 Canvas。

### 组件清单

| 组件 | 难点 |
|------|------|
| **PillowButton** | hover/pressed 三态切换 + 圆角 + 禁用态 |
| **PillowLabel** | 多行文字 + 左/中/右对齐 + 粗体 |
| **PillowCard** | 圆角容器 + 标题栏 + 边框 |
| **PillowProgressBar** | 圆角轨道 + 渐变填充 |
| **PillowSlider** | 拖拽交互 + 步进值 + `<B1-Motion>` |
| **PillowCheckBox** | 方框+勾号 + `tk.BooleanVar` 绑定 |
| **PillowOptionMenu** | Pillow 显示面 + 弹出原生 `tk.Menu` |
| **PillowScrollFrame** | PageCanvas 子类 + 垂直滚动 |

---

## 37 个渲染 bug，一个一个杀

代码写出来了，跑起来才发现 Pillow 渲染暗坑巨多。

### 文字周围出现暗色小框

**症状**：所有按钮上的文字边缘有灰色方框

**根因**：PIL 的 `ImageDraw.text()` 在透明 RGBA 层上绘字时，抗锯齿的边缘像素与 `(0,0,0,0)` 透明黑混合，产生暗色晕轮。`alpha_composite` 叠加后这些小框就显出来了。

**修复**：把文字画在**实色背景层**上（和组件自身填充色一致），再合成到透明层。

### 亮色主题一开，整个页面变黑

**症状**：点了"浅色模式"后所有卡片、标签、按钮全部黑色

**根因**：所有组件在 `__init__` 时把颜色写死在实例属性里。`refresh_theme()` 只改了 Canvas 背景色——组件颜色根本没动。

**修复**：给每个组件加 `apply_theme()` 方法，从 `ThemeColors` 动态重读颜色令牌。切主题时 PageCanvas 遍历所有组件调 `apply_theme()`。

### 设置页渲不出来——只见最后一张卡片

**症状**：设置页只显示"关于"卡片，前面三张全消失

**根因**：修复"文字小框"时把 `create_layer(cw, ch)` 改成了 `create_text_layer(cw, ch, fill)`。后者创建的是**全画布尺寸的不透明层**。`alpha_composite()` 逐层叠加时，每张卡片的全画布不透明层把前面所有内容全部覆盖——最后只剩最后一张卡片。

**修复**：`PillowCard` 和 `PillowOptionMenu` 改回透明层——它们在 `draw()` 里已经画了自己的圆角矩形填充，不需要额外的全画布背景层。

### emoji 全变豆腐块

**症状**：标题里的 🖥️ 📊 🌡️ 全部渲染为 □

**根因**：Pillow **没有字体回退**。msyh.ttc 没有 emoji 字形，PIL 不会自动找 Segoe UI Emoji——直接给你个方框。

**修复**：38 个 emoji 全部替换为纯文字。

---

## IP 漂移自动修复

树莓派 DHCP 换 IP 后 SSH 连接断开——这是日常问题。

### 第一版（v2.2.0）：功能写了，不好用

加了 `socket.getaddrinfo()` 重解析 + 指数退避重连。但上线后发现四个缺陷：

1. **主机名被解析后的 IP 覆盖**——漂移修复只生效一次
2. **Keep-alive 从未启动**——断线根本检测不到
3. **重连耗尽后 UI 仍显示"已连接"**——用户以为还连着
4. **Keep-alive 双重触发重连**——浪费重试配额

### 第二版（v2.2.2）：意外的连接杀手

更致命的是：keep-alive 每 30 秒探测一次，单次超时就触发 `_auto_reconnect()`，而 `connect()` 第一步是 `disconnect()`——**瞬态超时把健康连接杀死了**。

修复：`exec_command` 不再触发自动重连；keep-alive 连续 2 次失败才重连。

---

## 开源发布

2026 年 6 月 19 日，MIT 协议发布：

- **13 个 pillui 组件**，零外部 UI 依赖
- **261 单元测试**，全部通过
- **5 份文档**：README + 架构文档 + API 参考 + 用户指南 + 合规报告
- **跨平台**：paramiko + Pillow + tkinter，Python 3.9+ 即可运行

---

## 技术栈

| 层 | 选择 | 理由 |
|---|------|------|
| UI 渲染 | Pillow + tkinter Canvas | 和天气站统一风格 |
| 组件库 | 自建 pillui (13 文件) | CustomTkinter 定制性不足 |
| SSH | paramiko | Python 标准 SSH 库 |
| 主题 | ThemeColors 类 | 暗/亮双模式 + 三套强调色 |
| 测试 | unittest | 标准库，零依赖 |
| 许可 | MIT | 开源 |

---

> 写 UI 组件库最深的体会：**你以为修好的 bug，可能只是埋了另一个更隐蔽的**。alpha_composite、RGBA 透明层、PIL 抗锯齿——这些"看起来简单"的底层细节，才是真正吃时间的地方。
