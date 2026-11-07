FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

RUN useradd --uid 10001 --create-home agent \
 && mkdir -p /app/data && chown -R agent:agent /app

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY config.yaml entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown agent:agent config.yaml entrypoint.sh

USER agent
EXPOSE 8765
ENTRYPOINT ["./entrypoint.sh"]
