# fast-alpr sidecar — CPU ONNX inference, separate from the fiscal proxy.
FROM python:3.12-slim

# libGL/glib — runtime deps на opencv-python-headless
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Моделите се теглят при първия request и кешират в /root/.cache —
# монтирай volume там ако искаш да преживеят recreate.
EXPOSE 8002
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
