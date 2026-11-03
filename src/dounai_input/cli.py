"""Command-line interface for local operation, models and privacy controls."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import secrets
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from . import __version__
from .config import ConfigurationError, Settings, load_settings
from .errors import DounaiError
from .model_catalog import (
    ModelInstallError,
    catalog_entries,
    install_model,
    is_installed,
    remove_model,
    verify_model,
)
from .services import Services, create_services

logger = logging.getLogger("dounai_input.cli")


def main(argv: Sequence[str] | None = None) -> int:
    _load_environment()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    try:
        return _dispatch(args)
    except (ConfigurationError, ModelInstallError, DounaiError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


def _load_environment() -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dounai-input",
        description="豆奶输入层：本地多模态接入、模型切换与隐私控制。",
    )
    parser.add_argument("--config", help="dounai.toml 路径（默认读取当前目录）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 HTTP 服务与 Web 控制台")
    serve.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, help="监听端口（默认 8000）")
    serve.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    serve.add_argument(
        "--access-log",
        action="store_true",
        help="启用 HTTP 访问日志（含媒体路径，可能包含能力 URL，默认关闭）",
    )

    providers = subparsers.add_parser("providers", help="查看与切换模型适配器")
    provider_sub = providers.add_subparsers(dest="provider_command", required=True)
    provider_sub.add_parser("list", help="列出适配器及可用状态")
    activate = provider_sub.add_parser("activate", help="运行时切换当前模型")
    activate.add_argument("provider_id")

    models = subparsers.add_parser("models", help="管理可选本地模型")
    model_sub = models.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list", help="列出本地模型目录与安装状态")
    install = model_sub.add_parser("install", help="下载并安装本地模型")
    install.add_argument("model_id")
    install.add_argument("--yes", action="store_true", help="跳过交互确认")
    install.add_argument("--revision", help="固定 Hugging Face revision")
    verify = model_sub.add_parser("verify", help="校验已安装模型权重")
    verify.add_argument("model_id")
    remove = model_sub.add_parser("remove", help="删除本地模型")
    remove.add_argument("model_id")
    remove.add_argument("--yes", action="store_true")

    privacy = subparsers.add_parser("privacy", help="声纹授权、开关与数据权利")
    privacy_sub = privacy.add_subparsers(dest="privacy_command", required=True)
    privacy_sub.add_parser("status", help="查看隐私状态")
    privacy_sub.add_parser("enable", help="开启声纹功能")
    privacy_sub.add_parser("disable", help="关闭声纹功能")
    consent = privacy_sub.add_parser("consent", help="记录明确授权")
    consent.add_argument("--accept", action="store_true", help="同意当前政策版本")
    consent.add_argument("--policy-version", help="要同意的政策版本（默认当前版本）")
    privacy_sub.add_parser("withdraw", help="撤回授权并删除模板")

    voiceprint = privacy_sub.add_parser("enroll", help="用 WAV 样本录入声纹")
    voiceprint.add_argument("samples", nargs="+", help="3–8 个 PCM WAV 文件")
    voiceprint.add_argument("--subject", default="default")
    privacy_sub.add_parser("delete-voiceprint", help="删除声纹模板")

    data = subparsers.add_parser("data", help="导出或删除个人数据")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    export = data_sub.add_parser("export", help="导出 ZIP（含声纹模板）")
    export.add_argument("--output", required=True, help="输出 .zip 路径")
    export.add_argument("--force", action="store_true", help="覆盖已有文件")
    delete = data_sub.add_parser("delete", help="删除全部个人数据")
    delete.add_argument("--yes", action="store_true", help="跳过交互确认")

    subparsers.add_parser("doctor", help="检查配置、依赖与适配器")
    subparsers.add_parser("token", help="生成一个随机 API Bearer Token")
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "token":
        print(secrets.token_urlsafe(32))
        return 0
    if args.command == "models":
        return _models_command(args)

    settings = _settings_for(args)
    if args.command == "serve":
        return _serve(settings, args)
    services = create_services(settings)
    try:
        if args.command == "providers":
            return _providers_command(services, args)
        if args.command == "privacy":
            return _privacy_command(services, args)
        if args.command == "data":
            return _data_command(services, args)
        if args.command == "doctor":
            return _doctor(settings)
    finally:
        asyncio.run(services.providers.aclose())
    return 1


def _settings_for(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


def _serve(settings: Settings, args: argparse.Namespace) -> int:
    if args.host:
        settings = replace(settings, host=args.host)
    if args.port:
        old_default = f"http://127.0.0.1:{settings.port}"
        settings = replace(settings, port=args.port)
        if settings.public_base_url == old_default:
            settings = replace(settings, public_base_url=f"http://127.0.0.1:{args.port}")
    settings.validate_bind()

    import uvicorn

    from .api import build_app

    app = build_app(settings)
    print("豆奶输入层已就绪")
    print(f"  控制台  http://{settings.host}:{settings.port}/")
    print(f"  数据目录 {settings.data_dir}")
    print(f"  媒体地址 {settings.public_base_url}")
    print(f"  当前模型 {app.state.services['providers'].active_id}")
    for warning in settings.public_base_url_warnings():
        print(f"  警告 {warning}")
    if settings.api_token:
        print("  API 鉴权 已启用")
    else:
        print("  API 鉴权 未启用（仅绑定回环地址时可接受）")
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        access_log=args.access_log,
    )
    return 0


def _providers_command(services: Services, args: argparse.Namespace) -> int:
    manager = services.providers
    if args.provider_command == "list":
        rows = manager.list_providers()
        print(f"{'':2} {'ID':18} {'名称':32} {'能力':16} {'隐私':6} 状态")
        for row in rows:
            marker = "*" if row["active"] else " "
            capabilities = ",".join(row["capabilities"])
            status = "可用" if row["available"] else f"不可用：{row['unavailable_reason']}"
            print(
                f"{marker:2} {row['id']:18} {row['name'][:30]:32} "
                f"{capabilities:16} {row['privacy']:6} {status}"
            )
        for error in manager.registry.errors:
            print(f"插件提示：{error}", file=sys.stderr)
        return 0
    if args.provider_command == "activate":
        info = manager.set_active(args.provider_id)
        print(f"已切换当前模型：{info['name']} ({info['id']})")
        if not info["available"]:
            print("提示：该适配器当前不可用，语音会按降级链处理。", file=sys.stderr)
        return 0
    return 1


def _models_command(args: argparse.Namespace) -> int:
    settings = _settings_for(args)
    settings.ensure_directories()
    if args.model_command == "list":
        print(f"{'ID':16} {'名称':28} {'大小':12} {'状态':8} 说明")
        for entry in catalog_entries(settings.models_dir):
            status = "已安装" if entry["installed"] else "未安装"
            print(
                f"{entry['id']:16} {entry['name'][:26]:28} "
                f"{entry['approximate_size']:12} {status:8} {entry['description']}"
            )
        print("\n安装示例：dounai-input models install small")
        print("安装前需：pip install -e '.[local-asr]'")
        return 0
    if args.model_command == "install":
        return _install_model(settings, args)
    if args.model_command == "verify":
        result = verify_model(settings.models_dir, args.model_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.model_command == "remove":
        spec_exists = is_installed(settings.models_dir, args.model_id)
        if not spec_exists:
            print("该模型未安装。", file=sys.stderr)
            return 1
        if not args.yes and not _confirm("确认删除本地模型权重？"):
            print("已取消。", file=sys.stderr)
            return 1
        remove_model(settings.models_dir, args.model_id)
        print(f"已删除本地模型 {args.model_id}。")
        return 0
    return 1


def _install_model(settings: Settings, args: argparse.Namespace) -> int:
    entry = next(
        (item for item in catalog_entries(settings.models_dir) if item["id"] == args.model_id),
        None,
    )
    if entry is None:
        raise ModelInstallError(f"Unknown local model: {args.model_id}")
    if entry["installed"]:
        print(f"{entry['name']} 已安装，无需重复下载。")
        return 0
    print(f"模型：{entry['name']} ({entry['approximate_size']})")
    print(f"许可证：{entry['license']}")
    print(f"来源：https://huggingface.co/{entry['repo_id']}")
    print(f"目标：{settings.models_dir / ('whisper-' + args.model_id)}")
    if not args.yes and not _confirm("下载模型权重？本仓库不会提交这些文件。"):
        print("已取消。", file=sys.stderr)
        return 1
    result = install_model(
        settings.models_dir,
        args.model_id,
        revision=args.revision,
        progress=lambda message: print(message, flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n激活：dounai-input providers activate local-whisper")
    print("如需换用该模型，在 dounai.toml 的 [providers.local-whisper] 设置 model。")
    return 0


def _privacy_command(services: Services, args: argparse.Namespace) -> int:
    privacy = services.privacy
    command = args.privacy_command
    if command == "status":
        print(json.dumps(privacy.status(), ensure_ascii=False, indent=2))
        return 0
    if command == "enable":
        print(json.dumps(privacy.set_voiceprint_enabled(True), ensure_ascii=False, indent=2))
        return 0
    if command == "disable":
        print(json.dumps(privacy.set_voiceprint_enabled(False), ensure_ascii=False, indent=2))
        return 0
    if command == "consent":
        if not args.accept:
            print("请阅读文档并添加 --accept 表示明确同意。", file=sys.stderr)
            return 2
        version = args.policy_version or services.settings.consent_policy_version
        print(json.dumps(privacy.grant_consent(version), ensure_ascii=False, indent=2))
        return 0
    if command == "withdraw":
        print(json.dumps(privacy.withdraw_consent(), ensure_ascii=False, indent=2))
        return 0
    if command == "enroll":
        paths = [Path(item).expanduser() for item in args.samples]
        if len(paths) < 3 or len(paths) > 8:
            print("请提供 3–8 个 WAV 样本。", file=sys.stderr)
            return 2
        samples = [path.read_bytes() for path in paths]
        result = privacy.enroll(samples, subject=args.subject)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "delete-voiceprint":
        print(json.dumps(privacy.delete_voiceprint(), ensure_ascii=False, indent=2))
        return 0
    return 1


def _data_command(services: Services, args: argparse.Namespace) -> int:
    privacy = services.privacy
    if args.data_command == "export":
        output = Path(args.output).expanduser()
        if output.exists() and not args.force:
            print("输出文件已存在；如需覆盖请加 --force。", file=sys.stderr)
            return 2
        output.parent.mkdir(parents=True, exist_ok=True)
        archive, _ = privacy.export_zip()
        try:
            with output.open("wb") as handle:
                while chunk := archive.read(1024 * 1024):
                    handle.write(chunk)
        finally:
            archive.close()
        with contextlib.suppress(OSError):
            output.chmod(0o600)
        print(f"已导出到 {output}")
        print("压缩包包含声纹模板，请妥善保管。")
        return 0
    if args.data_command == "delete":
        if not args.yes and not _confirm("确认删除全部个人数据（事件、媒体、声纹、授权）？"):
            print("已取消。", file=sys.stderr)
            return 1
        print(json.dumps(privacy.delete_all_data(), ensure_ascii=False, indent=2))
        return 0
    return 1


def _doctor(settings: Settings) -> int:
    print(f"配置：{settings.config_path or '默认值（未找到 dounai.toml）'}")
    print(f"数据目录：{settings.data_dir}")
    print(f"服务地址：http://{settings.host}:{settings.port}")
    print(f"媒体地址：{settings.public_base_url}")
    print(f"API 鉴权：{'已启用' if settings.api_token else '未启用'}")
    problems: list[str] = []
    for warning in settings.public_base_url_warnings():
        problems.append(f"public_base_url: {warning}")
    try:
        settings.ensure_directories()
        probe = settings.data_dir / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("数据目录写入：正常")
    except OSError as exc:
        problems.append(f"数据目录不可写：{exc}")
    services = create_services(settings)
    try:
        print(f"数据库：{settings.database_path}")
        print(f"当前模型：{services.providers.active_id}")
        for row in services.providers.list_providers():
            state = "可用" if row["available"] else f"不可用（{row['unavailable_reason']}）"
            print(f"  - {row['id']}: {state}")
        if not settings.api_token:
            print("提示：绑定非回环地址前必须设置 DOUNAI_API_TOKEN。")
        if services.providers.registry.errors:
            problems.extend(services.providers.registry.errors)
    finally:
        asyncio.run(services.providers.aclose())
    if problems:
        for problem in problems:
            print(f"问题：{problem}", file=sys.stderr)
        return 1
    print("检查完成。")
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print("非交互终端需要显式 --yes。", file=sys.stderr)
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
