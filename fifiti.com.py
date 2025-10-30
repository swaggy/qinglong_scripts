#!/usr/bin/env python3
# _*_ coding:utf-8 _*_
"""
HiFiTi（https://hifiti.com/）自动签到脚本。

主要流程：
1. 从环境变量读取账号密码，默认变量名为 `fifiti_username` / `fifiti_password`。
2. 使用 `requests.Session` 登录站点。
3. 调用签到接口 `sg_sign.htm` 完成签到。
4. 获取签到页数据，整理签到情况及统计信息。
5. 通过已有的 `notify.send` 发送推送通知。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

import requests

from notify import send


@dataclass
class HiFitiConfig:
    """脚本运行所需的关键配置。"""

    username: str
    password: str
    base_url: str = "https://hifiti.com"
    timeout: int = 15
    display_name: Optional[str] = None


class HiFitiAutomation:
    """封装 HiFiTi 登录、签到、信息查询逻辑。"""

    def __init__(self, config: HiFitiConfig) -> None:
        self.cfg = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": f"{self.cfg.base_url}/",
            }
        )

    def _full_url(self, path: str) -> str:
        base = self.cfg.base_url.rstrip("/")
        norm_path = path.lstrip("/")
        return f"{base}/{norm_path}"

    def _bootstrap_session(self) -> None:
        """预先访问登录页，为后续请求准备站点所需的 Cookie。"""
        login_url = self._full_url("user-login.htm")
        try:
            resp = self.session.get(login_url, timeout=self.cfg.timeout)
            resp.raise_for_status()
            print("📝 已访问登录页，初始化 Cookie 完成")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 初始化登录页失败：{exc}")

    def login(self) -> bool:
        """完成登录，依据 Cookie 及响应文本判断结果。"""

        self._bootstrap_session()
        login_url = self._full_url("user-login.htm")
        payload = {
            "email": self.cfg.username,
            "password": self.cfg.password,
        }
        headers = {
            "Origin": self.cfg.base_url,
            "Referer": login_url,
        }

        try:
            resp = self.session.post(
                login_url,
                data=payload,
                headers=headers,
                timeout=self.cfg.timeout,
                allow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 登录请求异常：{exc}")
            return False

        if resp.status_code != 200:
            print(f"❌ 登录失败，HTTP 状态码：{resp.status_code}")
            return False

        uid = self.session.cookies.get("bbs_uid")
        if uid and uid != "0":
            print(f"🎉 登录成功，当前 UID：{uid}")
            return True

        message = self._extract_login_feedback(resp.text)
        print(f"❌ 登录失败，原因：{message}")
        return False

    @staticmethod
    def _extract_login_feedback(html: str) -> str:
        """尝试从登录页中提取提示信息。"""

        alert_match = re.search(
            r'<div class="alert[\s\w"-]*?">(.*?)</div>',
            html,
            flags=re.S,
        )
        if alert_match:
            text = re.sub(r"<.*?>", "", alert_match.group(1))
            return text.strip()

        invalid_match = re.search(
            r'<div class="invalid-feedback">\s*([^<]+)\s*</div>',
            html,
        )
        if invalid_match:
            return invalid_match.group(1).strip()

        return "站点未返回明确提示，请检查账号或网络情况"

    def sign(self) -> Tuple[int, str]:
        """调用签到接口，返回 (code, message)。"""

        sign_url = self._full_url("sg_sign.htm")
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": sign_url,
        }
        try:
            resp = self.session.post(sign_url, headers=headers, timeout=self.cfg.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 签到请求异常：{exc}")
            return -1, f"签到请求异常：{exc}"

        try:
            data: Dict[str, str] = resp.json()
        except ValueError:
            text_preview = resp.text[:200].strip()
            print(f"❌ 签到接口返回非 JSON，原始内容：{text_preview}")
            return -1, f"签到接口返回异常：{text_preview}"

        raw_code = data.get("code")
        try:
            code = int(raw_code)  # 站点返回 string，需要转换
        except (TypeError, ValueError):
            code = -1
        message = data.get("message", "").strip()
        print(f"📬 签到接口响应：code={raw_code}, message={message}")
        return code, message or "站点未返回消息"

    def fetch_sign_page(self) -> Optional[str]:
        """获取签到页面源码，后续用于提取统计信息。"""

        sign_page_url = self._full_url("sg_sign.htm")
        try:
            resp = self.session.get(sign_page_url, timeout=self.cfg.timeout)
            resp.raise_for_status()
            print("📰 已获取签到页面内容")
            return resp.text
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 获取签到页面失败：{exc}")
            return None

    @staticmethod
    def _extract_js_var(html: str, var_name: str) -> Optional[str]:
        """从 JS 变量赋值语句中提取文本。"""

        pattern = rf"var\s+{re.escape(var_name)}\s*=\s*'([^']*)';"
        match = re.search(pattern, html)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _extract_stat_block(html: str) -> Dict[str, str]:
        """解析签到页统计卡片信息。"""

        stats = {}
        patterns = {
            "total_signed": r"签到人数</span><br>\s*<b>([^<]+)</b>",
            "today_signed": r"今日签到</span><br>\s*<b>([^<]+)</b>",
            "today_top": r"今日第一</span><br>\s*<b>([^<]+)</b>",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, html)
            if match:
                stats[key] = match.group(1).strip()
        return stats

    @staticmethod
    def _extract_today_rank(html: str, username: str) -> Optional[Dict[str, str]]:
        """
        从签到列表中尝试匹配当前用户，返回签到奖励等信息。
        若用户名为空或列表中未找到，则返回 None。
        """

        if not username:
            return None

        # 签到列表的每一行形如：
        # <tr> ... <td width="60px">排名</td> <td width="100px">用户名</td> <td width="100px">xxx 金币</td> ...
        pattern = (
            r"<tr>\s*"
            r"<td[^>]*>(?P<rank>\d+)</td>\s*"
            r"<td[^>]*>\s*(?P<name>[^<]+)\s*</td>\s*"
            r"<td[^>]*>\s*(?P<reward>[^<]+)\s*</td>\s*"
            r"<td[^>]*>\s*(?P<extra>[^<]+)\s*</td>\s*"
            r"<td[^>]*>\s*(?P<time>[^<]+)\s*</td>\s*"
            r"<td[^>]*>\s*(?P<total_days>[^<]+)\s*</td>\s*"
            r"<td[^>]*>\s*(?P<streak>[^<]+)\s*</td>"
        )

        for match in re.finditer(pattern, html):
            row = {k: v.strip() for k, v in match.groupdict().items()}
            if row.get("name") == username:
                return row
        return None

    def build_summary(self, sign_code: int, sign_message: str, html: Optional[str]) -> str:
        """整理推送使用的文本。"""

        parts = [
            f"签到结果：{sign_message}",
        ]

        if html:
            status_text = self._extract_js_var(html, "s1")  # 按钮文字，可能显示“已签到”
            streak_text = self._extract_js_var(html, "s3")  # 连续签到
            stats = self._extract_stat_block(html)

            if status_text:
                parts.append(f"按钮状态：{status_text}")
            if streak_text:
                parts.append(f"{streak_text}")
            if stats:
                parts.append(
                    "站点统计："
                    + " | ".join(
                        [
                            f"累计签到 {stats.get('total_signed', '未知')}",
                            f"今日签到 {stats.get('today_signed', '未知')}",
                            f"今日第一 {stats.get('today_top', '未知')}",
                        ]
                    )
                )

            display_name = self.cfg.display_name or self.cfg.username
            user_rank = self._extract_today_rank(html, display_name)
            if user_rank:
                parts.append(
                    "个人记录："
                    + " | ".join(
                        [
                            f"今日排名 {user_rank.get('rank')}",
                            f"奖励 {user_rank.get('reward')}",
                            f"额外奖励 {user_rank.get('extra')}",
                            f"累计签到 {user_rank.get('total_days')}",
                            f"连续签到 {user_rank.get('streak')}",
                        ]
                    )
                )

        if sign_code != 0 and "成功" not in sign_message:
            parts.append("⚠️ 请检查账号状态或稍后重试")

        return "\n".join(parts).strip()

    def run(self) -> None:
        """整合整个流程，最后推送通知。"""

        notify_title = f"HiFiTi 签到 - {datetime.now():%Y-%m-%d}"

        if not self.login():
            failure_msg = "❌ 登录失败，签到流程未开始"
            send(notify_title, failure_msg)
            return

        sign_code, sign_message = self.sign()
        html = self.fetch_sign_page()
        summary = self.build_summary(sign_code, sign_message, html)

        print("📮 推送内容：")
        print(summary)
        send(notify_title, summary)


def build_config_from_env() -> Optional[HiFitiConfig]:
    """从环境变量构建配置。"""

    username = os.getenv("fifiti_username")
    password = os.getenv("fifiti_password")
    if not username or not password:
        print("❌ 请配置环境变量 fifiti_username / fifiti_password")
        return None

    base_url = os.getenv("fifiti_base_url", "https://hifiti.com")
    timeout_value = os.getenv("fifiti_timeout")
    try:
        timeout = int(timeout_value) if timeout_value else 15
    except ValueError:
        print(f"⚠️ fifiti_timeout 配置无效：{timeout_value}，将使用默认 15 秒")
        timeout = 15

    display_name = os.getenv("fifiti_display_name")

    return HiFitiConfig(
        username=username,
        password=password,
        base_url=base_url,
        timeout=timeout,
        display_name=display_name,
    )


def main() -> None:
    """脚本入口。"""

    config = build_config_from_env()
    if not config:
        sys.exit(1)

    automation = HiFitiAutomation(config)
    automation.run()


if __name__ == "__main__":
    main()
