# PyGame 工具箱

> 把重复用的代码封装好，下次直接拿来用

---

## 精灵动画管理器

管理多帧精灵动画——走路、跳跃、待机等状态切换。

```python
class SpriteAnim:
    def __init__(self, frames, fps=12):
        self.frames = frames      # list of Surface
        self.fps = fps
        self.current = 0
        self.timer = 0
    
    def update(self, dt):
        self.timer += dt
        if self.timer > 1/self.fps:
            self.current = (self.current + 1) % len(self.frames)
            self.timer = 0
    
    def get_frame(self):
        return self.frames[self.current]
```

## 粒子系统

火花、雪花、光点——很多游戏都需要。

**参数**：位置、速度、生命期、颜色渐变、大小衰减。

**优化**：对象池复用，避免频繁创建销毁。

## 对话框系统

RPG 风格的逐字显示对话框。

**功能**：打字机效果、头像、选项分支、滚动文本。

## 简单的状态机

```python
class GameState:
    def __init__(self):
        self.state = "menu"
        self.states = {
            "menu": MenuState(),
            "playing": PlayingState(),
            "paused": PausedState()
        }
    
    def update(self):
        next_state = self.states[self.state].update()
        if next_state:
            self.state = next_state
```

---

> 每个工具写一个 demo 验证，以后做新游戏时直接复制粘贴。
