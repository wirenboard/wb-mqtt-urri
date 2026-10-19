# pylint: disable=redefined-outer-name

import asyncio
import functools
import json
import logging
import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from wb_mqtt_urri.main import (
    EXIT_INVALIDARGUMENT,
    EXIT_NOTCONFIGURED,
    EXIT_NOTRUNNING,
    EXIT_SUCCESS,
    MQTTDevice,
    URRIClient,
    URRIDevice,
    main,
    read_and_validate_config,
)

TEST_CONFIG = {
    "debug": True,
    "devices": [
        {"device_id": "urr1", "device_title": "urr1", "urri_ip": "192.168.2.103", "urri_port": 9032}
    ],  # pylint: disable=line-too-long
}

# pybuild runs the suite from its build tree, the schema stays in the source root above it
SCHEMA_PATH = next(
    parent / "wb-mqtt-urri.schema.json"
    for parent in Path(__file__).resolve().parents
    if (parent / "wb-mqtt-urri.schema.json").is_file()
)


class URRIDeviceMock:
    def __init__(self, properties):
        assert properties == TEST_CONFIG["devices"][0]
        self.id = properties["device_id"]
        self.title = properties["device_title"]
        self.ip = properties["urri_ip"]

    def set_mqtt_device(self, _):
        pass

    async def run(self):
        pass

    async def stop(self):
        pass


class URRIDeviceForeverMock(URRIDeviceMock):
    """
    Runs until cancelled, like the real receiver task; lets the daemon be stopped by a signal.
    """

    async def run(self):
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def signal_handlers(monkeypatch):
    """
    Records the handlers run() registers instead of installing them on the pytest process, where a
    hung daemon test would swallow the SIGTERM meant to stop it. Autouse: every test that runs the
    daemon needs it, the rest never reach add_signal_handler. Maps signum to the bound handler.
    """
    handlers = {}

    def record(_loop, signum, callback, *args):
        handlers[signum] = functools.partial(callback, *args)

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", record)
    return handlers


def test_mosquitto_restart(mocker):
    publications = []

    def publish(topic, value, retain):  # pylint: disable=unused-argument
        publications.append((topic, value))

    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mocked.return_value.publish.side_effect = publish
    mocked.return_value.start.side_effect = lambda **_: mocked.return_value.on_connect(None, None, None, 0)
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    mocker.patch("wb_mqtt_urri.main.MQTTDevice.remove")
    urri_client = URRIClient(TEST_CONFIG["devices"])

    async def run_then_reconnect():
        await urri_client.run()
        first_run = list(publications)
        publications.clear()
        urri_client._mqtt_client.on_disconnect(None, None, None)  # pylint: disable=protected-access
        urri_client._mqtt_client.on_connect(None, None, None, 0)  # pylint: disable=protected-access
        return first_run

    assert asyncio.run(run_then_reconnect()) == publications


def test_initial_authentication_failure_returns_2(mocker):
    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mqtt_client = mocked.return_value
    mqtt_client.start.side_effect = lambda **_: mqtt_client.on_connect(None, None, None, 5)
    mqtt_client.is_connected.return_value = False
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceForeverMock)
    remove = mocker.patch("wb_mqtt_urri.main.MQTTDevice.remove")

    assert asyncio.run(URRIClient(TEST_CONFIG["devices"]).run()) == EXIT_INVALIDARGUMENT

    remove.assert_not_called()
    mqtt_client.stop.assert_called_once()


def test_authentication_failure_after_reconnect_stops_with_2():
    urri_client = URRIClient(TEST_CONFIG["devices"])
    urri_client._event_loop = MagicMock()  # pylint: disable=protected-access
    urri_client._mqtt_connected = asyncio.Event()  # pylint: disable=protected-access
    urri_client._on_mqtt_client_connect(None, None, None, 0)  # pylint: disable=protected-access

    urri_client._on_mqtt_client_connect(None, None, None, 5)  # pylint: disable=protected-access

    urri_client._event_loop.call_soon_threadsafe.assert_called_with(  # pylint: disable=protected-access
        urri_client._stop, EXIT_INVALIDARGUMENT  # pylint: disable=protected-access
    )


