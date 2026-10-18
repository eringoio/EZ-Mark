import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageLoadDiagnostics:
    context: str
    path: str
    exists: bool
    file_size: int | None
    flags: int
    imread_worked: bool
    fromfile_worked: bool
    byte_length: int | None
    imdecode_worked: bool
    image_shape: tuple[int, ...] | None
    image_dtype: str | None

    @property
    def loaded(self) -> bool:
        return self.imread_worked or self.imdecode_worked

    def as_dict(self) -> dict:
        return {
            "context": self.context,
            "path": self.path,
            "exists": self.exists,
            "file_size": self.file_size,
            "flags": self.flags,
            "imread_worked": self.imread_worked,
            "fromfile_worked": self.fromfile_worked,
            "byte_length": self.byte_length,
            "imdecode_worked": self.imdecode_worked,
            "loaded": self.loaded,
            "image_shape": list(self.image_shape) if self.image_shape else None,
            "image_dtype": self.image_dtype,
        }


@dataclass(frozen=True)
class ImageLoadResult:
    image: np.ndarray | None
    diagnostics: ImageLoadDiagnostics


@dataclass(frozen=True)
class ImageSaveDiagnostics:
    context: str
    path: str
    parent_exists: bool
    imwrite_result: bool | None
    imencode_result: bool | None
    final_exists: bool
    final_file_size: int | None

    @property
    def saved(self) -> bool:
        return self.final_exists and bool(self.final_file_size and self.final_file_size > 0)

    def as_dict(self) -> dict:
        return {
            "context": self.context,
            "path": self.path,
            "parent_exists": self.parent_exists,
            "imwrite_result": self.imwrite_result,
            "imencode_result": self.imencode_result,
            "final_exists": self.final_exists,
            "final_file_size": self.final_file_size,
            "saved": self.saved,
        }


def load_image_for_omr(
    path: Path,
    flags: int = cv2.IMREAD_COLOR,
    context: str = "",
):
    return inspect_image_loading_for_omr(path, flags=flags, context=context).image


def inspect_image_loading_for_omr(
    path: Path,
    flags: int = cv2.IMREAD_COLOR,
    context: str = "",
) -> ImageLoadResult:
    exists = path.exists()
    file_size = path.stat().st_size if exists else None
    image = None
    imread_worked = False
    fromfile_worked = False
    byte_length = None
    imdecode_worked = False

    try:
        image = cv2.imread(str(path), flags)
        imread_worked = image is not None
    except Exception:
        logger.exception("cv2.imread failed during OMR image loading")

    if image is None:
        try:
            data = np.fromfile(str(path), dtype=np.uint8)
            fromfile_worked = True
            byte_length = int(data.size)
            if data.size:
                image = cv2.imdecode(data, flags)
                imdecode_worked = image is not None
        except Exception:
            logger.exception("np.fromfile/cv2.imdecode failed during OMR image loading")

    diagnostics = ImageLoadDiagnostics(
        context=context,
        path=str(path),
        exists=exists,
        file_size=file_size,
        flags=flags,
        imread_worked=imread_worked,
        fromfile_worked=fromfile_worked,
        byte_length=byte_length,
        imdecode_worked=imdecode_worked,
        image_shape=tuple(image.shape) if image is not None else None,
        image_dtype=str(image.dtype) if image is not None else None,
    )
    log_payload = diagnostics.as_dict()
    if image is None:
        logger.error("OMR image loading failed", extra={"omr_image_load": log_payload})
    else:
        logger.info("OMR image loaded", extra={"omr_image_load": log_payload})
    return ImageLoadResult(image=image, diagnostics=diagnostics)


def save_image_for_omr(path: Path, image, context: str = "") -> bool:
    """Write an OMR image with a Windows-safe fallback for non-ASCII paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    imwrite_result = None
    imencode_result = None

    try:
        imwrite_result = bool(cv2.imwrite(str(path), image))
    except Exception:
        imwrite_result = False
        logger.exception("cv2.imwrite failed during OMR image saving")

    if not _written_file_exists(path):
        extension = path.suffix or ".png"
        try:
            success, encoded = cv2.imencode(extension, image)
            imencode_result = bool(success)
            if success:
                encoded.tofile(str(path))
        except Exception:
            imencode_result = False
            logger.exception("cv2.imencode/tofile failed during OMR image saving")

    final_exists = path.exists()
    final_file_size = path.stat().st_size if final_exists else None
    diagnostics = ImageSaveDiagnostics(
        context=context,
        path=str(path),
        parent_exists=path.parent.exists(),
        imwrite_result=imwrite_result,
        imencode_result=imencode_result,
        final_exists=final_exists,
        final_file_size=final_file_size,
    )
    log_payload = diagnostics.as_dict()
    if diagnostics.saved:
        logger.info("OMR image saved", extra={"omr_image_save": log_payload})
    else:
        logger.error("OMR image saving failed", extra={"omr_image_save": log_payload})
    return diagnostics.saved


def _written_file_exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0
