FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY server.py .

EXPOSE 9100
CMD ["python", "server.py"]