def test_publishes_only_after_the_broker_confirms_the_connection(mocker):
    """
    Nothing is published before CONNACK: paho drops QoS 0 messages sent without a connection,
    and a CONNACK arriving while the first device is being published would be missed.
    """
    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mqtt_client = mocked.return_value  # start() returns at once, the CONNACK is still on its way
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    mocker.patch("wb_mqtt_urri.main.MQTTDevice.remove")
    urri_client = URRIClient(TEST_CONFIG["devices"])

    async def scenario():
        running = asyncio.create_task(urri_client.run())
        await asyncio.sleep(0.05)
        mqtt_client.publish.assert_not_called()
        mqtt_client.on_connect(None, None, None, 0)
        assert await asyncio.wait_for(running, timeout=1) == EXIT_SUCCESS

    asyncio.run(scenario())
    assert "/devices/urr1/meta/driver" in [call.args[0] for call in mqtt_client.publish.call_args_list]


@pytest.mark.parametrize("broker_connected", [True, False])
def test_signal_stops_with_success(mocker, caplog, signal_handlers, broker_connected):
    """
    SIGTERM/SIGINT end the daemon with 0; topics are cleared, or an error is logged if the broker is gone.
    """
    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mqtt_client = mocked.return_value
    mqtt_client.is_connected.return_value = broker_connected
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceForeverMock)
    remove = mocker.patch("wb_mqtt_urri.main.MQTTDevice.remove")
    urri_client = URRIClient(TEST_CONFIG["devices"])

    def connect_then_signal(**_):
        mqtt_client.on_connect(None, None, None, 0)
        asyncio.get_running_loop().call_soon(urri_client._on_term_signal)  # pylint: disable=protected-access

    mqtt_client.start.side_effect = connect_then_signal

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(urri_client.run()) == EXIT_SUCCESS

    assert remove.call_count == (1 if broker_connected else 0)
    assert ("retained topics cannot be removed" in caplog.text) is not broker_connected
    mqtt_client.stop.assert_called_once()
    assert set(signal_handlers) == {signal.SIGTERM, signal.SIGINT}
    assert {handler.func for handler in signal_handlers.values()} == {
        urri_client._on_term_signal  # pylint: disable=protected-access
    }


def test_signal_while_waiting_for_the_broker_stops_with_0(mocker, caplog):
    """
    SIGTERM before the first CONNACK: nothing was published, the wait ends, the daemon exits with 0.
    """
    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mqtt_client = mocked.return_value  # start() returns at once, the broker never answers
    mqtt_client.is_connected.return_value = False
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceForeverMock)
    remove = mocker.patch("wb_mqtt_urri.main.MQTTDevice.remove")
    urri_client = URRIClient(TEST_CONFIG["devices"])
    mqtt_client.start.side_effect = lambda **_: asyncio.get_running_loop().call_later(
        0.05, urri_client._on_term_signal  # pylint: disable=protected-access
    )

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(urri_client.run()) == EXIT_SUCCESS

    mqtt_client.publish.assert_not_called()
    remove.assert_not_called()
    assert "retained topics cannot be removed" in caplog.text
    mqtt_client.stop.assert_called_once()


def make_mqtt_device(published: dict):
    """
    An MQTTDevice over a mocked client; published[topic] holds the last retained value.
    """
    client = MagicMock()
    client.publish.side_effect = lambda topic, value, retain=True: published.__setitem__(topic, value)
    urri_device = MagicMock(id="urr1", title="URRI 1", ip="192.168.2.103")
    mqtt_device = MQTTDevice(client)
    mqtt_device.set_urri_device(urri_device)
    mqtt_device.publicate()
    callbacks = {call.args[0]: call.args[1] for call in client.message_callback_add.call_args_list}
    return mqtt_device, urri_device, callbacks


def control_error(published: dict, control: str):
    return json.loads(published[f"/devices/urr1/controls/{control}/meta"]).get("error", "")


