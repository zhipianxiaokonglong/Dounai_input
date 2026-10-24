"""Offline passthrough provider.

The core package must work without models, API keys or network access.  This
provider advertises the modalities the gateway can accept and performs no
inference; it is the safe default until the user installs or selects a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import BaseProvider, ProviderManifest


class OfflineProvider(BaseProvider):
    manifest = ProviderManifest(
        id="offline",
        name="离线直通（无模型）",
        version="1.0.0",
        description=(
            "零依赖、零网络的直通模式：校验并标准化输入，不做语音转写。"
            "适合首次启动、断网演示和隐私优先的纯文本场景。"
        ),
        capabilities=("text", "image", "audio"),
        privacy="local",
        requires_api_key=False,
        can_transcribe=False,
    )

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)

    def availability(self) -> tuple[bool, str | None]:
        return True, None
