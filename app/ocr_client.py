from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

import httpx

from .config import Settings, get_settings


class QianfanOCRClient:
    """Baidu OCR via access token (works with Qianfan AK/SK on Baidu Cloud).

    Uses the general_basic endpoint; suitable for printed text. For more advanced
    forms/table/handwritten, switch the OCR API path accordingly.
    """

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        if not (self.settings.qianfan_access_key and self.settings.qianfan_secret_key):
            raise RuntimeError("Qianfan OCR enabled but AK/SK not configured")
        self._token: Optional[str] = None
        self._token_expiry: float = 0

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        params: dict[str, str] = {
            "grant_type": str(self.settings.qianfan_ocr_grant_type),
            "client_id": str(self.settings.qianfan_access_key),
            "client_secret": str(self.settings.qianfan_secret_key),
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(self.TOKEN_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        token = data.get("access_token")
        expires_in = data.get("expires_in", 0)
        if not token:
            raise RuntimeError(f"Failed to fetch OCR access_token: {data}")
        self._token = token
        self._token_expiry = now + float(expires_in)
        return token

    def extract(self, image_path: Path | str | bytes) -> str:
        token = self._get_token()
        if isinstance(image_path, (str, Path)):
            img_bytes = Path(image_path).read_bytes()
        else:
            img_bytes = image_path
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"image": img_b64}
        url = f"{self.OCR_URL}?access_token={token}"
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, data=data, headers=headers)
            resp.raise_for_status()
            result = resp.json()
        words_result = result.get("words_result", [])
        if not words_result:
            return ""
        return "\n".join([item.get("words", "") for item in words_result])


def get_ocr_client(settings: Optional[Settings] = None) -> QianfanOCRClient:
    settings = settings or get_settings()
    if not settings.use_qianfan_ocr:
        raise RuntimeError("Qianfan OCR is disabled in settings")
    return QianfanOCRClient(settings)
