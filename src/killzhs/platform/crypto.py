"""Request encryption, progress encoding and campus signatures used by the platform pages."""

from base64 import b64encode
from datetime import timedelta
from hashlib import md5

from Crypto.Cipher import AES

IV = b"1g3qqdh4jvbskb9x"
HOME_KEY = b"7q9oko0vqb3la20r"
AI_KEY = b"hw2fdlwcj4cs1mx7"
VIDEO_KEY = b"azp53h0kft7qi78q"
EXAM_KEY = b"onbfhdyvz8x7otrp"
HIKE_SALT = "o6xpt3b#Qy$Z"


class Cipher:
    def __init__(self, key=VIDEO_KEY):
        self.key = key

    def encrypt(self, text):
        # The pages pad by characters; every payload is ASCII-only JSON, so this equals PKCS#7.
        padding = 16 - len(text) % 16
        data = (text + chr(padding) * padding).encode()
        return b64encode(AES.new(self.key, AES.MODE_CBC, IV).encrypt(data)).decode()


def hms(seconds):
    return str(timedelta(seconds=seconds))


def encode_ev(values, key="zzpttjd"):
    """XOR the ``;``-joined values with a repeating key, two hex digits per character."""
    text = ";".join(map(str, values))
    return "".join(f"{ord(character) ^ ord(key[index % len(key)]):02x}"[-4:] for index, character in enumerate(text))


class WatchPoints:
    """The comma-separated watch-point trail reported with shared-course progress."""

    INTERVAL = 2

    def __init__(self, start=0):
        self.reset(start)

    def reset(self, start=0):
        self.points = [0, 1]
        self.last = int(start) or 1

    def add(self, end):
        end = int(end)
        start, self.last = self.last, end
        self.points.extend(second // 5 + 2 for second in range(start, end + 1)[::self.INTERVAL])

    def __str__(self):
        return ",".join(map(str, self.points))


def hike_signature(params):
    keys = ("uuid", "courseId", "fileId", "studyTotalTime", "startDate", "endDate", "endWatchTime",
            "startWatchTime", "uuid")
    return md5((HIKE_SALT + "".join(params[key] for key in keys)).encode()).hexdigest()
