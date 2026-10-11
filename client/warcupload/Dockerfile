FROM python:3.11-bookworm

RUN mkdir /input
RUN apt-get update && apt-get install tini rsync wget && apt-get clean
RUN pip install --no-cache-dir requests
RUN wget https://github.com/ArchiveTeam/ArchiveBot/raw/cff95a86d9ce36b4d67dbd98fd37bdfc1cc9b398/uploader/uploader.py -O /uploader.py

ENTRYPOINT ["/bin/tini", "--", "python", "/uploader.py", "/input/warcs"]

