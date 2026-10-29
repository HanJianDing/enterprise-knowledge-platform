"""演示环境启动器（企业知识库产品化·体验种子数据）

用法（项目根目录）：
    python run_demo.py

与真实环境完全隔离：
- 数据库：ragdb_demo（真实环境用 .env 里的 ragdb）
- 向量库：backend/chroma_demo/（真实环境 backend/chroma_db/）
- 上传目录：backend/uploads_demo/（真实环境 backend/uploads/）
- 管理员密码：.env.demo 的 INITIAL_ADMIN_PASSWORD

演示数据由 scripts/seed_demo.py 生成（部门/知识库/权限/账号/文档）。
真实环境启动仍用 python run.py，两条线互不干扰。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env(path: Path, override: bool) -> None:
    """逐行加载 .env 文件；override=False 用 setdefault（真实配置优先），
    override=True 直接赋值（演示隔离项必须生效）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if override:
                os.environ[key.strip()] = value.strip()
            else:
                os.environ.setdefault(key.strip(), value.strip())


# 1) 先加载真实 .env（DASHSCOPE_API_KEY/模型配置从真实环境来）
_load_env(ROOT / ".env", override=False)
# 2) 再加载 .env.demo（演示专用：管理员密码/端口/JWT），显式覆盖
_load_env(ROOT / ".env.demo", override=True)

# 演示隔离路径：数据库 URL 从真实 .env 继承凭据、只换库名（凭据不写死）；
# 向量库/上传目录用演示专用目录（防误配置指向真实数据）；
# 演示 admin 密码强制 CHANGE_ME_DEMO_PASSWORD（直接赋值，真实 .env 的同名配置不得生效）
import urllib.parse as _urlparse

_real_url = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:CHANGE_ME_DATABASE_PASSWORD@localhost:5432/ragdb"
)
_parsed = _urlparse.urlparse(_real_url)
os.environ["DATABASE_URL"] = _parsed._replace(path="/ragdb_demo").geturl()
os.environ["CHROMA_PERSIST_DIR"] = str(ROOT / "backend" / "chroma_demo")
os.environ["UPLOAD_DIR"] = str(ROOT / "backend" / "uploads_demo")
os.environ["INITIAL_ADMIN_PASSWORD"] = os.environ["DEMO_ADMIN_PASSWORD"]

# 确保 backend 在 sys.path（真实 run.py 同样处理）
sys.path.insert(0, str(ROOT / "backend"))

if __name__ == "__main__":
    import uvicorn

    from app.config import settings

    print("=" * 60)
    print("演示环境启动（与真实数据完全隔离）")
    print(f"  数据库: ragdb_demo")
    print(f"  向量库: {settings.chroma_persist_dir}")
    print(f"  上传目录: {settings.upload_dir}")
    print("=" * 60)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.backend_port,
        reload=False,
    )
