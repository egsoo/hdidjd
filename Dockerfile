FROM python:3.11-slim-bookworm

RUN apt-get update -qq && apt-get install -y --no-install-recommends ffmpeg mediainfo git gcc python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

COPY . .

CMD [ "python", "-m", "bot" ]
