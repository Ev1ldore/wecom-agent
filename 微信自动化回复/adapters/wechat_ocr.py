"""Read personal-WeChat desktop messages through screen capture and OCR.

Each read finds the on-screen WeChat main window, captures it with Quartz,
runs local RapidOCR with bundled Chinese models, and classifies
the observations into time separators, peer bubbles, own bubbles and
system lines purely by geometry.  It never opens WeChat's private data
files and never fabricates time: a bubble without a preceding visible
time separator is skipped with a visible counter instead of being stamped
with a local receipt time.
"""

import asyncio
import hashlib
import io
import json
import math
import re
import shutil
import statistics
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from structlog.contextvars import get_contextvars

from adapters.inbox import InboxError, Msg
from adapters.personal_tail import TailProtocol
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.logging import get_logger

READ_TIMEOUT_SECONDS = 20.0
MIN_WINDOW_WIDTH = 560.0
MIN_WINDOW_HEIGHT = 480.0
TITLE_BAND_BOTTOM = 0.06
COMPOSER_BAND_TOP = 0.78
COMPOSER_FOOTER_TOP = 0.92
CHAT_RIGHT_EDGE_REF = 0.99
MIN_TEXT_CONFIDENCE = 0.3
SEPARATOR_MAX_WIDTH = 0.20
SEPARATOR_MIN_X_CENTER = 0.45
CENTER_TOLERANCE = 0.06
SYSTEM_MIN_WIDTH = 0.15
SELF_RIGHT_EDGE = 0.86
SYSTEM_RIGHT_LIMIT = 0.87
PEER_LEFT_MARGIN = 0.12
BUBBLE_MAX_GAP_RATIO = 0.6
MAX_BUBBLE_LINES = 20
MAX_OBSERVATIONS = 2000

TIME_LABEL_RE = re.compile(
    r"^(?:(?:今天|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日)\s*)?"
    r"(?:(?:上午|下午|晚上|早上|中午|凌晨|傍晚)\s*)?"
    r"\d{1,2}:\d{2}$"
)
OWNER_HINTS = ("WeChat", "微信")
WECHAT_APP_PATH = "/Applications/微信 2.app"
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat2"
WECHAT_DATA_ROOT = (
    Path.home()
    / "Library"
    / "Containers"
    / WECHAT_BUNDLE_ID
    / "Data"
    / "Documents"
    / "xwechat_files"
)
SECONDARY_WINDOW_HINTS = ("窗口", "(Window", "(window")
TEXT_NORMALIZER_RE = re.compile(r"[\s，。、！？；：,.!?;:~·—（）()\[\]【】\x27\x22]+")

logger = get_logger(__name__)
_OCR_ENGINE_LOCK = threading.Lock()

_ERRORS = {
    "NO_WINDOW": "微信没有可读取的聊天主窗口；请先打开并登录微信。",
    "CAPTURE_DENIED": "无法读取 wx2 窗口图像；需核验采集权限与客户端状态。",
    "CAPTURE_FAILED": "微信窗口截图失败；本轮未读取任何消息。",
    "CAPTURE_EMPTY": "wx2 截图为空白或透明图；本轮未进行文字识别。",
    "OCR_SETUP": "本地 RapidOCR 或随包模型不完整；未下载模型或读取聊天。",
    "OCR_FAILED": "微信窗口文字识别失败；本轮未读取任何消息。",
    "NO_TIME_ANCHOR": "当前聊天窗口中没有可见的原始时间分隔；已停止读取，未伪造时间。",
    "TITLE_MISMATCH": "当前打开的会话不是已确认的 WECHAT_OCR_CONTACT 单聊；已停止读取。",
    "AMBIGUOUS_LAYOUT": "微信聊天窗口布局无法确认；已停止读取。",
    "READ_TIMEOUT": "微信窗口 OCR 只读快照超时。",
    "READ_FAILED": "微信窗口 OCR 只读快照失败。",
    "WINDOW_CHANGED": "wx2 的进程、窗口或位置发生变化；本轮已暂停。",
    "CLI_SETUP": "wx-cli 尚未配置并核实为 wx2 账号；不启用其他收件路径。",
    "CLI_FAILED": "wx-cli 读取失败或超时；本轮暂停，不回退到其他账号。",
    "CLI_STALE": "wx-cli 数据新鲜度无法确认；本轮暂停。",
}


