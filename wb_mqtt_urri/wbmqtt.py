import json
import logging
import random
import threading


class ControlMeta:  # pylint: disable=R0903,disable=R0913
    def __init__(
        self,
        title: str = None,
        control_type: str = "value",
        order: int = None,
        read_only: bool = False,
        min_value: int = None,
        max_value: int = None,
    ) -> None:
        self.title = title
        self.control_type = control_type
        self.order = order
        self.read_only = read_only
        self.min_value = min_value
        self.max_value = max_value


class ControlState:  # pylint: disable=R0903
    def __init__(self, meta: ControlMeta, value: str) -> None:
        self.meta = ControlMeta(meta.title, meta.control_type, meta.order, meta.read_only)
        self.value = value


class Device:
    def __init__(self, mqtt_client, device_mqtt_name: str, device_title: str, driver_name: str) -> None:
        self._mqtt_client = mqtt_client
        self._base_topic = f"/devices/{device_mqtt_name}"
        self._controls = {}
        self._publish(self._base_topic + "/meta/name", device_title)
        self._publish(self._base_topic + "/meta/driver", driver_name)

    def remove_device(self) -> None:
        self._publish(self._base_topic + "/meta/driver", None)
        self._publish(self._base_topic + "/meta/name", None)
        for mqtt_control_name in self._controls.copy():
            self.remove_control(mqtt_control_name)

    def create_control(self, mqtt_control_name: str, meta: ControlMeta, value: str) -> None:
        self._controls[mqtt_control_name] = ControlState(meta, None)
        self._publish_control_meta(mqtt_control_name, meta)
        self.set_control_value(mqtt_control_name, value)

    def remove_control(self, mqtt_control_name: str) -> None:
        if mqtt_control_name in self._controls:
            self._controls.pop(mqtt_control_name)
            self._publish(self._get_control_base_topic(mqtt_control_name), None)
            self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", None)

    def set_control_value(self, mqtt_control_name: str, value: str, force=False) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.value != value or force:
                control.value = value
                self._publish(self._get_control_base_topic(mqtt_control_name), value)
        else:
            logging.debug("Can't set value of undeclared control %s", mqtt_control_name)

    def set_control_read_only(self, mqtt_control_name: str, read_only: bool) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.meta.read_only != read_only:
                control.meta.read_only = read_only
                self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug("Can't set readonly property of undeclared control %s", mqtt_control_name)

    def set_control_title(self, mqtt_control_name: str, title: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.meta.title != title:
                control.meta.title = title
                self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug("Can't set title of undeclared control %s", mqtt_control_name)

    def add_control_message_callback(self, mqtt_control_name: str, callback: callable) -> None:
        if mqtt_control_name in self._controls:
            control_base_topic = self._get_control_base_topic(mqtt_control_name)
            self._mqtt_client.subscribe(control_base_topic + "/on")
            self._mqtt_client.message_callback_add(control_base_topic + "/on", callback)
        else:
            logging.debug("Can't add message callback to undeclared control %s", mqtt_control_name)

    def _get_control_base_topic(self, mqtt_control_name: str) -> None:
        return f"{self._base_topic}/controls/{mqtt_control_name}"

    def _publish_control_meta(self, mqtt_control_name: str, meta: ControlMeta) -> None:
        meta_dict = {
            "type": meta.control_type,
            "readonly": meta.read_only,
        }
        if meta.title is not None:
            meta_dict["title"] = {"en": meta.title}
        if meta.order is not None:
            meta_dict["order"] = meta.order
        if meta.min_value is not None:
            meta_dict["min"] = meta.min_value
        if meta.max_value is not None:
            meta_dict["max"] = meta.max_value

        if meta_dict:
            meta_json = json.dumps(meta_dict)
            self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", meta_json)

    def _publish(self, topic: str, value: str) -> None:
        if value is None:
            logging.debug('Clear "%s"', topic)
        else:
            logging.debug('Publish "%s" "%s"', topic, value)
        self._mqtt_client.publish(topic, value, retain=True)


def retain_hack(mqtt_client) -> None:
    random.seed()
    retain_hack_topic = f"/wbretainhack/{random.random()}"

    sem = threading.Semaphore(0)

    def on_retain_hack(_, __, _message):
        sem.release()

    mqtt_client.subscribe(retain_hack_topic)
    mqtt_client.message_callback_add(retain_hack_topic, on_retain_hack)
    mqtt_client.publish(retain_hack_topic, "2", qos=2)
    sem.acquire(timeout=10)  # pylint: disable=R1732
    mqtt_client.unsubscribe(retain_hack_topic)
    mqtt_client.message_callback_remove(retain_hack_topic)


def remove_topics_by_device_prefix(mqtt_client, device_prefix: str) -> None:
    topics = []

    pattern = "/devices/" + device_prefix

    def on_message(_, __, message):
        if message.topic.startswith(pattern):
            topics.append(message.topic)

    devices_pattern = "/devices/#"
    mqtt_client.message_callback_add(devices_pattern, on_message)
    mqtt_client.subscribe(devices_pattern)

    retain_hack(mqtt_client)

    mqtt_client.unsubscribe(devices_pattern)
    mqtt_client.message_callback_remove(devices_pattern)

    for topic in topics:
        logging.debug("Clear old topic %s", topic)
        mqtt_client.publish(topic, None, retain=True)
