# Import necessary libraries
import appdaemon.plugins.hass.hassapi as hass
import mysql.connector
from mysql.connector import pooling
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, atan2
import aiohttp
import asyncio
import queue
from threading import Thread
import json
import os
import logging
import time

class GPSLogger(hass.Hass):
    """
    A class to log GPS coordinates and generate routes between distant points.
    This class integrates with Home Assistant and uses a MySQL database for storage.
    """

    def initialize(self):
        """
        Initialize the GPSLogger. This method is called by AppDaemon when the app starts.
        It sets up the configuration, database connection, and starts necessary processes.
        """
        self._setup_config()
        self._setup_database()
        self._setup_previous_coordinates()
        self._setup_queue_and_thread()
        self._setup_listeners()

        # Schedule a periodic check for new GPS points
        self.run_every(self.check_for_new_points, "now", 60)  # Check every 60 seconds

        # Process any points in the temp log file on startup
        self.process_temp_log()

    def _setup_config(self):
        """
        Set up configuration variables from environment variables or AppDaemon args.
        """
        self.gps_lat_sensor = self.args["gps_lat_sensor"]
        self.gps_lon_sensor = self.args["gps_lon_sensor"]
        self.gps_speed_sensor = self.args["gps_speed_sensor"]  # New sensor for speed
        self.db_host = os.getenv("DB_HOST", self.args["db_host"])
        self.db_user = os.getenv("DB_USER", self.args["db_user"])
        self.db_password = os.getenv("DB_PASSWORD", self.args["db_password"])
        self.db_name = self.args["db_name"]
        self.speed_threshold = self.args["speed_threshold"]  # Replaces log_distance
        self.route_distance = self.args["route_distance"]
        self.min_distance_threshold = 10  # Minimum distance threshold to log new points
        self.final_distance_threshold = 100  # Distance threshold to avoid perpetual routing
        self.temp_log_file = "/homeassistant/temp_gps_log.json"  # Temporary file for logging points
        
        # New variables for speed-based logging
        self.is_logging = False
        self.high_speed_start_time = None
        self.low_speed_start_time = None
        self.speed_check_duration = 5  # 5 seconds

    def _setup_database(self):
        """
        Set up a connection pool to the MySQL database.
        """
        self.db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="gps_pool",
            pool_size=5,
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            database=self.db_name
        )

    def _setup_previous_coordinates(self):
        """
        Retrieve the last known coordinates from the database.
        """
        self.previous_latitude, self.previous_longitude = self.get_last_coordinates()

    def _setup_queue_and_thread(self):
        """
        Set up a queue for route processing and start a background thread.
        """
        self.route_queue = queue.Queue()
        self.background_thread = Thread(target=self.process_queue)
        self.background_thread.daemon = True
        self.background_thread.start()

    def _setup_listeners(self):
        """
        Set up state listeners for GPS sensors in Home Assistant.
        """
        self.listen_state(self.check_speed, self.gps_speed_sensor)
        self.listen_state(self.log_position, self.gps_lat_sensor)
        self.listen_state(self.log_position, self.gps_lon_sensor)

    def process_temp_log(self):
        """
        Process any GPS points stored in the temporary log file.
        This is useful for recovering data after a restart.
        """
        if os.path.exists(self.temp_log_file):
            with open(self.temp_log_file, "r") as f:
                temp_data = json.load(f)
            for entry in temp_data:
                self.add_coordinates_to_db(entry["coordinates"], entry["timestamp"])
            os.remove(self.temp_log_file)

    def check_speed(self, entity, attribute, old, new, kwargs):
        """
        Check the current speed and determine if logging should start or stop.
        """
        current_time = time.time()
        current_speed = float(new)
        
        if current_speed > self.speed_threshold:
            if self.high_speed_start_time is None:
                self.high_speed_start_time = current_time
            self.low_speed_start_time = None
            
            if not self.is_logging and (current_time - self.high_speed_start_time >= self.speed_check_duration):
                self.is_logging = True
                self.log("Started logging due to sustained speed increase", level="INFO")
        else:
            if self.low_speed_start_time is None:
                self.low_speed_start_time = current_time
            self.high_speed_start_time = None
            
            if self.is_logging and (current_time - self.low_speed_start_time >= self.speed_check_duration):
                self.is_logging = False
                self.log("Stopped logging due to sustained speed decrease", level="INFO")

    def log_position(self, entity, attribute, old, new, kwargs):
        """
        Log the current position if logging is active.
        """
        if self.is_logging:
            current_latitude = float(self.get_state(self.gps_lat_sensor))
            current_longitude = float(self.get_state(self.gps_lon_sensor))
            
            if self.previous_latitude is None or self.previous_longitude is None:
                self.previous_latitude, self.previous_longitude = self.get_last_coordinates()

            if self.previous_latitude is not None and self.previous_longitude is not None:
                distance = self.haversine(current_latitude, current_longitude, self.previous_latitude, self.previous_longitude)

                if distance > self.route_distance:
                    # If the distance is large, queue a route calculation
                    timestamp = datetime.utcnow()
                    self.route_queue.put((self.previous_latitude, self.previous_longitude, current_latitude, current_longitude, timestamp))
                    self.previous_latitude, self.previous_longitude = current_latitude, current_longitude
                elif distance > self.min_distance_threshold:
                    # If the distance is significant, log the new point
                    self.add_coordinates_to_db([(current_latitude, current_longitude)])
                    self.previous_latitude, self.previous_longitude = current_latitude, current_longitude
            else:
                # If we don't have previous coordinates, log the current position
                self.add_coordinates_to_db([(current_latitude, current_longitude)])
                self.previous_latitude, self.previous_longitude = current_latitude, current_longitude

    def check_for_new_points(self, kwargs):
        """
        Periodically check for new GPS points in the database.
        This helps catch any points that might have been missed by the state listeners.
        """
        new_latitude, new_longitude, timestamp = self.get_new_coordinates()

        if new_latitude is not None and new_longitude is not None:
            if self.previous_latitude is not None and self.previous_longitude is not None:
                distance = self.haversine(new_latitude, new_longitude, self.previous_latitude, self.previous_longitude)
                if distance > self.route_distance:
                    # If the distance is large, queue a route calculation
                    self.route_queue.put((self.previous_latitude, self.previous_longitude, new_latitude, new_longitude, timestamp))
                    self.delete_coordinate(new_latitude, new_longitude, timestamp)

    def get_new_coordinates(self):
        """
        Retrieve the most recent GPS coordinates from the database.
        Returns: (latitude, longitude, timestamp) or (None, None, None) if no new coordinates are found.
        """
        try:
            with self.db_pool.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT latitude, longitude, timestamp FROM gps_data ORDER BY timestamp DESC LIMIT 1")
                    row = cursor.fetchone()

                    if row and (row[0] != self.previous_latitude or row[1] != self.previous_longitude):
                        return row[0], row[1], row[2]
                    else:
                        return None, None, None
        except mysql.connector.Error as db_err:
            self.log(f"Database error fetching new coordinates: {db_err}", level="ERROR")
        except Exception as e:
            self.log(f"Unexpected error fetching new coordinates: {e}", level="ERROR")
        return None, None, None

    def delete_coordinate(self, lat, lon, timestamp):
        """
        Delete a specific coordinate from the database.
        This is used to remove points that will be replaced by a calculated route.
        """
        try:
            with self.db_pool.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM gps_data WHERE latitude = %s AND longitude = %s AND timestamp = %s", (lat, lon, timestamp))
                    conn.commit()
        except mysql.connector.Error as db_err:
            self.log(f"Database error deleting coordinate: {db_err}", level="ERROR")
        except Exception as e:
            self.log(f"Unexpected error deleting coordinate: {e}", level="ERROR")

    def haversine(self, lat1, lon1, lat2, lon2):
        """
        Calculate the great circle distance between two points on the earth.
        """
        R = 6371000  # Radius of the Earth in meters
        phi1 = radians(lat1)
        phi2 = radians(lat2)
        delta_phi = radians(lat2 - lat1)
        delta_lambda = radians(lon2 - lon1)

        a = sin(delta_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2.0) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))

        return R * c

    def get_last_coordinates(self):
        """
        Retrieve the last known GPS coordinates from the database.
        Returns: (latitude, longitude) or (None, None) if no coordinates are found.
        """
        try:
            with self.db_pool.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT latitude, longitude FROM gps_data ORDER BY timestamp DESC LIMIT 1")
                    row = cursor.fetchone()

                    if row:
                        return row[0], row[1]
                    else:
                        return None, None
        except mysql.connector.Error as db_err:
            self.log(f"Database error fetching last coordinates: {db_err}", level="ERROR")
        except Exception as e:
            self.log(f"Unexpected error fetching last coordinates: {e}", level="ERROR")
        return None, None

    def add_coordinates_to_db(self, coordinates, timestamp=None):
        """
        Add GPS coordinates to the database.
        """
        try:
            with self.db_pool.get_connection() as conn:
                with conn.cursor() as cursor:
                    for coordinate in coordinates:
                        if timestamp is None:
                            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                        lat, lon = coordinate if len(coordinate) == 2 else coordinate[:2]
                        location = f"POINT({lon} {lat})"
                        cursor.execute("INSERT INTO gps_data (latitude, longitude, timestamp, location) VALUES (%s, %s, %s, ST_GeomFromText(%s))", (lat, lon, timestamp, location))
                    conn.commit()

            if coordinates:
                self.previous_latitude = coordinates[-1][0]
                self.previous_longitude = coordinates[-1][1]
        except mysql.connector.Error as db_err:
            self.log(f"Database error inserting coordinates: {db_err}", level="ERROR")
        except Exception as e:
            self.log(f"Unexpected error inserting coordinates: {e}", level="ERROR")

    async def fetch_route(self, start_lat, start_lon, end_lat, end_lon):
        """
        Fetch a route between two points using the OSRM API.
        """
        try:
            url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json()
                    route = data['routes'][0]['geometry']['coordinates']
                    return [(lat, lon) for lon, lat in route]
        except aiohttp.ClientError as aio_err:
            self.log(f"HTTP error fetching route: {aio_err}", level="ERROR")
        except Exception as e:
            self.log(f"Unexpected error fetching route: {e}", level="ERROR")
        return [(start_lat, start_lon), (end_lat, end_lon)]  # Return a simple start-end point route

    def process_queue(self):
        """
        Process the queue of routes to be calculated.
        This method runs in a separate thread to avoid blocking the main thread.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            task = self.route_queue.get()
            if task is None:
                break
            start_lat, start_lon, end_lat, end_lon, original_timestamp = task
            distance = self.haversine(start_lat, start_lon, end_lat, end_lon)
            if distance < self.final_distance_threshold:
                # If the distance is small, just log the end point
                self.add_coordinates_to_db([(end_lat, end_lon)], original_timestamp.strftime('%Y-%m-%d %H:%M:%S'))
                self.previous_latitude = end_lat
                self.previous_longitude = end_lon
            else:
                # If the distance is large, calculate a route
                route_there = loop.run_until_complete(self.fetch_route(start_lat, start_lon, end_lat, end_lon))
                route_back = loop.run_until_complete(self.fetch_route(end_lat, end_lon, start_lat, start_lon))
                if route_there and route_back:
                    # Generate intermediate points along the route
                    intermediate_points = []
                    num_points_there = len(route_there)
                    num_points_back = len(route_back)
                    for i, point in enumerate(route_there):
                        lat, lon = point
                        point_timestamp = (original_timestamp + timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S')
                        intermediate_points.append((lat, lon, point_timestamp))
                    for i, point in enumerate(route_back):
                        lat, lon = point
                        point_timestamp = (original_timestamp + timedelta(seconds=(num_points_there + i))).strftime('%Y-%m-%d %H:%M:%S')
                        intermediate_points.append((lat, lon, point_timestamp))
                    final_timestamp = (original_timestamp + timedelta(seconds=(num_points_there + num_points_back))).strftime('%Y-%m-%d %H:%M:%S')
                    intermediate_points.append((start_lat, start_lon, final_timestamp))
                    self.add_coordinates_to_db(intermediate_points)
                else:
                    # If route calculation fails, just log the end point
                    self.add_coordinates_to_db([(end_lat, end_lon)], original_timestamp.strftime('%Y-%m-%d %H:%M:%S'))
            self.route_queue.task_done()
