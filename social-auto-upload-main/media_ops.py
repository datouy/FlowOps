# -*- coding: utf-8 -*-
"""媒体处理：从视频首帧生成多比例封面。"""
from __future__ import annotations

from pathlib import Path

import cv2
from flask import jsonify, request

from conf import BASE_DIR

VIDEO_DIR = Path(BASE_DIR) / "videoFile"


def _first_frame(path: Path):
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return cv2.imread(str(path))
    cap = cv2.VideoCapture(str(path))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _cover(frame, width: int, height: int):
    h, w = frame.shape[:2]
    target_ratio = width / height
    ratio = w / h
    if ratio > target_ratio:
        new_w = int(h * target_ratio)
        x = (w - new_w) // 2
        crop = frame[:, x:x + new_w]
    else:
        new_h = int(w / target_ratio)
        y = (h - new_h) // 2
        crop = frame[y:y + new_h, :]
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_AREA)


def register(app):
    @app.route("/media/covers", methods=["POST"])
    def make_covers():
        data = request.get_json(force=True) or {}
        filename = data.get("file")
        if not filename:
            return jsonify({"code": 400, "msg": "file required"}), 400
        src = VIDEO_DIR / filename
        if not src.exists():
            return jsonify({"code": 404, "msg": "file not found"}), 404
        frame = _first_frame(src)
        if frame is None:
            return jsonify({"code": 500, "msg": "无法读取视频首帧"}), 500
        sizes = {"3x4": (1080, 1440), "9x16": (1080, 1920)}
        result = []
        for name, (w, h) in sizes.items():
            out = src.with_name(src.stem + "_cover_" + name + ".jpg")
            cv2.imwrite(str(out), _cover(frame, w, h))
            result.append({"name": name, "file": out.name, "url": "/getFile?filename=" + out.name})
        return jsonify({"code": 200, "data": {"covers": result}, "msg": "ok"})

