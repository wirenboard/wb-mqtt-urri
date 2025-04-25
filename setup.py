#!/usr/bin/env python

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-mqtt-urri",
    version=get_version(),
    author="Ekaterina Volkova",
    author_email="ekaterina.volkova@wirenboard.ru",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board MQTT Driver for URRI receiver",
    url="https://github.com/wirenboard/wb-mqtt-urri",
    packages=["wb_mqtt_urri"],
    license="MIT",
)
