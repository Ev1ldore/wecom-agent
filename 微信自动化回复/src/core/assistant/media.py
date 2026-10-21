import csv
import io
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET


class MediaProtocol(Protocol):
    def extract(self, relative: str, kind: str) -> tuple[list[dict], str]:
        """Return local content with provenance and coverage limits."""
        raise NotImplementedError


class MediaError(ValueError):
    pass


class Media:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.config = config

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise MediaError(
                "附件必须是 inbox 内存在的文件，不能越界或通过符号链接越界"
            )
        if path.stat().st_size > 25 * 1024 * 1024:
            raise MediaError("附件超过25 MiB限制")
        return path

    def run(self, args, timeout=120):
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        for attempt in range(2):
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    check=True,
                )
                return result.stdout
            except (OSError, subprocess.SubprocessError):
                if attempt:
                    raise MediaError("本地解析器失败或超时，已重试一次") from None
        raise AssertionError("unreachable")

    def duration(self, path):
        text = self.run(
            [
                self.config["ffprobe"],
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            15,
        )
        duration = float(text.strip())
        if not 0 < duration <= 180:
            raise MediaError("音视频仅接收0至180秒内容")
        return duration

    def audio(self, path):
        self.duration(path)
        model = Path(self.config.get("asr_model", ""))
        if (
            not (model / "config.json").is_file()
            or not (model / "weights.safetensors").is_file()
        ):
            raise MediaError("缺少本地 Whisper 权重，不会自动联网下载")
        worker = Path(__file__).with_name("transcribe.py")
        with tempfile.TemporaryDirectory() as temp:
            normalized = Path(temp) / "normalized.wav"
            self.run(
                [
                    self.config["ffmpeg"],
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(normalized),
                ],
                30,
            )
            data = json.loads(
                self.run(
                    [
                        self.config["asr_python"],
                        str(worker),
                        str(normalized),
                        str(model),
                    ],
                    180,
                )
            )
        text = data.get("text", "").strip()
        if not text:
            raise MediaError("没有识别到语音")
        return text, "本地Whisper转写；人名、时间与金额需核对；不提供身份识别"

    def image(self, path):
        executable = Path(self.config.get("ocr_binary", ""))
        if not executable.is_file():
            raise MediaError("本地OCR未编译，请运行 scripts/build_assistant_ocr.sh")
        result = json.loads(self.run([str(executable.resolve()), str(path)], 45))
        text = "\n".join(x["text"] for x in result)
        if not text:
            raise MediaError("未识别到文字；本版不推断图片中的物体或人物")
        return text, "macOS Vision OCR；仅文字识别，非通用视觉理解"

    def extract(self, relative, kind):
        path = self.path(relative)
        ext = path.suffix.lower()
        if kind == "voice":
            if ext not in {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}:
                raise MediaError("语音格式不支持；微信原始编码需在接入层转换")
            text, evidence = self.audio(path)
            chunks = [{"source": path.name + ":转写", "text": text}]
        elif kind == "image":
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".tiff"}:
                raise MediaError("图片格式不支持")
            text, evidence = self.image(path)
            chunks = [{"source": path.name + ":OCR", "text": text}]
        elif kind == "video":
            if ext not in {".mp4", ".mov", ".m4v"}:
                raise MediaError("视频格式不支持")
            duration = self.duration(path)
            chunks = []
            with tempfile.TemporaryDirectory() as temp:
                for n, position in enumerate((0, duration / 2, duration * 0.8)):
                    frame = Path(temp) / f"{n}.png"
                    self.run(
                        [
                            self.config["ffmpeg"],
                            "-v",
                            "error",
                            "-protocol_whitelist",
                            "file,pipe",
                            "-ss",
                            str(position),
                            "-i",
                            str(path),
                            "-frames:v",
                            "1",
                            str(frame),
                        ],
                        30,
                    )
                    try:
                        text, _ = self.image(frame)
                    except MediaError as exc:
                        text = "[帧文字不可读] " + str(exc)
                    chunks.append(
                        {"source": f"{path.name}:{position:.1f}s", "text": text}
                    )
                audio = Path(temp) / "audio.wav"
                try:
                    self.run(
                        [
                            self.config["ffmpeg"],
                            "-v",
                            "error",
                            "-protocol_whitelist",
                            "file,pipe",
                            "-i",
                            str(path),
                            "-vn",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            str(audio),
                        ],
                        30,
                    )
                    text, _ = self.audio(audio)
                    chunks.append({"source": path.name + ":音轨", "text": text})
                except MediaError as exc:
                    chunks.append(
                        {"source": path.name + ":音轨", "text": "[未解析] " + str(exc)}
                    )
            evidence = "仅0%、50%、80%时间位置的抽帧OCR及音轨；不是完整视频视觉理解"
        elif kind == "file":
            chunks, evidence = self.document(path)
        else:
            raise MediaError("未知附件类型")
        size = sum(len(x["text"]) for x in chunks)
        if size > 60000:
            raise MediaError("解析文本超过60000字符，拒绝静默截断；请拆分附件")
        if not any(x["text"].strip() for x in chunks):
            raise MediaError("附件没有可读取的文本")
        return chunks, evidence

    def document(self, path):
        ext = path.suffix.lower()
        if ext in {".txt", ".md", ".json", ".csv"}:
            text = path.read_text(encoding="utf-8-sig")
            if ext == ".json":
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            if ext == ".csv":
                rows = list(csv.reader(io.StringIO(text)))
                chunks = [
                    {"source": f"{path.name}:行{i + 1}", "text": " | ".join(row)}
                    for i, row in enumerate(rows)
                ]
            else:
                chunks = [
                    {"source": f"{path.name}:行{i + 1}", "text": line}
                    for i, line in enumerate(text.splitlines())
                    if line.strip()
                ]
            return chunks, "UTF-8原文；CSV保留行号，不自动推断统计口径"
        if ext in {".docx", ".xlsx"}:
            with zipfile.ZipFile(path) as archive:
                if sum(x.file_size for x in archive.infolist()) > 30_000_000:
                    raise MediaError("解压内容过大")
                if ext == ".docx":
                    root = ET.fromstring(archive.read("word/document.xml"))
                    ns = {
                        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    }
                    chunks = []
                    for i, paragraph in enumerate(root.findall(".//w:p", ns)):
                        text = "".join(
                            x.text or "" for x in paragraph.findall(".//w:t", ns)
                        )
                        if text:
                            chunks.append(
                                {"source": f"{path.name}:段{i + 1}", "text": text}
                            )
                    return (
                        chunks,
                        "DOCX正文及表格段落；未提取页眉、页脚、批注和嵌入对象",
                    )
            import openpyxl

            workbook = openpyxl.load_workbook(
                path, read_only=True, data_only=False, keep_links=False
            )
            chunks = []
            try:
                for sheet in workbook:
                    count = 0
                    for row in sheet:
                        count += len(row)
                        if count > 20000:
                            raise MediaError("每张表超过20000单元格，请拆分")
                        cells = [cell for cell in row if cell.value is not None]
                        if cells:
                            chunks.append(
                                {
                                    "source": f"{path.name}:{sheet.title}!{cells[0].coordinate}:{cells[-1].coordinate}",
                                    "text": " | ".join(
                                        f"{cell.coordinate}={cell.value}"
                                        for cell in cells
                                    ),
                                }
                            )
            finally:
                workbook.close()
            return chunks, "XLSX单元格与公式原文；不执行公式、宏或外部链接"
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            if reader.is_encrypted:
                raise MediaError("加密PDF需先手动解密")
            if len(reader.pages) > 100:
                raise MediaError("PDF超过100页，请拆分")
            chunks = []
            missing = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if not text.strip():
                    missing.append(i + 1)
                chunks.append(
                    {
                        "source": f"{path.name}:页{i + 1}",
                        "text": text or "[本页无可提取文字]",
                    }
                )
            evidence = "PDF文字层；未做版面和扫描页OCR"
            if missing:
                evidence += "；未读取页：" + ",".join(map(str, missing))
            return chunks, evidence
        raise MediaError("不支持此文件格式；不会执行附件")


def retrieve(documents, query, limit=6):
    def grams(text):
        text = text.lower()
        return {text[i : i + 2] for i in range(max(0, len(text) - 1))}

    q = grams(query)
    chunks = []
    for document in documents:
        for chunk in json.loads(document["content"]):
            # Bound each prompt chunk; expose partial coverage explicitly.
            for start in range(0, max(1, len(chunk["text"])), 1000):
                text = chunk["text"][start : start + 1000]
                source = chunk["source"] + (
                    f":字符{start + 1}-{start + len(text)}"
                    if len(chunk["text"]) > 1000
                    else ""
                )
                chunks.append(
                    {"source": source, "text": text, "evidence": document["evidence"]}
                )
    ranked = sorted(
        enumerate(chunks), key=lambda x: (-len(q & grams(x[1]["text"])), x[0])
    )
    return [chunk for _, chunk in ranked[:limit]]
