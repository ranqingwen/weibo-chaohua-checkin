FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 用通配符复制全部源码模块, 避免新增 .py (如 auth.py) 后忘记加进 COPY 导致
# ModuleNotFoundError: No module named 'xxx'
COPY *.py ./
COPY static ./static

EXPOSE 8787

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
