FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 DB_PATH=/tmp/docs.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
EXPOSE 8000
# Render/Fly/Railway inject $PORT; Hugging Face Spaces: set PORT=7860.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
