# fast-alpr sidecar

Reference ALPR μservice for the camera-stream driver family. Wraps
[`fast-alpr`](https://github.com/ankandrew/fast-alpr) (MIT — ONNX YOLO
plate detector + `fast-plate-ocr`) behind the stable contract the
proxy expects.

It runs as a **separate container** on purpose: the heavy ONNX /
OpenCV stack never enters the LGPL-3 fiscal Python process. The proxy
(`FastAlprSiblingEngine`) is a thin httpx client — HTTP-coupled, no
Python import, no copyleft combination.

## Contract

```
POST /v1/recognize           multipart/form-data
    image          <jpeg>    (required)
    min_confidence <float>   (optional)
    region         <str>     (optional, advisory)
→ 200 {"results":[{"plate":"CA1234AB","confidence":0.93,
                    "box":{"x":120,"y":88,"w":210,"h":70}}]}

GET  /healthz → {"ok":true,"detector":...,"ocr":...,"loaded":bool}
```

`region` is advisory — the global/EU OCR model already covers
Bulgarian plates. Swap models via env `ALPR_DETECTOR` / `ALPR_OCR`.

## Run

```bash
# from the repo root
docker compose --profile cameras up -d go2rtc alpr
# or standalone
docker build -t erpnet-alpr-sidecar tools/alpr_sidecar
docker run --rm -p 8002:8002 erpnet-alpr-sidecar
```

First request lazily downloads the ONNX models (~tens of MB) and
caches them in `/root/.cache`; mount a volume there to keep them
across `recreate`.

## Swapping for a cloud engine

Point a camera's `lpr.url` at any adapter that speaks the same JSON
contract (e.g. a translating shim in front of Plate Recognizer). No
proxy code change — `engine: cloud` reuses the same wire format.