class WeChatOCRError(InboxError):
    """Expose only an allowlisted code and fixed explanation without raw data."""

    def __init__(self, code: str) -> None:
        """Discard capture, OCR and private chat text from error surfaces."""
        self.code: str = code if code in _ERRORS else "READ_FAILED"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class OCRObservation:
    """Hold one recognized text line with a normalized top-left-origin box."""

    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0
    bubble_side: str | None = None

    @property
    def x_center(self) -> float:
        """Return the horizontal center of this observation."""
        return self.x + self.w / 2


@dataclass(frozen=True)
class OCRChatMessage:
    """Describe one verified chat bubble observed through OCR."""

    text: str
    is_self: bool
    time_display: str


@dataclass(frozen=True)
class OCRParseStats:
    """Count every skipped observation so nothing is dropped silently."""

    system: int = 0
    unanchored: int = 0
    sidebar: int = 0
    ambiguous: int = 0


@dataclass(frozen=True)
class OCRParseResult:
    """Return parsed messages together with visible skip accounting."""

    messages: tuple[OCRChatMessage, ...]
    stats: OCRParseStats


def normalize_text(value: str) -> str:
    """Ignore only whitespace; preserve punctuation, numbers and letter case."""
    return re.sub(r"\s+", "", value)


def chat_identity(contact: str) -> str:
    """Name the verified OCR chat without inventing a server identity."""
    if not isinstance(contact, str) or not contact.strip():
        raise ValueError("A non-empty verified contact name is required")
    return f"desktop:wechat-ocr:{contact.strip()}"


def _is_separator(obs: OCRObservation) -> bool:
    """Accept only narrow, right-half time labels as original time separators."""
    compact = re.sub(r"\s+", "", obs.text)
    if not TIME_LABEL_RE.fullmatch(compact):
        return False
    return (
        obs.w <= SEPARATOR_MAX_WIDTH
        and obs.x_center >= SEPARATOR_MIN_X_CENTER
        and obs.x_center <= CHAT_RIGHT_EDGE_REF
    )


def _line_side(obs: OCRObservation, chat_center: float, chat_left: float) -> str | None:
    """Classify one body line as peer, own or system/ignored by geometry only."""
    if obs.bubble_side == "self":
        return "self"
    if obs.x < chat_left + PEER_LEFT_MARGIN:
        return "peer"
    centered = abs(obs.x_center - chat_center) <= CENTER_TOLERANCE
    if (
        centered
        and obs.w >= SYSTEM_MIN_WIDTH
        and obs.x_center + obs.w / 2 < SYSTEM_RIGHT_LIMIT
    ):
        return "system"
    if obs.x + obs.w > SELF_RIGHT_EDGE:
        return "self"
    if centered and obs.w >= SYSTEM_MIN_WIDTH:
        return "system"
    return None


