"""Provider discovery and registration.

Built-in adapters and third-party plugins use the same registry.  Plugins can be
installed as Python packages exposing the ``dounai_input.providers`` entry point,
or dropped into a local plugin directory as a single ``*.py`` file with a
module-level ``PROVIDER`` object.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from collections.abc import Mapping
from typing import Any

from ..config import Settings
from .base import BaseProvider

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "dounai_input.providers"


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self.errors: list[str] = []

    def register(self, provider: BaseProvider, *, origin: str = "builtin") -> bool:
        provider_id = provider.manifest.id
        if provider_id in self._providers:
            self.errors.append(
                f"Provider id collision for {provider_id!r} from {origin}; the first registration wins."
            )
            logger.warning("provider id collision: %s (%s)", provider_id, origin)
            return False
        self._providers[provider_id] = provider
        logger.info("registered provider %s from %s", provider_id, origin)
        return True

    def get(self, provider_id: str) -> BaseProvider | None:
        return self._providers.get(provider_id)

    def all(self) -> list[BaseProvider]:
        return list(self._providers.values())

    def ids(self) -> list[str]:
        return list(self._providers)

    def __len__(self) -> int:
        return len(self._providers)


def _instantiate(target: Any, config: Mapping[str, Any], origin: str) -> BaseProvider | None:
    try:
        candidate = target(config)
    except Exception as exc:
        logger.warning("provider plugin %s failed to initialize: %s", origin, type(exc).__name__)
        return None
    if not isinstance(candidate, BaseProvider):
        logger.warning("provider plugin %s did not return a BaseProvider", origin)
        return None
    return candidate


def load_entry_point_plugins(settings: Settings, registry: ProviderRegistry) -> None:
    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - platform metadata edge case
        registry.errors.append(f"Could not read plugin entry points: {type(exc).__name__}")
        return
    for entry_point in entry_points:
        try:
            target = entry_point.load()
        except Exception as exc:
            registry.errors.append(
                f"Plugin {entry_point.name!r} failed to load ({type(exc).__name__})."
            )
            logger.warning("plugin %s failed to load", entry_point.name)
            continue
        config = settings.provider_config(entry_point.name)
        if config.get("enabled") is False:
            continue
        provider = _instantiate(target, config, entry_point.name)
        if provider is not None:
            registry.register(provider, origin=f"entrypoint:{entry_point.name}")


def load_directory_plugins(settings: Settings, registry: ProviderRegistry) -> None:
    directories = [settings.plugins_dir, *settings.plugin_paths]
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"dounai_input_plugin_{path.stem}_{abs(hash(path)) & 0xFFFFFF:x}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not create module spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                target = getattr(module, "PROVIDER", None)
                if target is None:
                    raise AttributeError("module-level PROVIDER is missing")
            except Exception as exc:
                registry.errors.append(
                    f"Local plugin {path.name!r} failed to load ({type(exc).__name__})."
                )
                logger.warning("local plugin %s failed to load: %s", path.name, type(exc).__name__)
                continue
            config = settings.provider_config(path.stem)
            if config.get("enabled") is False:
                continue
            provider = _instantiate(target, config, path.name)
            if provider is not None:
                registry.register(provider, origin=f"file:{path.name}")


def build_registry(settings: Settings) -> ProviderRegistry:
    from .custom_http import CustomHttpProvider
    from .local_whisper import LocalWhisperProvider
    from .offline import OfflineProvider
    from .openai_compatible import build_provider

    registry = ProviderRegistry()
    factories: dict[str, Any] = {
        "offline": lambda config: OfflineProvider(config),
        "local-whisper": lambda config: LocalWhisperProvider(
            config, models_dir=settings.models_dir
        ),
        "dashscope": lambda config: build_provider(
            "dashscope", config, models_dir=settings.models_dir
        ),
        "openai": lambda config: build_provider("openai", config, models_dir=settings.models_dir),
        "custom-http": lambda config: CustomHttpProvider(config),
    }
    for provider_id, factory in factories.items():
        config = settings.provider_config(provider_id)
        if not bool(config.get("enabled", False)):
            continue
        try:
            provider = factory(config)
        except Exception as exc:
            registry.errors.append(
                f"Built-in provider {provider_id!r} failed to initialize ({type(exc).__name__})."
            )
            logger.warning("built-in provider %s failed: %s", provider_id, type(exc).__name__)
            continue
        registry.register(provider, origin="builtin")

    load_entry_point_plugins(settings, registry)
    load_directory_plugins(settings, registry)
    return registry