def test_failed_command_sets_write_error_and_does_not_raise():
    published = {}
    mqtt_device, urri_device, callbacks = make_mqtt_device(published)
    urri_device.set_volume.side_effect = requests.ConnectionError("receiver is down")

    callbacks["/devices/urr1/controls/Volume/on"](None, None, MagicMock(payload=b"20"))
    assert control_error(published, "Volume") == "w"

    urri_device.set_volume.side_effect = None
    callbacks["/devices/urr1/controls/Volume/on"](None, None, MagicMock(payload=b"20"))
    assert control_error(published, "Volume") == ""

    callbacks["/devices/urr1/controls/Volume/on"](None, None, MagicMock(payload=b"loud"))  # not an int
    assert control_error(published, "Volume") == "w"

    urri_device.play_alert_by_name.side_effect = AttributeError("unexpected receiver answer")
    callbacks["/devices/urr1/controls/Play Alert/on"](None, None, MagicMock(payload=b"bell"))
    assert control_error(published, "Play Alert") == "w"

    urri_device.play_radio_by_id.return_value = False  # refused by the receiver
    callbacks["/devices/urr1/controls/Radio ID/on"](None, None, MagicMock(payload=b"7"))
    assert control_error(published, "Radio ID") == "w"
    mqtt_device.set_error_state(False)  # unrelated read state must not clear it
    assert control_error(published, "Radio ID") == "w"


def test_lost_connection_marks_the_controls_unreadable():
    """
    The socket.io client reconnects by itself after a drop; until it succeeds the controls carry "r".
    """
    published = {}
    client = MagicMock()
    client.publish.side_effect = lambda topic, value, retain=True: published.__setitem__(topic, value)
    urri_device = URRIDevice(TEST_CONFIG["devices"][0])
    mqtt_device = MQTTDevice(client)
    mqtt_device.set_urri_device(urri_device)
    urri_device.set_mqtt_device(mqtt_device)
    mqtt_device.publicate()
    handlers = urri_device._urri_client.handlers["/"]  # pylint: disable=protected-access

    asyncio.run(handlers["disconnect"]("transport close"))
    assert control_error(published, "Volume") == "r"
    assert control_error(published, "IP address") == ""

    asyncio.run(handlers["connect"]())
    assert control_error(published, "Volume") == ""


def test_read_and_write_errors_do_not_overwrite_each_other():
    published = {}
    mqtt_device, urri_device, callbacks = make_mqtt_device(published)
    urri_device.set_mute.side_effect = requests.Timeout()

    callbacks["/devices/urr1/controls/Mute/on"](None, None, MagicMock(payload=b"1"))
    mqtt_device.set_error_state(True)
    assert control_error(published, "Mute") == "rw"
    assert control_error(published, "Volume") == "r"
    assert control_error(published, "IP address") == ""

    urri_device.set_mute.side_effect = None
    callbacks["/devices/urr1/controls/Mute/on"](None, None, MagicMock(payload=b"1"))
    assert control_error(published, "Mute") == "r"

    mqtt_device.set_error_state(False)
    assert control_error(published, "Mute") == ""


@pytest.mark.parametrize(
    "content",
    [
        None,  # file is missing
        "{not json",
        json.dumps(
            {"devices": [{"device_id": "urri", "device_title": "URRI", "urri_ip": "", "urri_port": 9032}]}
        ),
        json.dumps({"devices": [TEST_CONFIG["devices"][0], TEST_CONFIG["devices"][0]], "debug": False}),
    ],
    ids=["missing", "broken-json", "schema-violation", "duplicate-ids"],
)
def test_invalid_config_is_rejected(tmp_path, content):
    config_path = tmp_path / "wb-mqtt-urri.conf"
    if content is not None:
        config_path.write_text(content)

    assert read_and_validate_config(str(config_path), str(SCHEMA_PATH)) is None


@pytest.mark.parametrize(
    "devices, exit_code",
    [([], EXIT_NOTRUNNING), (None, EXIT_NOTCONFIGURED)],
    ids=["no-receivers", "missing-config"],
)
def test_main_exit_codes_without_receivers(mocker, tmp_path, devices, exit_code):
    config_path = tmp_path / "wb-mqtt-urri.conf"
    if devices is not None:
        config_path.write_text(json.dumps({"devices": devices, "debug": False}))
    mocker.patch("wb_mqtt_urri.main.SCHEMA_FILEPATH", str(SCHEMA_PATH))

    assert main(["wb-mqtt-urri", "-c", str(config_path)]) == exit_code
