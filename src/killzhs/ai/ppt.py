"""Course slides to text through an OpenAI-compatible file service (Moonshot by default).

Only files uploaded by this instance are managed or deleted; downloads are cached
under the data directory by URL hash.
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("killzhs.ai")

SUFFIXES = (".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt")


class SlidesToText:
    def __init__(self, api_key, base_url, cache_dir, *, session_factory, delete_immediately=False,
                 max_file_mb=100, max_cache_files=500, max_cache_gb=8):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.cache_dir = Path(cache_dir)
        self.session_factory = session_factory
        self.delete_immediately = delete_immediately
        self.max_file_size = max_file_mb * 1024 * 1024
        self.max_cache_files = max_cache_files
        self.max_cache_size = max_cache_gb * 1024 ** 3
        self.uploads = {}

    def text(self, url):
        """The extracted text, or an empty string when anything fails."""
        try:
            path = self.download(url)
            size = path.stat().st_size
            if size > self.max_file_size:
                raise ValueError(f"File size ({size} bytes) exceeds the maximum allowed size ({self.max_file_size} bytes)")
            file_id = self.upload(path)
            content = self.extract(file_id)
            self.trim()
            return content
        except Exception as error:
            log.error("课件转换失败：%s（%s）", url.split("?")[0], error)
            return ""

    def download(self, url):
        suffix = Path(urlparse(url).path).suffix.lower()
        path = self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + (suffix if suffix in SUFFIXES else ".bin"))
        if path.exists():
            return path
        with self.session_factory() as session:
            response = session.get(url, stream=True, timeout=30)
            if response.status_code != 200:
                raise ValueError(f"Failed to download file: {response.status_code}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=8192):
                    stream.write(chunk)
        return path

    def upload(self, path):
        size = path.stat().st_size
        cached = self.uploads.get(path.name)
        if cached and cached["size"] == size:
            return cached["id"]
        uploaded = self.client.files.create(file=path, purpose="file-extract")
        self.uploads[path.name] = {"id": uploaded.id, "size": size, "created_at": datetime.now().timestamp()}
        return uploaded.id

    def extract(self, file_id):
        try:
            content = self.client.files.content(file_id=file_id).text
            try:
                return json.loads(content).get("content", content)
            except (json.JSONDecodeError, AttributeError):
                return content
        finally:
            if self.delete_immediately:
                self.delete(file_id)

    def delete(self, file_id):
        try:
            self.client.files.delete(file_id=file_id)
            self.uploads = {name: item for name, item in self.uploads.items() if item["id"] != file_id}
        except Exception as error:
            log.error("服务端课件删除失败：%s", error)

    def trim(self):
        while self.uploads and (len(self.uploads) > self.max_cache_files
                                or sum(item["size"] for item in self.uploads.values()) > self.max_cache_size):
            name = min(self.uploads, key=lambda key: self.uploads[key]["created_at"])
            self.delete(self.uploads.pop(name)["id"])
