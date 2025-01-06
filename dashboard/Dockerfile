FROM python:3.11-alpine

RUN apk add --no-cache git
RUN pip install git+https://github.com/TheTechRobo/rue
RUN pip install quart hypercorn

COPY . /app

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["hypercorn", "app"]
