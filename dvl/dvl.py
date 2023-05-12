"""
Code for integration of Cerulean DVL with Companion and ArduSub
"""
import json
import math
import os
import socket
import threading
import time
from enum import Enum
from select import select
from typing import Any, Dict, List
import pynmea2

from loguru import logger

from blueoshelper import request
# from dvlfinder import find_the_dvl
from mavlink2resthelper import GPS_GLOBAL_ORIGIN_ID, Mavlink2RestHelper

HOSTNAME = "192.168.2.3"
DVL_DOWN = 1
DVL_FORWARD = 2
LATLON_TO_CM = 1.1131884502145034e5
POOL_MODE_COMMAND = "MANUAL-MODE 0.001,10.0,0.5,56,0.1,50,20.6,-0.671,100,100"
AUTOMATIC_MODE_COMMAND = "MANUAL-MODE OFF"


class MessageType(str, Enum):
    POSITION_DELTA = "POSITION_DELTA"
    POSITION_ESTIMATE = "POSITION_ESTIMATE"
    SPEED_ESTIMATE = "SPEED_ESTIMATE"

    @staticmethod
    def contains(value):
        return value in set(item.value for item in MessageType)


# pylint: disable=too-many-instance-attributes
# pylint: disable=unspecified-encoding
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
class DvlDriver(threading.Thread):
    """
    Responsible for the DVL interactions themselves.
    This handles fetching the DVL data and forwarding it to Ardusub
    """

    status = "Starting"
    version = ""
    mav = Mavlink2RestHelper()
    socket = None
    command_port = 50000
    port = 27000
    last_attitude = (0, 0, 0)  # used for calculating the attitude delta
    last_gps_timestamp = 0
    gps_update_interval = 10
    current_orientation = DVL_DOWN
    enabled = True
    rangefinder_enable = True
    hostname = HOSTNAME
    timeout = 10  # tcp timeout in seconds
    origin = [0, 0]
    settings_path = os.path.join(os.path.expanduser(
        "~"), ".config", "dvl", "settings.json")
    configuration = []

    should_send = MessageType.POSITION_DELTA
    reset_counter = 0
    #timestamp = 0

    # Cerulean DVL Info
    dvl_lock = ""
    dvl_gps_status = ""
    dvl_calibration = ""
    dvl_lock_a = ""
    dvl_lock_b = ""
    dvl_lock_c = ""
    dvl_lock_d = ""
    dvl_gain_a = -1
    dvl_gain_b = -1
    dvl_gain_c = -1
    dvl_gain_d = -1
    dvl_altitude = -1
    pool_mode = False

    def __init__(self, orientation=DVL_DOWN) -> None:
        threading.Thread.__init__(self)
        self.current_orientation = orientation

    def report_status(self, msg: str) -> None:
        self.status = msg
        logger.debug(msg)

    def load_settings(self) -> None:
        """
        Load settings from .config/dvl/settings.json
        """
        try:
            with open(self.settings_path) as settings:
                data = json.load(settings)
                self.enabled = data["enabled"]
                self.current_orientation = data["orientation"]
                self.hostname = data["hostname"]
                self.origin = data["origin"]
                self.rangefinder_enable = data["rangefinder_enable"]
                self.should_send = data["should_send"]

        except FileNotFoundError:
            logger.warning("Settings file not found, using default.")
        except ValueError:
            logger.warning("File corrupted, using default settings.")
        except KeyError as error:
            logger.warning("key not found: ", error)
            logger.warning("using default instead")

    def save_settings(self) -> None:
        """
        Load settings from .config/dvl/settings.json
        """

        def ensure_dir(file_path) -> None:
            """
            Helper to guarantee that the file path exists
            """
            directory = os.path.dirname(file_path)
            if not os.path.exists(directory):
                os.makedirs(directory)

        ensure_dir(self.settings_path)
        with open(self.settings_path, "w") as settings:
            settings.write(
                json.dumps(
                    {
                        "enabled": self.enabled,
                        "orientation": self.current_orientation,
                        "hostname": self.hostname,
                        "origin": self.origin,
                        "rangefinder_enable": self.rangefinder_enable,
                        "should_send": self.should_send,
                    }
                )
            )

    def get_status(self) -> dict:
        """
        Returns a dict with the current status
        """
        return {
            "status": self.status,
            "enabled": self.enabled,
            "orientation": self.current_orientation,
            "hostname": self.hostname,
            "origin": self.origin,
            "rangefinder_enable": self.rangefinder_enable,
            "should_send": self.should_send,
            "dvl_lock": self.dvl_lock,
            "dvl_gps_status": self.dvl_gps_status,
            "dvl_calibration": self.dvl_calibration,
            "dvl_altitude": self.dvl_altitude
        }

    @property
    def host(self) -> str:
        """Make sure there is no port in the hostname allows local testing by where http can be running on other ports than 80"""
        try:
            host = self.hostname.split(":")[0]
        except IndexError:
            host = self.hostname
        return host

    # def look_for_dvl(self):
    #     """
    #     Waits for the dvl to show up at the designated hostname
    #     """
    #     self.wait_for_cable_guy()
    #     ip = self.hostname
    #     self.status = f"Trying to talk to dvl at http://{ip}/api/v1/about"
    #     while not self.version:
    #         if not request(f"http://{ip}/api/v1/about"):
    #             self.report_status(
    #                 f"could not talk to dvl at {ip}, looking for it in the local network...")
    #         found_dvl = find_the_dvl()
    #         if found_dvl:
    #             self.report_status(
    #                 f"Dvl found at address {found_dvl}, using it instead.")
    #             self.hostname = found_dvl
    #             return
    #         time.sleep(1)

    def wait_for_cable_guy(self):
        while not request("http://127.0.0.1/cable-guy/v1.0/ethernet"):
            self.report_status("waiting for cable-guy to come online...")
            time.sleep(1)

    def wait_for_vehicle(self):
        """
        Waits for a valid heartbeat to Mavlink2Rest
        """
        self.report_status("Waiting for vehicle...")
        while not self.mav.get("/HEARTBEAT"):
            time.sleep(1)

    def set_orientation(self, orientation: int) -> bool:
        """
        Sets the DVL orientation, either DVL_FORWARD or DVL_DOWN
        """

        if orientation in [DVL_FORWARD, DVL_DOWN]:
            if orientation == DVL_DOWN:
                angles = "0,0,0"
            elif orientation == DVL_FORWARD:
                angles = "0,90,0"
            message = "SET-SENSOR-ORIENTATION " + angles + "\r\n"
            self.socket.sendto(
                message.encode(), (self.host, self.command_port))
            self.current_orientation = orientation
            self.save_settings()
            return True
        return False

    def set_should_send(self, should_send):
        if not MessageType.contains(should_send):
            raise Exception(f"bad messagetype: {should_send}")
        self.should_send = should_send
        self.save_settings()

    @staticmethod
    def longitude_scale(lat: float):
        """
        from https://github.com/ArduPilot/ardupilot/blob/Sub-4.1/libraries/AP_Common/Location.cpp#L325
        """
        scale = math.cos(math.radians(lat))
        return max(scale, 0.01)

    def lat_lng_to_NE_XY_cm(self, lat: float, lon: float) -> List[float]:
        """
        From https://github.com/ArduPilot/ardupilot/blob/Sub-4.1/libraries/AP_Common/Location.cpp#L206
        """
        x = (lat - self.origin[0]) * LATLON_TO_CM
        y = self.longitude_scale(
            (lat + self.origin[0]) / 2) * LATLON_TO_CM * (lon - self.origin[1])
        return [x, y]

    def has_origin_set(self) -> bool:
        try:
            old_time = self.mav.get_float(
                "/GPS_GLOBAL_ORIGIN/message/time_usec")
            if math.isnan(old_time):
                logger.warning(
                    "Unable to read current time for GPS_GLOBAL_ORIGIN, using 0")
                old_time = 0
        except Exception as e:
            logger.warning(
                f"Unable to read current time for GPS_GLOBAL_ORIGIN, using 0: {e}")
            old_time = 0

        for attempt in range(5):
            logger.debug(f"Trying to read origin, try # {attempt}")
            self.mav.request_message(GPS_GLOBAL_ORIGIN_ID)
            time.sleep(0.1)  # make this a timeout?
            try:
                new_origin_data = json.loads(
                    self.mav.get("/GPS_GLOBAL_ORIGIN/message"))
                if new_origin_data["time_usec"] != old_time:
                    self.origin = [new_origin_data["latitude"]
                                   * 1e-7, new_origin_data["longitude"] * 1e-7]
                    return True
                continue  # try again
            except Exception as e:
                logger.warning(e)
                return False
        return False

    def set_current_position(self, lat: float, lon: float):
        """
        Sets the EKF origin to lat, lon
        """
        # If origin has never been set, set it
        now = time.time()
        if (now < (self.last_gps_timestamp + (self.gps_update_interval))):
            # TODO this is to limit the rate of the GPS updates. Unfortunately this is necessary.
            return
        self.last_gps_timestamp = now
        if not self.has_origin_set():
            logger.info("Origin was never set, trying to set it.")
            self.set_gps_origin(lat, lon)
        else:
            logger.info(
                "Origin has already been set, sending POSITION_ESTIMATE instead")
            # if we already have an origin set, send a new position instead
            x, y = self.lat_lng_to_NE_XY_cm(lat, lon)
            depth = float(self.mav.get("/VFR_HUD/message/alt"))

            attitude = json.loads(self.mav.get("/ATTITUDE/message"))
            attitudes = [attitude["roll"], attitude["pitch"], attitude["yaw"]]
            positions = [x, y, -depth]
            self.reset_counter += 1
            self.mav.send_vision_position_estimate(
                self.last_gps_timestamp, positions, attitudes, reset_counter=self.reset_counter
            )

    def set_gps_origin(self, lat: float, lon: float) -> None:
        """
        Sets the EKF origin to lat, lon
        """
        self.mav.set_gps_origin(lat, lon)
        self.origin = [float(lat), float(lon)]
        self.save_settings()

    def set_enabled(self, enable: bool) -> bool:
        """
        Enables/disables the driver
        """
        self.enabled = enable
        self.save_settings()
        if enable:
            self.resume()
        else:
            self.pause()
        return True

    def set_use_as_rangefinder(self, enable: bool) -> bool:
        """
        Enables/disables DISTANCE_SENSOR messages
        """
        self.rangefinder_enable = enable
        self.save_settings()
        if enable:
            self.mav.set_param(
                "RNGFND1_TYPE", "MAV_PARAM_TYPE_UINT8", 10)  # MAVLINK
        return True

    def set_pool_mode(self, enable: bool) -> bool:
        """
        Enables/disables DISTANCE_SENSOR messages
        """
        self.pool_mode = enable
        # self.save_settings()
        if enable:
            message = POOL_MODE_COMMAND + "\r\n"
            self.socket.sendto(
                message.encode(), (self.host, self.command_port))
        else:
            message = AUTOMATIC_MODE_COMMAND + "\r\n"
            self.socket.sendto(
                message.encode(), (self.host, self.command_port))
        return True

    def setup_mavlink(self) -> None:
        """
        Sets up mavlink streamrates so we have the needed messages at the
        appropriate rates
        """
        self.report_status("Setting up MAVLink streams...")
        self.mav.ensure_message_frequency("ATTITUDE", 30, 30)
        self.mav.ensure_message_frequency("GLOBAL_POSITION_INT", 33, 30)
        self.mav.ensure_message_frequency("LOCAL_POSITION_NED", 32, 30)

    def setup_params(self) -> None:
        """
        Sets up the required params for DVL integration
        """
        # https://ardupilot.org/copter/docs/parameters.html#rngfnd1-parameters

        self.mav.set_param("AHRS_EKF_TYPE", "MAV_PARAM_TYPE_UINT8", 3)
        # TODO: Check if really required. It doesn't look like the ekf2 stops at all
        self.mav.set_param("EK2_ENABLE", "MAV_PARAM_TYPE_UINT8", 0)

        self.mav.set_param("EK3_ENABLE", "MAV_PARAM_TYPE_UINT8", 1)
        self.mav.set_param("VISO_TYPE", "MAV_PARAM_TYPE_UINT8", 1)
        self.mav.set_param("EK3_GPS_TYPE", "MAV_PARAM_TYPE_UINT8", 3)
        self.mav.set_param("GPS_TYPE", "MAV_PARAM_TYPE_UINT8", 1)
        self.mav.set_param(
            "EK3_SRC1_POSXY", "MAV_PARAM_TYPE_UINT8", 6)  # EXTNAV
        self.mav.set_param(
            "EK3_SRC1_VELXY", "MAV_PARAM_TYPE_UINT8", 6)  # EXTNAV
        self.mav.set_param("EK3_SRC1_POSZ", "MAV_PARAM_TYPE_UINT8", 1)  # BARO
        if self.rangefinder_enable:
            self.mav.set_param(
                "RNGFND1_TYPE", "MAV_PARAM_TYPE_UINT8", 10)  # MAVLINK
            self.mav.set_param(
                "RNGFND1_MAX_CM", "MAV_PARAM_TYPE_UINT8", 5000)

    def setup_dvl(self):
        self.set_gps_enabled()
        self.set_dvpdl_enabled()
        self.set_dvext_enabled()
        self.set_retweet_imu_enabled(False)
        self.set_gprmc_enabled(False)
        self.set_orientation(self.current_orientation)

    # TCP
    def setup_connections(self, timeout=300) -> None:
        """
        Sets up the socket to talk to the DVL
        """
        while timeout > 0:
            try:
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.connect((self.host, self.port))
                self.socket.setblocking(0)
                return True
            except socket.error:
                time.sleep(0.1)
            timeout -= 1
        self.report_status(
            f"Setup connection to {self.host}:{self.port} timed out")
        return False

    # UDP
    def setup_connections_udp(self, timeout=300):
        """
        Sets up the socket to talk to the DVL
        """
        while timeout > 0:
            try:
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.setblocking(False)
                self.socket.bind(('0.0.0.0', 27000))
                return True
            except socket.error:
                time.sleep(0.1)
            except Exception as error:
                print(error)
                time.sleep(0.1)
            timeout -= 1
        self.status = "Setup connection timeout"
        return False

    def reconnect(self):
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()
            except Exception as e:
                self.report_status(
                    f"Unable to reconnect: {e}, looking for dvl again...")
                # self.look_for_dvl()
        success = self.setup_connections_udp()
        if success:
            self.last_recv_time = time.time()  # Don't disconnect directly after connect
            return True

        return False

    def handle_velocity(self, data: Dict[str, Any]) -> None:
        # TODO: test if this is used by ArduSub or could be [0, 0, 0]
        # extract velocity data from the DVL JSON

        vx, vy, vz, alt, valid, fom = (
            data["vx"],
            data["vy"],
            data["vz"],
            data["altitude"],
            data["velocity_valid"],
            data["fom"],
        )
        dt = data["time"] / 1000
        dx = dt * vx
        dy = dt * vy
        dz = dt * vz

        # fom is the standard deviation. scaling it to a confidence from 0-100%
        # 0 is a very good measurement, 0.4 is considered a inaccurate measurement
        _fom_max = 0.4
        confidence = 100 * (1 - min(_fom_max, fom) / _fom_max) if valid else 0
        # confidence = 100 if valid else 0

        # feeding back the angles seemed to aggravate the gyro drift issue
        angles = [0, 0, 0]

        if self.rangefinder_enable:
            self.mav.send_rangefinder(alt)

        if not valid:
            logger.info("Invalid  dvl reading, ignoring it.")
            return

        if self.should_send == MessageType.POSITION_DELTA:
            if self.current_orientation == DVL_DOWN:
                self.mav.send_vision(
                    [dx, dy, dz], angles, dt=data["time"] * 1e3, confidence=confidence)
            elif self.current_orientation == DVL_FORWARD:
                self.mav.send_vision(
                    [dz, dy, -dx], angles, dt=data["time"] * 1e3, confidence=confidence)
        elif self.should_send == MessageType.SPEED_ESTIMATE:
            if self.current_orientation == DVL_DOWN:
                self.mav.send_vision_speed_estimate([vx, vy, vz])
            elif self.current_orientation == DVL_FORWARD:
                self.mav.send_vision_speed_estimate([vz, vy, -vx])

    def handle_PDL(self, data):
        dx, dy, dz = data.pdx, data.pdy, data.pdz
        dt = data.dtu
        c = data.c

        angles = [0, 0, 0]

        if self.should_send == MessageType.POSITION_DELTA:
            if self.current_orientation == DVL_DOWN:
                self.mav.send_vision(
                    [dx, dy, dz], angles, dt=dt, confidence=c)
                return True
            elif self.current_orientation == DVL_FORWARD:
                self.mav.send_vision(
                    [dz, dy, -dx], angles, dt=dt, confidence=c)
                return True

    def handle_EXT(self, data):

        self.dvl_lock = data.v
        self.dvl_gps_status = data.g
        self.dvl_calibration = data.cal
        self.dvl_lock_a = data.la
        self.dvl_lock_b = data.lb
        self.dvl_lock_c = data.lc
        self.dvl_lock_d = data.ld
        self.dvl_gain_a = data.ga
        self.dvl_gain_b = data.gb
        self.dvl_gain_c = data.gc
        self.dvl_gain_d = data.gd
        self.dvl_altitude = data.t

        if self.rangefinder_enable and self.dvl_altitude > 0.05:
            self.mav.send_rangefinder(
                self.dvl_altitude, self.current_orientation)

    def handle_configuration(self, cfg):
        # Clear Config
        self.configuration = []
        # Set new config
        for i in cfg.split(","):
            item = i.split("=")
            if len(item) > 1:
                self.configuration.append(item)
                print(item)

    # def handle_position_local(self, data):
    #     # if True:
    #     if self.should_send == MessageType.POSITION_ESTIMATE:
    #         x, y, z, roll, pitch, yaw = data["x"], data["y"], data["z"], data["roll"], data["pitch"], data["yaw"]
    #         self.timestamp = data["ts"]
    #         self.mav.send_vision_position_estimate(
    #             self.timestamp, [x, y, z], [roll, pitch,
    #                                         yaw], reset_counter=self.reset_counter
    #         )

    def is_nmea(self, packet):
        try:
            if pynmea2.parse(packet):
                return True
            else:
                return False
        except Exception as error:
            pass

    def is_gps_passthrough(self, packet):
        # print(packet)
        try:
            if (packet[0:5] == 'GPS:$'):
                return True
            else:
                return False
        except Exception as error:
            pass

    def is_configuration(self, packet):
        # print(packet)
        try:
            if (packet[0:7] == "$DVNVM,"):
                return True
            else:
                return False
        except Exception as error:
            pass

    def set_gps_enabled(self, enable=True):
        setting = "RETWEET-GPS"
        self.set_dvl_setting(setting, enable)

    def set_dvpdl_enabled(self, enable=True):
        setting = "SEND-DVPDL"
        self.set_dvl_setting(setting, enable)

    def set_dvext_enabled(self, enable=True):
        setting = "SEND-DVEXT"
        self.set_dvl_setting(setting, enable)

    def set_retweet_imu_enabled(self, enable=True):
        setting = "RETWEET-IMU"
        self.set_dvl_setting(setting, enable)

    def set_gprmc_enabled(self, enable=True):
        setting = "SEND-GPRMC"
        self.set_dvl_setting(setting, enable)

    def set_dvl_setting(self, setting, enable=True):
        command = ""
        if enable:
            command = "ON"
        else:
            command = "OFF"
        message = setting + " " + command + "\r\n"
        self.socket.sendto(message.encode(), (self.host, self.command_port))
        time.sleep(0.1)

    def get_configuration(self):
        message = "?" + "\r\n"
        self.socket.sendto(message.encode(), (self.host, self.command_port))

    def resume(self):
        message = "RESUME" + "\r\n"
        self.socket.sendto(message.encode(), (self.host, self.command_port))

    def pause(self):
        message = "PAUSE" + "\r\n"
        self.socket.sendto(message.encode(), (self.host, self.command_port))

    def reboot(self):
        message = "REBOOT" + "\r\n"
        self.socket.sendto(message.encode(), (self.host, self.command_port))

    def run(self):
        """
        Runs the main routing
        """
        self.load_settings()
        self.save_settings()
        # self.look_for_dvl()
        self.setup_connections_udp()
        self.wait_for_vehicle()
        self.setup_mavlink()
        self.setup_params()
        self.setup_dvl()
        time.sleep(1)
        self.last_recv_time = time.time()
        buf = ""
        connected = True
        if (self.enabled):
            self.resume()
            self.get_configuration()
        else:
            self.pause()
        self.report_status("Running")

        while True:
            if not self.enabled:
                time.sleep(1)
                buf = ""  # Reset buf when disabled
                continue
            r, _, _ = select([self.socket], [], [], 0)
            data = None
            line = None
            if r:
                try:
                    recv = self.socket.recv(1024).decode()
                    connected = True
                    if recv:
                        self.last_recv_time = time.time()
                        buf += recv
                except socket.error as e:
                    logger.warning(f"Disconnected: {e}")
                    connected = False
                except Exception as e:
                    logger.warning(f"Error receiving: {e}")

            # Extract 1 complete line from the buffer if available
            if len(buf) > 0:
                lines = buf.split("\n", 1)
                if len(lines) > 1:
                    buf = lines[1]
                    line = lines[0]
            if self.is_nmea(line):
                data = pynmea2.parse(line)
                # print(repr(data))
                if data.sentence_type == 'PDL':
                    self.handle_PDL(data)
                elif data.sentence_type == 'EXT':
                    self.handle_EXT(data)
                # else:
                # print(data)
            elif (self.is_gps_passthrough(line)):
                try:
                    data = pynmea2.parse(line[4:])
                    if data.latitude and data.longitude:
                        if data.gps_qual > 0 and float(data.horizontal_dil) < 1.8 and float(data.num_sats) > 5:
                            # print("HDOP: ", str(
                            #     float(data.horizontal_dil)))
                            # print("Fix: ", str(data.gps_qual))
                            self.set_current_position(
                                data.latitude, data.longitude)
                except Exception as error:
                    continue
            elif self.is_configuration(line):
                self.handle_configuration(line)
            elif line != None:
                print(line)

            if not connected:
                buf = ""
                self.report_status("restarting")
                self.reconnect()
                time.sleep(0.003)
                continue

            if not line:
                if time.time() - self.last_recv_time > self.timeout:
                    buf = ""
                    self.report_status("timeout, restarting")
                    connected = self.reconnect()
                time.sleep(0.003)
                continue

            self.status = "Running"

            time.sleep(0.003)
        logger.error("Driver Quit! This should not happen.")
