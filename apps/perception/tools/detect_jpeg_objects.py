#!/usr/bin/env python3
"""Run bounded local YOLO inference on one JPEG received through stdin."""

import argparse
import json
import sys


MAX_JPEG_BYTES = 8 * 1024 * 1024


def detect(jpeg, model_path, confidence, device):
    import cv2
    import numpy as np
    from ultralytics import YOLO

    if not jpeg or len(jpeg) > MAX_JPEG_BYTES:
        raise ValueError("JPEG input is empty or exceeds the bounded size limit")
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("JPEG input could not be decoded")
    model = YOLO(str(model_path))
    detections = []
    for result in model.predict(
        source=image,
        conf=float(confidence),
        device=device,
        verbose=False,
    ):
        names = result.names
        for box in result.boxes:
            class_id = int(box.cls[0].item())
            detections.append({
                "label": str(names[class_id]),
                "confidence": float(box.conf[0].item()),
                "bbox": [float(value) for value in box.xyxy[0].tolist()],
            })
    return detections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if not 0.0 < args.confidence <= 1.0:
        parser.error("--confidence must be in (0, 1]")
    jpeg = sys.stdin.buffer.read(MAX_JPEG_BYTES + 1)
    try:
        detections = detect(jpeg, args.model, args.confidence, args.device)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, "detections": detections}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
