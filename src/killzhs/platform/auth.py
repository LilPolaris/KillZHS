"""QR login; expired codes are replaced without recursion or image files."""

import logging
import time
from base64 import b64decode

log = logging.getLogger("killzhs.platform")
PASSPORT = "https://passport.zhihuishu.com/"
LOGIN = PASSPORT + "login?service=https://onlineservice-api.zhihuishu.com/login/gologin"


class Auth:
    def __init__(self, http):
        self.http = http

    def login(self, qr_callback):
        if not callable(qr_callback):
            raise ValueError("callable qr_callback is required")
        while True:
            self.http.ready()
            response = self.http.session.get(PASSPORT + "qrCodeLogin/getLoginQrImg", timeout=10).json()
            token = response["qrToken"]
            qr_callback(b64decode(response["img"]))
            while True:
                if self.http.control is not None:
                    self.http.control.check()
                time.sleep(0.5)
                response = self.http.session.get(PASSPORT + "qrCodeLogin/getLoginQrInfo",
                                                 params={"qrToken": token}, timeout=10).json()
                status = response.get("status")
                if status in (-1, 0):
                    continue
                if status == 2:
                    break
                if status == 1:
                    self.http.session.get(LOGIN, params={"pwd": response["oncePassword"]},
                                          proxies=self.http.proxies, timeout=10)
                    self.http.cookies = self.http.session.cookies.copy()
                    if not self.http.cookies:
                        raise ValueError("No cookies found")
                    log.info("扫码登录成功。")
                    return
                raise ValueError("登录已取消。" if status == 3 else "扫码登录响应无法识别。")
