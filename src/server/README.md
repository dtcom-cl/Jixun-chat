# server 文件夹（服务端）

Python 服务端，跑在电脑上。负责与大模型通讯、管理状态、调度消息、持久化存储。手机 App 连接的就是它。

## 怎么启动

首次启动：

```bash
cd src/server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

以后每次启动：

```bash
cd src/server
.venv\Scripts\activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

启动后 WebSocket 地址为 ws://电脑IP:8000/ws，在 App 里填入该地址即可连接。

## 各个文件干什么

- `main.py` 程序入口，启动 FastAPI 与 WebSocket 服务
- `config.py` 配置项：端口、数据库路径、各功能开关
- `database.py` SQLite 访问层，建表与读写
- `schemas.py` 定义模型必须返回的 JSON 结构，用于校验
- `ws_gateway.py` WebSocket 网关，负责收发、ACK、断线补发
- `dispatcher.py` 消息调度器，决定消息何时发出、发给谁
- `state_machine.py` 状态机，维护情绪、能量、亲密度
- `personality.py` 人格管理，从聊天记录提取人格档案
- `memory.py` 记忆管理：短期窗口、中期摘要、长期向量
- `reflection.py` 后台反思循环，决定是否主动发起
- `group_chat.py` 群聊抢麦
- `api_pool.py` 多模型接口池，故障自动切换
- `prompt_builder.py` 把系统提示词、人格、状态与记忆拼成完整 Prompt

## 依赖

`requirements.txt` 包含：

```
fastapi
uvicorn[standard]
websockets
pydantic
tiktoken
httpx
sqlalchemy
lancedb
```

版本以 `requirements.txt` 为准。
