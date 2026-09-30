FROM python:3.13-alpine

RUN apk add --no-cache git
RUN pip install --break-system-packages quart hypercorn validators websockets uuid_utils sqlalchemy[asyncio] urlcanon aiohttp[speedups] argon2-cffi asyncpg regex
RUN pip install --break-system-packages git+https://github.com/TheTechRobo/bot2h
RUN mkdir /app

COPY . /app/tracker
WORKDIR /app

ENTRYPOINT ["python3", "-m"]
