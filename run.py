#!/usr/bin/env python
"""一键启动脚本 - 同时启动后端和 React 前端"""
import subprocess
import sys
import os
import time

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    print("=" * 60)
    print("  🤖 AI客服 - 电商智能客服系统")
    print("=" * 60)

    # ── 启动后端 ──
    print("\n[1/2] 启动后端 (FastAPI, 端口 8000)...")
    backend = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "0.0.0.0", "--port", "8000",
            "--app-dir", os.path.join(ROOT_DIR, "backend"),
        ],
    )

    # 等待后端就绪
    print("  等待后端启动...")
    time.sleep(3)

    # ── 启动 React 前端 ──
    print("[2/2] 启动 React 前端 (Vite, 端口 5173)...")
    frontend_dir = os.path.join(ROOT_DIR, "frontend")
    if sys.platform == "win32":
        frontend = subprocess.Popen(
            "npm run dev -- --host 0.0.0.0",
            cwd=frontend_dir, shell=True,
        )
    else:
        frontend = subprocess.Popen(
            ["npm", "run", "dev", "--", "--host", "0.0.0.0"],
            cwd=frontend_dir,
        )

    print("\n" + "=" * 60)
    print("  ✅ 系统已启动！")
    print(f"  🖥️  前端:    http://localhost:5173")
    print(f"  📚 API:     http://localhost:8000")
    print(f"  📖 Swagger: http://localhost:8000/docs")
    print(f"  👤 管理员:  admin（密码见 .env 中 INITIAL_ADMIN_PASSWORD）")
    print("=" * 60)
    print(" 按 Ctrl+C 停止所有服务")
    print("=" * 60)

    try:
        backend.wait()
        frontend.wait()
    except KeyboardInterrupt:
        print("\n🛑 正在停止服务...")
        backend.terminate()
        frontend.terminate()
        print("已停止。")


if __name__ == "__main__":
    main()
