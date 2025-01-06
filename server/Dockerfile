FROM python:3.11-alpine

RUN apk add --no-cache git
RUN pip install --break-system-packages rethinkdb websockets
RUN pip install --break-system-packages git+https://github.com/TheTechRobo/rue
RUN pip install --break-system-packages git+https://github.com/TheTechRobo/bot2h

COPY . /app
WORKDIR /app

ENTRYPOINT ["python3", "app.py"]
