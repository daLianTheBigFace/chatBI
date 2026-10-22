# ChatBI

ChatBI 是一个基于 Python 的对话式商业智能项目，使用 `uv` 管理 Python 环境和依赖。

## 快速开始

```powershell
cd E:\project\chatbi
uv sync
uv run chatbi
```

## 常用命令

```powershell
uv add <package>          # 添加运行依赖
uv add --dev <package>   # 添加开发依赖
uv sync                   # 同步环境
uv run pytest             # 运行测试
uv run ruff check .       # 代码检查
uv run ruff format .      # 代码格式化
```

Python 版本由 `.python-version` 固定为 3.12。
