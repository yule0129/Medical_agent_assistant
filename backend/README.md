# PostgreSQL 后端

## 1. 创建数据库

先在 PostgreSQL 中创建一个空数据库，例如：

```sql
CREATE DATABASE health_agent;
```

## 2. 配置连接

将项目根目录的 `.env.example` 复制为 `.env`，填入你自己的连接信息：

```env
DATABASE_URL=postgresql+psycopg://用户名:密码@主机:端口/数据库名
CORS_ORIGINS=http://localhost:5500,http://127.0.0.1:5500
```

不要把包含真实密码的 `.env` 提交到 Git。

## 3. 安装并启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

首次启动会自动创建表。开发环境可以用下面的接口写入虚构演示数据：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/dev/seed
```

接口文档：<http://127.0.0.1:8000/docs>

## 主要接口

- `GET /api/health`
- `GET /api/medical-records`
- `GET /api/medical-records/{id}`
- `PATCH /api/medical-records/{id}`
- `POST /api/medical-records/{id}/sign`
- `POST /api/dev/seed`

当前后端只使用虚构数据。接入真实医院系统前，需要增加统一身份认证、医生与患者权限校验、审计日志、加密和数据库迁移管理。
