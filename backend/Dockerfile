FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# 下架入口与对账入口（与 app/ 内的故障注入、对账模块分开落地）
COPY run_sweep.py reconcile.py ./
ENV DATA_DIR=/data
EXPOSE 10300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10300"]
