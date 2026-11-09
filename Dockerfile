FROM python:3.10

WORKDIR /app

RUN pip3 install --upgrade pip
RUN pip3 install pyserial requests influxdb paho-mqtt
#RUN python /var/teleinfo.py

