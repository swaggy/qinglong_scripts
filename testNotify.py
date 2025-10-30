#!/usr/bin/env python3
# _*_ coding:utf-8 _*_
"""
简易通知测试脚本。

直接运行即可触发 notify.py 中配置的推送渠道，便于快速验证通知链路是否通畅。
"""
from datetime import datetime

from notify import send


def main() -> None:
    title = f"通知服务测试 - {datetime.now():%Y-%m-%d %H:%M:%S}"
    content = (
        "这是一条来自 testNotify.py 的测试通知，"
        "用于验证当前环境下的推送配置是否生效。"
    )
    print("🚀 正在发送测试通知...")
    send(title, content)
    print("✅ 推送调用完成，请到对应渠道确认是否收到消息。")


if __name__ == "__main__":
    main()