def parse_chat_observations(
    observations: list[OCRObservation], *, contact: str
) -> OCRParseResult:
    """Map raw OCR lines to verified bubbles, or fail closed on layout doubt.

    Geometry anchors are derived from the visible time separators, so a
    window whose separators cannot be found never yields messages.
    """
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise WeChatOCRError("AMBIGUOUS_LAYOUT")
    if not all(isinstance(item, OCRObservation) for item in observations):
        raise WeChatOCRError("AMBIGUOUS_LAYOUT")
    if any(
        not all(math.isfinite(v) for v in (o.x, o.y, o.w, o.h, o.confidence))
        or min(o.x, o.y, o.w, o.h) < 0
        or o.x + o.w > 1.01
        or o.y + o.h > 1.01
        or o.confidence < MIN_TEXT_CONFIDENCE
        for o in observations
    ):
        raise WeChatOCRError("AMBIGUOUS_LAYOUT")

    title_lines = sorted(
        (o for o in observations if o.y < TITLE_BAND_BOTTOM),
        key=lambda o: o.x,
    )
    body = [
        o
        for o in observations
        if o.y >= TITLE_BAND_BOTTOM and o.y + o.h / 2 <= COMPOSER_BAND_TOP
    ]
    separators = [o for o in body if _is_separator(o)]
    if not separators:
        raise WeChatOCRError("NO_TIME_ANCHOR")
    chat_center = statistics.median(o.x_center for o in separators)
    chat_left = max(0.30, 2 * chat_center - CHAT_RIGHT_EDGE_REF)

    title_band = "".join(
        o.text
        for o in title_lines
        if o.x_center >= chat_left
        and not (o.x > 0.85 and o.text.strip() in {"•••", "⋯", "…", "..."})
    )
    title_norm = normalize_text(title_band)
    contact_norm = normalize_text(contact)
    if not title_norm or contact_norm != title_norm:
        raise WeChatOCRError("TITLE_MISMATCH")

    system = unanchored = sidebar = ambiguous = 0
    lines: list[tuple[str, OCRObservation]] = []
    for obs in body:
        if _is_separator(obs):
            continue
        if obs.x_center < chat_left:
            sidebar += 1
            continue
        side = _line_side(obs, chat_center, chat_left)
        if side == "system":
            system += 1
            continue
        if side is None:
            ambiguous += 1
            continue
        lines.append((side, obs))
    lines.sort(key=lambda item: (item[1].y, item[1].x))

    bubbles: list[dict[str, Any]] = []
    for side, obs in lines:
        gap = None
        if bubbles and bubbles[-1]["side"] == side:
            previous = bubbles[-1]["lines"][-1]
            gap = obs.y - (previous.y + previous.h)
            max_gap = max(0.012, BUBBLE_MAX_GAP_RATIO * previous.h)
        if (
            bubbles
            and bubbles[-1]["side"] == side
            and gap is not None
            and gap <= max_gap
        ):
            if len(bubbles[-1]["lines"]) >= MAX_BUBBLE_LINES:
                raise WeChatOCRError("AMBIGUOUS_LAYOUT")
            bubbles[-1]["lines"].append(obs)
        else:
            bubbles.append({"side": side, "lines": [obs]})

    ordered_separators = sorted(separators, key=lambda o: o.y)
    messages: list[OCRChatMessage] = []
    for bubble in bubbles:
        top = min(o.y for o in bubble["lines"])
        anchor = None
        for sep in ordered_separators:
            if sep.y + sep.h <= top + 0.005:
                anchor = sep
        if anchor is None:
            unanchored += 1
            continue
        text = "\n".join(o.text for o in bubble["lines"]).strip()
        if not text:
            unanchored += 1
            continue
        messages.append(
            OCRChatMessage(
                text=text,
                is_self=bubble["side"] == "self",
                time_display=re.sub(r"\s+", " ", anchor.text).strip(),
            )
        )
    logger.info(
        "wechat_ocr_parsed",
        msg_id=get_contextvars().get("msg_id"),
        observations=len(observations),
        messages=len(messages),
        system=system,
        unanchored=unanchored,
        sidebar=sidebar,
        ambiguous=ambiguous,
    )
    return OCRParseResult(
        messages=tuple(messages),
        stats=OCRParseStats(
            system=system, unanchored=unanchored, sidebar=sidebar, ambiguous=ambiguous
        ),
    )


def _load_quartz() -> Any:
    """Import Quartz lazily so tests and non-mac hosts can still import."""
    try:
        import Quartz
    except ImportError as exc:  # pragma: NO COVER - depends on host platform.
        raise WeChatOCRError("READ_FAILED") from exc
    return Quartz


@lru_cache(maxsize=1)
def _load_rapidocr() -> Any:
    """Use explicit bundled model paths so inference cannot download models."""
    try:
        import onnxruntime
        import rapidocr

        onnxruntime.disable_telemetry_events()
        model_dir = Path(rapidocr.__file__).resolve().parent / "models"
        names = {
            "Det": "PP-OCRv6_det_small.onnx",
            "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "Rec": "PP-OCRv6_rec_small.onnx",
        }
        if not all((model_dir / name).is_file() for name in names.values()):
            raise WeChatOCRError("OCR_SETUP")
        return rapidocr.RapidOCR(
            params={
                **{
                    f"{stage}.model_path": str(model_dir / name)
                    for stage, name in names.items()
                },
                "Global.text_score": 0.0,
                "Global.log_level": "critical",
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "EngineConfig.onnxruntime.use_coreml": False,
                "EngineConfig.onnxruntime.use_cuda": False,
            }
        )
    except Exception:  # noqa: BLE001 - Installation details stay out of logs.
        raise WeChatOCRError("OCR_SETUP") from None


