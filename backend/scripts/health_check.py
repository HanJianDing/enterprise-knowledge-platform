"""健康检查与告警脚本（产品化 C4）

用法（backend 目录下）：
    python scripts/health_check.py --url http://localhost:8000
    python scripts/health_check.py --url http://localhost:8000 --webhook https://oapi.dingtalk.com/robot/send?access_token=xxx

逻辑：GET /health（含数据库/向量库连通探测）→ 任一检查失败或请求失败 →
若有 webhook（参数或 HEALTH_WEBHOOK_URL 环境变量）发送文本告警 → exit 1。

webhook 格式用钉钉/企微机器人通用的 text 消息体：
    {"msgtype": "text", "text": {"content": "..."}}

部署：cron 每 5 分钟（配合腾讯云拨测或替代方案），示例：
    */5 * * * * cd /srv/app/backend && python scripts/health_check.py --url http://localhost:8000 >> health.log 2>&1
"""
import argparse
import json
import os
import sys

import httpx

DEFAULT_URL = "http://localhost:8000"


def build_message(result: dict) -> str:
    """根据 /health 结果生成告警文案（纯函数，单测覆盖）"""
    checks = result.get("checks", {})
    bad = [name for name, ok in checks.items() if not ok]
    return f"⚠️ 企业知识库告警：服务状态 {result.get('status', 'unknown')}，异常检查项：{', '.join(bad) or '无（请求失败）'}"


def send_webhook(url: str, content: str) -> bool:
    """发送钉钉/企微通用 text 消息；失败返回 False"""
    try:
        resp = httpx.post(
            url, json={"msgtype": "text", "text": {"content": content}}, timeout=10
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def check(url: str) -> dict:
    """GET /health，返回 JSON；请求失败返回 {"status": "unreachable", "checks": {}}"""
    try:
        resp = httpx.get(f"{url.rstrip('/')}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return {"status": "unreachable", "checks": {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="企业知识库健康检查与告警")
    parser.add_argument("--url", type=str, default=DEFAULT_URL)
    parser.add_argument("--webhook", type=str, default="")
    args = parser.parse_args()

    result = check(args.url)
    healthy = result.get("status") == "ok"

    webhook = args.webhook or os.environ.get("HEALTH_WEBHOOK_URL", "")
    if not healthy:
        content = build_message(result)
        print(content)
        if webhook:
            sent = send_webhook(webhook, content)
            print(f"告警发送: {'成功' if sent else '失败'}")
        sys.exit(1)

    print(f"健康检查通过: {result}")
    sys.exit(0)


if __name__ == "__main__":
    main()
