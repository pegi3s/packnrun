FROM python:3.11-slim-trixie

RUN pip install --no-cache-dir paramiko==3.5.1

COPY packnrun.py /usr/local/bin/packnrun
RUN chmod 755 /usr/local/bin/packnrun

WORKDIR /data

ENTRYPOINT ["packnrun"]
