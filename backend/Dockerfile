FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \

    PYTHONUNBUFFERED=1 \

    PIP_NO_CACHE_DIR=1 \

    PORT=8020

RUN apt-get update && apt-get install -y --no-install-recommends \

    tesseract-ocr \

    imagemagick \

    libgl1 \

    libglib2.0-0 \

    libsm6 \

    libxext6 \

    libxrender1 \

    libgomp1 \

    fonts-dejavu \

    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip \

    && pip install -r requirements.txt

COPY app ./app

COPY data ./data

RUN mkdir -p /app/data \

    && python -m compileall -q /app/app

EXPOSE 8020

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8020} --workers 1"]