def _load_appkit() -> Any:
    """Import AppKit lazily so tests and non-mac hosts can still import."""
    try:
        import AppKit
    except ImportError as exc:  # pragma: NO_COVER - depends on host platform.
        raise WeChatOCRError("CAPTURE_FAILED") from exc
    return AppKit


@dataclass(frozen=True)
class WeChatWindow:
    """Bind exactly one wx2 process and its on-screen main-window geometry."""

    window_id: int
    pid: int
    x: float
    y: float
    width: float
    height: float


def find_wechat_window() -> WeChatWindow:
    """Match the exact app path and bundle ID, refusing multiple candidate windows."""
    quartz = _load_quartz()
    appkit = _load_appkit()
    apps = [
        app
        for app in appkit.NSWorkspace.sharedWorkspace().runningApplications()
        if str(app.bundleIdentifier()) == WECHAT_BUNDLE_ID
        and app.bundleURL() is not None
        and str(app.bundleURL().path()) == WECHAT_APP_PATH
    ]
    if len(apps) != 1:
        raise WeChatOCRError("NO_WINDOW")
    pid = int(apps[0].processIdentifier())
    infos = quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionAll, quartz.kCGNullWindowID
    )
    candidates: list[WeChatWindow] = []
    for info in infos or []:
        if int(info.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        if info.get("kCGWindowLayer", 1) != 0:
            continue
        if not info.get("kCGWindowIsOnscreen", False):
            continue
        name = str(info.get("kCGWindowName") or "")
        if any(hint in name for hint in SECONDARY_WINDOW_HINTS):
            continue
        bounds = info.get("kCGWindowBounds") or {}
        width = float(bounds.get("Width") or 0)
        height = float(bounds.get("Height") or 0)
        if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
            continue
        candidates.append(
            WeChatWindow(
                int(info["kCGWindowNumber"]),
                pid,
                float(bounds.get("X", 0)),
                float(bounds.get("Y", 0)),
                width,
                height,
            )
        )
    if len(candidates) != 1:
        raise WeChatOCRError("NO_WINDOW")
    return candidates[0]


def find_wechat_window_id() -> int:
    """Return the unique wx2 main window; never select the other WeChat instance."""
    return find_wechat_window().window_id


def capture_window_png(window_id: int) -> bytes:
    """Capture one window at best resolution and return PNG bytes."""
    quartz = _load_quartz()
    appkit = _load_appkit()
    image = quartz.CGWindowListCreateImage(
        quartz.CGRectNull,
        quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        quartz.kCGWindowImageBoundsIgnoreFraming | quartz.kCGWindowImageBestResolution,
    )
    if image is None:
        raise WeChatOCRError("CAPTURE_DENIED")
    rep = appkit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    data = rep.representationUsingType_properties_(
        appkit.NSBitmapImageFileTypePNG, None
    )
    if data is None or not data.length():
        raise WeChatOCRError("CAPTURE_FAILED")
    return bytes(data)


def _green_bubble(bitmap: Any, observation: OCRObservation) -> bool:
    """Use background pixels to retain ownership of long, left-reaching own bubbles."""
    width, height = int(bitmap.pixelsWide()), int(bitmap.pixelsHigh())
    green = total = 0
    for fx in (0.05, 0.25, 0.5, 0.75, 0.95):
        for fy in (0.15, 0.5, 0.85):
            x = min(
                width - 1, max(0, int((observation.x + fx * observation.w) * width))
            )
            y = min(
                height - 1, max(0, int((observation.y + fy * observation.h) * height))
            )
            color = bitmap.colorAtX_y_(x, y)
            if color is None:
                continue
            red, g, blue = (
                float(color.redComponent()),
                float(color.greenComponent()),
                float(color.blueComponent()),
            )
            total += 1
            green += g > 0.5 and g - red > 0.12 and g - blue > 0.12
    return total >= 10 and green / total > 0.4


def ocr_png(png: bytes) -> list[OCRObservation]:
    """Recognize local PNG bytes and adapt pixel quadrilaterals to chat geometry."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise ValueError("Not PNG")
            width, height = image.size
            extrema = image.convert("RGB").getextrema()
            if all(low == high for low, high in extrema) or (
                "A" in image.getbands() and image.getchannel("A").getextrema()[1] == 0
            ):
                raise WeChatOCRError("CAPTURE_EMPTY")
        bitmap = _load_appkit().NSBitmapImageRep.imageRepWithData_(png)
        if bitmap is None:
            raise ValueError("Invalid native bitmap")
    except WeChatOCRError:
        raise
    except Exception:  # noqa: BLE001 - Invalid inputs expose no image payload.
        raise WeChatOCRError("CAPTURE_FAILED") from None

    try:
        with _OCR_ENGINE_LOCK:
            result = _load_rapidocr()(png)
        if (
            result.boxes is None
            or result.txts is None
            or result.scores is None
            or not 0 < len(result.boxes) <= MAX_OBSERVATIONS
            or len(result.boxes) != len(result.txts)
            or len(result.boxes) != len(result.scores)
        ):
            raise ValueError("Invalid OCR result")
        observations: list[OCRObservation] = []
        for box, text, score in zip(
            result.boxes, result.txts, result.scores, strict=True
        ):
            if len(box) != 4 or any(len(point) != 2 for point in box):
                raise ValueError("Invalid quadrilateral")
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            confidence = float(score)
            if (
                not isinstance(text, str)
                or not text.strip()
                or not all(math.isfinite(v) for v in [*xs, *ys, confidence])
                or not 0 <= confidence <= 1
                or not 0 <= min(xs) < max(xs) <= width
                or not 0 <= min(ys) < max(ys) <= height
            ):
                raise ValueError("Invalid OCR line")
            observation = OCRObservation(
                text=text,
                x=min(xs) / width,
                y=min(ys) / height,
                w=(max(xs) - min(xs)) / width,
                h=(max(ys) - min(ys)) / height,
                confidence=confidence,
            )
            if (
                TITLE_BAND_BOTTOM <= observation.y < COMPOSER_BAND_TOP
                and observation.x > 0.35
                and _green_bubble(bitmap, observation)
            ):
                observation = OCRObservation(
                    observation.text,
                    observation.x,
                    observation.y,
                    observation.w,
                    observation.h,
                    observation.confidence,
                    "self",
                )
            observations.append(observation)
        return observations
    except WeChatOCRError:
        raise
    except Exception:  # noqa: BLE001 - Inference errors can contain private image data.
        raise WeChatOCRError("OCR_FAILED") from None


class WeChatOCRChatReader(Protocol):
    """Expose bounded read-only OCR reads of the verified chat window."""

    async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
        """Return verified bubbles for the open verified chat or raise safely."""
        raise NotImplementedError


class WeChatChangeProbe(Protocol):
    """Expose a read-only CLI snapshot token without consuming a global cursor."""

    async def read_token(self) -> str:
        """Return one fresh history fingerprint or fail closed."""
        raise NotImplementedError


class WxCLIChangeProbe:
    """Use idempotent wx history polling to trigger OCR, never as a send authority."""

    def __init__(self, *, contact: str, workdir: str, account_verified: bool) -> None:
        """Require an explicitly verified wx2 CLI working directory and config."""
        self._contact = contact
        self._workdir = Path(workdir).expanduser().resolve()
        self._binary = shutil.which("wx")
        if (
            account_verified is not True
            or not self._binary
            or not (self._workdir / "config.json").is_file()
        ):
            raise WeChatOCRError("CLI_SETUP")
        try:
            config_bytes = (self._workdir / "config.json").read_bytes()
            config = json.loads(config_bytes)
            data_dir = Path(config["db_dir"]).expanduser()
            if not data_dir.is_absolute():
                data_dir = self._workdir / data_dir
            keys_file = Path(config["keys_file"]).expanduser()
            if not keys_file.is_absolute():
                keys_file = self._workdir / keys_file
            if (
                not data_dir.resolve().is_relative_to(WECHAT_DATA_ROOT.resolve())
                or not data_dir.is_dir()
                or not keys_file.is_file()
                or not isinstance(config.get("your_wxid"), str)
                or not config["your_wxid"].strip()
            ):
                raise WeChatOCRError("CLI_SETUP")
            self._config_token = hashlib.sha256(config_bytes).hexdigest()
        except (OSError, ValueError, KeyError, TypeError):
            raise WeChatOCRError("CLI_SETUP") from None

    @staticmethod
    def payload_token(payload: Any) -> str:
        """Require fresh metadata; fingerprint data without volatile meta fields."""
        if not isinstance(payload, dict):
            raise WeChatOCRError("CLI_STALE")
        meta = payload.get("meta")
        if (
            not isinstance(meta, dict)
            or meta.get("status") != "ok"
            or meta.get("unknown_shards")
        ):
            raise WeChatOCRError("CLI_STALE")
        data = {key: value for key, value in payload.items() if key != "meta"}
        if not isinstance(data.get("messages"), list) or not all(
            isinstance(row, dict) for row in data["messages"]
        ):
            raise WeChatOCRError("CLI_FAILED")
        return hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def read_token(self) -> str:
        """Retry one read-only query once, bounded and without private diagnostics."""
        try:
            if (
                hashlib.sha256((self._workdir / "config.json").read_bytes()).hexdigest()
                != self._config_token
            ):
                raise WeChatOCRError("CLI_SETUP")
        except OSError:
            raise WeChatOCRError("CLI_SETUP") from None
        for attempt in range(2):
            process = None
            drained = False
            try:
                async with asyncio.timeout(25):
                    process = await asyncio.create_subprocess_exec(
                        self._binary,
                        "history",
                        self._contact,
                        "-n",
                        "200",
                        "--json",
                        "--with-meta",
                        cwd=str(self._workdir),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=IS_POSIX,
                    )
                    stdout, _ = await process.communicate()
                    drained = True
                    if process.returncode != 0 or len(stdout) > 2_000_000:
                        raise WeChatOCRError("CLI_FAILED")
                    return self.payload_token(json.loads(stdout))
            except WeChatOCRError as exc:
                if exc.code == "CLI_STALE":
                    raise
            except (OSError, TimeoutError, ValueError):
                pass
            finally:
                if process is not None and not drained:
                    await stop_cli_process(process, isolated_session=IS_POSIX)
            logger.warning(
                "wx_cli_read_failed",
                msg_id=get_contextvars().get("msg_id"),
                attempt=attempt + 1,
                retry=attempt == 0,
            )
        raise WeChatOCRError("CLI_FAILED")


class DesktopWeChatOCRReader:
    """Run window discovery, capture and OCR with one bounded retry."""

    def __init__(
        self,
        *,
        window_finder: Callable[[], int] = find_wechat_window_id,
        capture: Callable[[int], bytes] = capture_window_png,
        ocr: Callable[[bytes], list[OCRObservation]] = ocr_png,
        attempts: int = 2,
        probe: WeChatChangeProbe | None = None,
        select_contact: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Inject every stage so tests never touch the window server."""
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self._window_finder = window_finder
        self._capture = capture
        self._ocr = ocr
        self._attempts = attempts
        self._probe = probe
        self._select_contact = select_contact
        self._last_token: str | None = None
        self._last_messages: list[OCRChatMessage] | None = None
        self._bound_window_id: int | None = None

    async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
        """Retry transient capture/OCR failures once; guards never retry."""
        failure = WeChatOCRError("READ_FAILED")
        token = await self._probe.read_token() if self._probe is not None else None
        if (
            token is not None
            and token == self._last_token
            and self._last_messages is not None
        ):
            return list(self._last_messages)
        if self._select_contact is not None:
            await self._select_contact()
        for attempt in range(self._attempts):
            try:
                async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                    window_id = await asyncio.to_thread(self._window_finder)
                    if (
                        self._bound_window_id is not None
                        and window_id != self._bound_window_id
                    ):
                        raise WeChatOCRError("WINDOW_CHANGED")
                    png = await asyncio.to_thread(self._capture, window_id)
                    observations = await asyncio.to_thread(self._ocr, png)
                    result = parse_chat_observations(observations, contact=contact)
                    second_png = await asyncio.to_thread(self._capture, window_id)
                    second = parse_chat_observations(
                        await asyncio.to_thread(self._ocr, second_png), contact=contact
                    )
                    if second.messages != result.messages:
                        raise WeChatOCRError("AMBIGUOUS_LAYOUT")
                self._bound_window_id = window_id
                self._last_token = token
                self._last_messages = list(result.messages)
                return list(result.messages)
            except WeChatOCRError as exc:
                failure = exc
                if exc.code in {
                    "NO_TIME_ANCHOR",
                    "TITLE_MISMATCH",
                    "AMBIGUOUS_LAYOUT",
                    "NO_WINDOW",
                    "CAPTURE_DENIED",
                    "CAPTURE_EMPTY",
                    "OCR_SETUP",
                    "WINDOW_CHANGED",
                }:
                    break
            except TimeoutError:
                failure = WeChatOCRError("READ_TIMEOUT")
            except Exception:  # noqa: BLE001 - Native diagnostics can contain chat text.
                failure = WeChatOCRError("READ_FAILED")
            logger.warning(
                "wechat_ocr_read_failed",
                msg_id=get_contextvars().get("msg_id"),
                code=failure.code,
                attempt=attempt + 1,
                retry=attempt + 1 < self._attempts,
            )
        raise failure


class WeChatOCRInbox(TailProtocol):
    """Map verified OCR bubbles to normalized minute-precision messages."""

    def __init__(
        self,
        *,
        contact: str,
        reader: WeChatOCRChatReader | None = None,
    ) -> None:
        """Require one verified contact name; durable dedup belongs to the tail."""
        self._identity = chat_identity(contact)
        self._reader = reader if reader is not None else DesktopWeChatOCRReader()

    async def baseline(self) -> list[Msg]:
        """Read one snapshot; the caller decides which rows become the baseline."""
        return await self._read_messages()

    async def poll_unread(self) -> list[Msg]:
        """Read one snapshot; the tail layer turns unchanged prefixes into no-ops."""
        return await self._read_messages()

    async def _read_messages(self) -> list[Msg]:
        """Convert verified bubbles, keeping the owner's own bubbles for anchoring."""
        rows = await self._reader.read_chat(contact=self._identity.rsplit(":", 1)[-1])
        messages: list[Msg] = []
        for row in rows:
            is_self = bool(row.is_self)
            messages.append(
                Msg(
                    chat=self._identity,
                    sender="desktop:self" if is_self else self._identity,
                    content=row.text,
                    ts=None,
                    time_precision="minute",
                    time_display=row.time_display,
                    chat_type="single",
                    is_self=is_self,
                )
            )
        return messages


async def read_composer_band_text() -> str:
    """Read composer text, excluding only the separately located send button."""
    window_id = await asyncio.to_thread(find_wechat_window_id)
    png = await asyncio.to_thread(capture_window_png, window_id)
    observations = await asyncio.to_thread(ocr_png, png)
    band = sorted(
        (o for o in observations if o.y > COMPOSER_BAND_TOP), key=lambda o: (o.y, o.x)
    )
    if any(o.confidence < MIN_TEXT_CONFIDENCE for o in band):
        raise WeChatOCRError("AMBIGUOUS_LAYOUT")
    buttons = [o for o in band if o.text.strip() == "发送" and o.x > 0.85]
    if len(buttons) != 1:
        raise WeChatOCRError("AMBIGUOUS_LAYOUT")
    band = [o for o in band if o.y < COMPOSER_FOOTER_TOP]
    return "".join(o.text for o in band)


async def verify_own_bubble(
    reader: WeChatOCRChatReader,
    *,
    contact: str,
    draft: str,
    before: list[OCRChatMessage] | None = None,
) -> bool:
    """Require an unchanged prefix and a newly appended, exactly matching own bubble."""
    wanted = normalize_text(draft)
    if not wanted or before is None:
        return False
    rows = await reader.read_chat(contact=contact)
    if len(rows) <= len(before) or rows[: len(before)] != before:
        return False
    return any(
        row.is_self and wanted == normalize_text(row.text)
        for row in rows[len(before) :]
    )
