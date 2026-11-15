# Container for the Streamlit app. The model key is read from the environment at runtime
# (GEMINI_API_KEY); it is never copied into the image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hosts set $PORT; default to 8501 locally.
ENV PORT=8501
EXPOSE 8501
CMD ["sh", "-c", "streamlit run app/streamlit_app.py --server.address=0.0.0.0 --server.port=${PORT} --server.headless=true"]
