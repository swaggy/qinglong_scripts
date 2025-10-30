import base64
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from notify import send

@dataclass
class SJSConfig:
    """脚本运行所需的关键配置"""

    username: str
    password: str
    ocr_service: Optional[str] = None
    base_url: str = "https://xsijishe.com"
    timeout: int = 10
    sign_path: str = "/k_misign-sign.html"
    chromium_binary: str = "/usr/bin/chromium"
    chromedriver_path: str = "/usr/bin/chromedriver"


class SJSAutomation:
    """封装司机社登录、签到、信息查询的一揽子流程"""

    def __init__(self, config: SJSConfig) -> None:
        self.cfg = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/114.0 Safari/537.36",
                "Referer": config.base_url,
            }
        )
        self.domain = urlparse(self.cfg.base_url).hostname or "xsijishe.com"
        self.formhash = ""
        self.seccodehash = ""
        self.referer = ""
        self.cookies: Dict[str, str] = {}
        self.check_in_status = 2  # 0-已签到 1-签到成功 2-失败

    @staticmethod
    def _random_suffix(code_len: int = 4) -> str:
        chars = "qazwsxedcrfvtgbyhnujmikolpQAZWSXEDCRFVTGBYHNUJIKOLP"
        return "".join(random.choices(chars, k=code_len))

    @contextmanager
    def web_driver(self):
        """统一创建并回收浏览器实例"""

        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if self.cfg.chromium_binary:
            options.binary_location = self.cfg.chromium_binary

        service = Service(executable_path=self.cfg.chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
        try:
            yield driver
        finally:
            driver.quit()

    def _fetch_login_form(self) -> bool:
        """通过浏览器拉取登录所需的 formhash 等信息"""

        try:
            with self.web_driver() as driver:
                driver.get(f"{self.cfg.base_url}/home.php?mod=space")
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "referer")))
                referer_input = driver.find_element(By.NAME, "referer")
                self.referer = referer_input.get_attribute("value")

                driver.get(self.referer)
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "formhash")))
                self.formhash = driver.find_element(By.NAME, "formhash").get_attribute("value")

                seccode_el = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//span[starts-with(@id, 'seccode_')]")
                ))
                self.seccodehash = seccode_el.get_attribute("id").replace("seccode_", "")

                self.cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
                for name, value in self.cookies.items():
                    self.session.cookies.set(name, value)

            self.session.headers["Referer"] = self.referer
            print(f"📝 [信息] 获取成功: formhash={self.formhash}, seccodehash={self.seccodehash}")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 获取登录参数失败：{exc}")
            return False

    def _recognize_captcha(self, base64_img: str) -> str:
        """调用 OCR 服务识别验证码"""

        if "," in base64_img:
            base64_img = base64_img.split(",", 1)[1]

        if not self.cfg.ocr_service:
            print("🤖 未配置 OCR 服务地址，无法识别验证码")
            return ""

        try:
            resp = requests.post(
                self.cfg.ocr_service,
                json={"image": base64_img},
                timeout=self.cfg.timeout,
            )
            if resp.ok:
                return resp.json().get("result", "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"🤖 OCR 识别错误: {exc}")
        return ""

    def _check_captcha(self, seccodeverify: str) -> bool:
        params = {
            "mod": "seccode",
            "action": "check",
            "inajax": "1",
            "modid": "member::logging",
            "idhash": self.seccodehash,
            "secverify": seccodeverify,
        }
        headers = {
            "Referer": self.referer,
            "User-Agent": self.session.headers.get("User-Agent", ""),
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = self.session.get(
                f"{self.cfg.base_url}/misc.php",
                params=params,
                headers=headers,
                timeout=self.cfg.timeout,
            )
            return resp.ok and "succeed" in resp.text
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 验证码校验异常: {exc}")
            return False

    def login(self) -> bool:
        """通过 requests 完成登录"""

        if not self._fetch_login_form():
            return False

        captcha_url = (
            f"{self.cfg.base_url}/misc.php?mod=seccode&update={int(time.time())}&idhash={self.seccodehash}"
        )
        seccodeverify = ""
        for _ in range(5):
            resp = self.session.get(captcha_url, timeout=self.cfg.timeout)
            if "image" not in resp.headers.get("Content-Type", ""):
                print("❗ 验证码图片响应异常，重试...")
                time.sleep(1)
                continue

            img = Image.open(BytesIO(resp.content))
            buffer = BytesIO()
            img.save(buffer, format="JPEG")
            base64_img = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

            seccodeverify = self._recognize_captcha(base64_img)
            if len(seccodeverify) == 4 and self._check_captcha(seccodeverify):
                print(f"🤖 [OCR] 验证码识别结果: {seccodeverify} | ✅ [验证通过]")
                break
            print(f"🤖 [OCR] 验证码识别结果: {seccodeverify} | ❌ [验证不通过]")
            time.sleep(1)
        else:
            print("❌ [失败] 验证码识别/验证失败")
            return False

        login_url = (
            f"{self.cfg.base_url}/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login"
            f"&loginhash=L{self._random_suffix()}&inajax=1"
        )
        payload = {
            "formhash": self.formhash,
            "referer": self.referer,
            "username": self.cfg.username,
            "password": self.cfg.password,
            "questionid": "0",
            "answer": "",
            "seccodehash": self.seccodehash,
            "seccodemodid": "member::logging",
            "seccodeverify": seccodeverify,
        }

        resp = self.session.post(
            login_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.cfg.timeout,
        )

        if "欢迎您回来" in resp.text:
            print("🎉 [成功] 登录成功！")
            self.cookies.update(self.session.cookies.get_dict())
            return True

        print(f"❌ [失败] 登录失败：{resp.text[:100]}...")
        return False

    def do_sign_in(self, driver: webdriver.Chrome) -> bool:
        """使用 Selenium 执行签到操作"""

        try:
            print("⏳ 正在执行签到操作...")
            driver.get(self.cfg.base_url)
            time.sleep(1)

            driver.delete_all_cookies()
            for cookie_name, cookie_value in self.cookies.items():
                driver.add_cookie(
                    {
                        "name": cookie_name,
                        "value": cookie_value,
                        "path": "/",
                        "domain": self.domain,
                    }
                )

            sign_page_url = f"{self.cfg.base_url}{self.cfg.sign_path}"
            print(f"➡️ 访问签到页面: {sign_page_url}")
            driver.get(sign_page_url)

            wait = WebDriverWait(driver, 15)
            wait.until(EC.presence_of_element_located((By.ID, "JD_sign")))

            page_source = driver.page_source
            if "今日已签" in page_source or "您今天已经签到过了" in page_source:
                print("✅ 今日已签到")
                self.check_in_status = 0
                return True

            sign_button = driver.find_element(By.ID, "JD_sign")
            print("👉 找到签到按钮，准备点击")

            driver.save_screenshot("before_sign.png")

            sign_button.click()
            print("✅ 已点击签到按钮")

            time.sleep(2)

            driver.save_screenshot("after_sign.png")

            new_page_source = driver.page_source
            if "今日已签" in new_page_source or "您今天已经签到过了" in new_page_source:
                print("✅ 签到成功，页面显示今日已签到")
                self.check_in_status = 0
                return True
            if "签到成功" in new_page_source:
                print("🎉 签到成功")
                self.check_in_status = 1
                return True

            print("⚠️ 签到后页面未显示成功信息，尝试刷新页面再次确认")
            driver.refresh()
            time.sleep(2)

            refresh_page_source = driver.page_source
            if "今日已签" in refresh_page_source or "您今天已经签到过了" in refresh_page_source:
                print("✅ 刷新后确认签到成功")
                self.check_in_status = 0
                return True

            self.check_in_status = 2
            print("❌ 签到失败")
            return False
        except Exception as exc:  # noqa: BLE001
            print("❌ 签到过程中出现异常")
            print(exc)
            self.check_in_status = 2
            return False

    def fetch_user_info(self, driver: webdriver.Chrome) -> Optional[str]:
        """拉取签到后的用户信息并返回拼装后的通知文本"""

        try:
            print("🔎 准备获取用户信息...")
            sign_page_url = f"{self.cfg.base_url}{self.cfg.sign_path}"
            print(f"➡️ 访问签到页面: {sign_page_url}")
            driver.get(sign_page_url)

            wait = WebDriverWait(driver, 20)
            wait.until(EC.presence_of_element_located((By.ID, "qiandaobtnnum")))

            qiandao_num = driver.find_element(By.ID, "qiandaobtnnum").get_attribute("value")
            lxdays = driver.find_element(By.ID, "lxdays").get_attribute("value")
            lxtdays = driver.find_element(By.ID, "lxtdays").get_attribute("value")
            lxlevel = driver.find_element(By.ID, "lxlevel").get_attribute("value")
            lxreward = driver.find_element(By.ID, "lxreward").get_attribute("value")

            page_content = driver.page_source
            if "今日已签" in page_content or "您今天已经签到过了" in page_content:
                print("✅ 页面显示今日已签到")
                self.check_in_status = 0
            elif "签到成功" in page_content:
                print("🎉 页面显示签到成功")
                self.check_in_status = 1

            check_in_labels = ["已签到", "签到成功", "签到失败"]
            lxqiandao_content = (
                f"签到排名：{qiandao_num}\n"
                f"签到等级：Lv.{lxlevel}\n"
                f"连续签到：{lxdays} 天\n"
                f"签到总数：{lxtdays} 天\n"
                f"签到奖励：{lxreward}\n"
            )

            profile_url = f"{self.cfg.base_url}/home.php?mod=space"
            print(f"➡️ 访问个人主页: {profile_url}")
            driver.get(profile_url)

            wait.until(EC.presence_of_element_located((By.ID, "ct")))
            driver.save_screenshot("profile_page.png")

            xm = None
            xpaths = [
                '//*[@id="ct"]/div/div[2]/div/div[1]/div[1]/h2',
                "//div[contains(@class, 'h')]/h2",
                "//h2[contains(@class, 'mt')]",
                "//div[contains(@id, 'profile')]//h2",
            ]

            for xpath in xpaths:
                elements = driver.find_elements(By.XPATH, xpath)
                if elements:
                    xm = elements[0].text.strip()
                    print(f"👤 找到用户名: {xm}")
                    break
            if not xm:
                print("⚠️ 警告: 无法获取用户名，将使用默认值")
                xm = "未知用户"

            jf = ww = cp = gx = "未知"
            try:
                stats_container = driver.find_element(By.ID, "psts")
                stats = stats_container.find_elements(By.TAG_NAME, "li")
                for stat in stats:
                    text = stat.text.lower()
                    if "积分" in text:
                        jf = stat.text
                    elif "威望" in text:
                        ww = stat.text
                    elif "车票" in text:
                        cp = stat.text
                    elif "贡献" in text:
                        gx = stat.text
            except Exception:  # noqa: BLE001
                try:
                    all_elements = driver.find_elements(
                        By.XPATH,
                        "//*[contains(text(), '积分') or contains(text(), '威望') or contains(text(), '车票') or contains(text(), '贡献')]",
                    )
                    for element in all_elements:
                        text = element.text.lower()
                        if "积分" in text:
                            jf = element.text
                        elif "威望" in text:
                            ww = element.text
                        elif "车票" in text:
                            cp = element.text
                        elif "贡献" in text:
                            gx = element.text
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ 无法获取详细统计信息: {exc}")

            xm = f"账户【{xm}】".center(24, "=")

            info_text = (
                f"{xm}\n"
                f"签到状态: {check_in_labels[self.check_in_status]} \n"
                f"{lxqiandao_content} \n"
                f"当前积分: {jf}\n"
                f"当前威望: {ww}\n"
                f"当前车票: {cp}\n"
                f"当前贡献: {gx}\n\n"
            )
            print(info_text)
            return info_text
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 获取用户信息失败: {exc}")
            try:
                driver.save_screenshot("error_screenshot.png")
                print("保存错误截图到 error_screenshot.png")
            except Exception:  # noqa: BLE001
                pass
            return None

    def run(self) -> None:
        notify_title = f"司机社签到 - {time.strftime('%Y-%m-%d')}"
        notify_lines = []

        if not self.login():
            message = "❌ 登录失败，脚本结束"
            print(message)
            send(notify_title, message)
            return

        print("✔️ 登录成功，准备启动浏览器执行签到和信息获取")
        notify_lines.append("✔️ 登录成功")
        with self.web_driver() as driver:
            if self.do_sign_in(driver):
                print("✔️ 签到操作完成")
                notify_lines.append("✔️ 签到操作完成")
            else:
                print("❌ 签到操作失败")
                notify_lines.append("❌ 签到操作失败")

            user_info = self.fetch_user_info(driver)
            if user_info:
                notify_lines.append(user_info.strip())
            else:
                notify_lines.append("⚠️ 未能获取用户信息，请检查日志输出")

        notify_content = "\n".join(line for line in notify_lines if line).strip()
        if notify_content:
            send(notify_title, notify_content)


def build_config_from_env() -> Optional[SJSConfig]:
    username = os.getenv("sjs_username")
    password = os.getenv("sjs_password")
    if not username or not password:
        print("❌ 请先配置环境变量 sjs_username 和 sjs_password")
        return None

    ocr_service = os.getenv("ocr_service")
    return SJSConfig(username=username, password=password, ocr_service=ocr_service)


def main() -> None:
    config = build_config_from_env()
    if not config:
        return

    automation = SJSAutomation(config)
    automation.run()


if __name__ == "__main__":
    main()
