from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock

import cv2
import numpy as np

from core.capture import decode_png
from core.errors import ClashAgentError


@dataclass(frozen=True)
class PreparedTemplate:
    gray: np.ndarray


class TemplateCache:
    """Bounded, content-addressed cache shared by recognition workers."""

    def __init__(self, max_entries: int = 64, max_bytes: int = 32 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[bytes, PreparedTemplate] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def prepare(self, png: bytes) -> PreparedTemplate:
        key = sha256(png).digest()
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
            gray = cv2.cvtColor(decode_png(png), cv2.COLOR_BGR2GRAY)
            if float(gray.std()) < 1:
                raise ClashAgentError("模板缺少可识别纹理，请选择包含图标或文字的区域。")
            gray.setflags(write=False)
            prepared = PreparedTemplate(gray)
            if gray.nbytes <= self.max_bytes and self.max_entries > 0:
                self._items[key] = prepared
                self._bytes += gray.nbytes
                while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
                    _, removed = self._items.popitem(last=False)
                    self._bytes -= removed.gray.nbytes
            return prepared

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0


DEFAULT_TEMPLATE_CACHE = TemplateCache()
