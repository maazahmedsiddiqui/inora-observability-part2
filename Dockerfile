FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY inora_core/ ./inora_core/
COPY inora_obs/ ./inora_obs/
COPY openapi.yaml app.py ./
EXPOSE 5000
CMD ["python", "app.py"]