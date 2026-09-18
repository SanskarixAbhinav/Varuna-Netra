"""Oil-spill segmentation detector interface.

Production interface for a trained Sentinel-1 SAR oil-spill semantic-segmentation model
(U-Net / DeepLabV3+, ONNX Runtime), loaded from OIL_MODEL_PATH. When no trained model is
installed the system falls back to the existing experimental Otsu dark-spot heuristic.

TRUTHFULNESS: Otsu output is NEVER labelled ML inference. No weights, confidence or metrics
are ever fabricated. Drop a real ONNX model at OIL_MODEL_PATH to activate ML with no code change.
"""
import os

ML_VERSION = "oilseg-unet-1.0"
FALLBACK_VERSION = "darkspot-otsu-0.1.0"
OIL_MODEL_PATH = os.environ.get("OIL_MODEL_PATH")

_session = None


def _onnx_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def model_installed() -> bool:
    return bool(OIL_MODEL_PATH and os.path.isfile(OIL_MODEL_PATH) and _onnx_available())


def load_session():
    """Lazily load the ONNX model. Returns the session or None when no model is installed."""
    global _session
    if _session is not None:
        return _session
    if not model_installed():
        return None
    import onnxruntime as ort
    _session = ort.InferenceSession(OIL_MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def detector_status() -> dict:
    installed = model_installed()
    return {
        "mode": "ML" if installed else "OTSU_FALLBACK",
        "active_detector": ML_VERSION if installed else FALLBACK_VERSION,
        "ml_version": ML_VERSION,
        "fallback_version": FALLBACK_VERSION,
        "ml_model_installed": installed,
        "onnx_runtime_available": _onnx_available(),
        "model_path_configured": bool(OIL_MODEL_PATH),
        "label": "AI SAR oil-spill candidate segmentation" if installed else "Experimental SAR dark-spot detection",
        "status_text": "ML MODEL ACTIVE" if installed else "ML MODEL NOT INSTALLED — EXPERIMENTAL OTSU FALLBACK ACTIVE",
        "input_channels": ["VV", "VH", "VV-VH"] if installed else ["VV (quicklook)"],
        "note": ("Trained ONNX SAR segmentation model active." if installed
                 else "No trained ONNX model at OIL_MODEL_PATH; using the experimental Otsu dark-spot heuristic. Otsu output is NOT ML inference and carries no trained confidence."),
    }
