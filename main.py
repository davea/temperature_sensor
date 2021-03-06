#!/usr/bin/env python3
from time import sleep
from socket import gethostname
from configparser import ConfigParser
from os.path import expanduser

try:
    from w1thermsensor import W1ThermSensor
except Exception:
    # Can't use the actual exception class that's thrown
    # (w1thermsensor.errors.KernelModuleLoadError) because we can't import it
    # without it being thrown. Oops.
    # One-Wire might not be enabled on this Raspbian host, nevermind.
    W1ThermSensor = None

import bme680
import smbus

import paho.mqtt.publish as publish

config = ConfigParser()
config.read(expanduser("~/.config/homesense/temperature.ini"))


class BaseSensor:
    mqtt_id = None

    def topic_for_attribute(self, attribute):
        return config['mqtt']['topic_format'].format(attribute=attribute, id=self.mqtt_id)

    @property
    def topics_and_values(self):
        return []


class W1Sensor(BaseSensor):
    _sensor = None

    def __init__(self, mqtt_id, sensor):
        self.mqtt_id = mqtt_id
        self._sensor = sensor


    @property
    def topics_and_values(self):
        yield (self.topic_for_attribute('temperature'), self._sensor.get_temperature())


    @classmethod
    def create_sensors(cls):
        if W1ThermSensor is None:
            return
        sensors = W1ThermSensor.get_available_sensors()
        hostname = config['general'].get('hostname', gethostname().split('.')[0])

        if len(sensors) == 1:
            sensor = sensors[0]
            mqtt_id = config['w1sensors'].get(sensor.id, hostname)
            yield cls(mqtt_id=mqtt_id, sensor=sensor)
        else:
            for sensor in sensors:
                mqtt_id = config['w1sensors'].get(sensor.id, "{}_{}".format(hostname, sensor.id))
                yield cls(mqtt_id=mqtt_id, sensor=sensor)


class BME680Sensor(BaseSensor):
    _sensor = None

    def __init__(self, mqtt_id, i2c_addr=None, i2c_device=None):
        self.mqtt_id = mqtt_id

        if i2c_addr:
            self._sensor = bme680.BME680(i2c_addr=i2c_addr, i2c_device=i2c_device)
        else:
            self._sensor = bme680.BME680(i2c_device=i2c_device)

        self._sensor.set_humidity_oversample(bme680.OS_8X)
        self._sensor.set_pressure_oversample(bme680.OS_8X)
        self._sensor.set_temperature_oversample(bme680.OS_8X)
        self._sensor.set_filter(bme680.FILTER_SIZE_3)
        self._sensor.set_gas_status(bme680.ENABLE_GAS_MEAS)
        self._sensor.set_gas_heater_temperature(320)
        self._sensor.set_gas_heater_duration(150)
        self._sensor.select_gas_heater_profile(0)

    @property
    def topics_and_values(self):
        attempts = 60
        for data, stable in ((self._sensor.get_sensor_data(), self._sensor.data.heat_stable) for _ in range(attempts)):
            if data and stable:
                break
            sleep(0.5)
        else:
            return []
        return [
            (self.topic_for_attribute('temperature'), self._sensor.data.temperature),
            (self.topic_for_attribute('humidity'), self._sensor.data.humidity),
            (self.topic_for_attribute('pressure'), self._sensor.data.pressure),
            (self.topic_for_attribute('gas_resistance'), self._sensor.data.gas_resistance),
        ]

    @classmethod
    def create_sensors(cls):
        sensors = []

        i2c_device = None
        # bme680 doesn't always seem to appear on the same i2c bus...
        for bus_id in (0, 1):
            try:
                i2c_device = smbus.SMBus(bus_id)
            except FileNotFoundError:
                pass

        # Create sensors based on what's in the ini file
        for i2c_addr, mqtt_id in config['bme680sensors'].items():
            sensors.append(cls(mqtt_id, i2c_addr=int(i2c_addr, 16), i2c_device=i2c_device))

        # If there's nothing in the ini file, then create one sensor
        # with the default i2c address and mqtt id from the hostname
        if not sensors:
            hostname = config['general'].get('hostname', gethostname().split('.')[0])
            try:
                sensors.append(cls(hostname, i2c_device=i2c_device))
            except OSError:
                # Perhaps there's no BME680 sensor attached?
                pass

        return sensors


def main():
    sensors = []
    sensors.extend(BME680Sensor.create_sensors())
    sensors.extend(W1Sensor.create_sensors())

    if not sensors:
        raise Exception("No sensors found.")

    while True:
        messages = []
        for sensor in sensors:
            for topic, payload in sensor.topics_and_values:
                messages.append({'topic': topic, 'payload': str(payload)})
        if messages:
            publish.multiple(messages, hostname=config['mqtt']['broker'])
        else:
            print("No messages to publish, bit weird.")
        sleep(int(config['general']['delay']))

if __name__ == '__main__':
    main()
