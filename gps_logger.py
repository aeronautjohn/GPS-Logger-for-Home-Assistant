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

class GPSLogger(hass.Hass):

    def initialize(self):
        self.gps_lat_sensor = self.args["gps_lat_sensor"]
        self.gps_lon_sensor = self.args["gps_lon_sensor"]
        self.db_host = self.args["db_host"]
        self.db_user = self.args["db_user"]
        self.db_password = self.args["db_password"]
        self.db_name = self.args["db_name"]
        self.log_distance = self.args["log_distance"]
        self.route_distance = self.args["route_distance"]
        self.min_distance_threshold = 10  # Minimum distance threshold to log new points
        self.final_distance_threshold = 100  # Distance threshold to avoid perpetual routing
        self.temp_log_file = "/homeassistant/temp_gps_log.json"  # Temporary file for logging points

        # Setup database connection pool
        self.db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="gps_pool",
            pool_size=5,
            host=self.db_host,
            user=self.db_user,
            password=self.db_password,
            database=self.db_name
        )

        # Fetch last coordinates on startup
        self.previous_latitude, self.previous_longitude = self.get_last_coordinates()

        if self.previous_latitude is not None and self.previous_longitude is not None:
            self.log(f"Initialized previous coordinates to ({self.previous_latitude}, {self.previous_longitude})")
        else:
            self.log("No previous coordinates found on startup")

        # Setup the route queue and background thread
        self.route_queue = queue.Queue()
        self.background_thread = Thread(target=self.process_queue)
        self.background_thread.daemon = True
        self.background_thread.start()

        # Listen for changes in GPS sensors
        self.listen_state(self.check_movement, self.gps_lat_sensor)
        self.listen_state(self.check_movement, self.gps_lon_sensor)

        # Periodically check for new points added via SQL
        self.run_every(self.check_for_new_points, "now", 60)  # Check every 60 seconds

        # Process any points in the temp log file on startup
        self.process_temp_log()

    def process_temp_log(self):
        if os.path.exists(self.temp_log_file):
            with open(self.temp_log_file, "r") as f:
                temp_data = json.load(f)
            for entry in temp_data:
                self.add_coordinates_to_db(entry["coordinates"], entry["timestamp"])
            os.remove(self.temp_log_file)
            self.log(f"Processed and removed temporary log file: {self.temp_log_file}")

    def check_movement(self, entity, attribute, old, new, kwargs):
        if old == new:
            return

        current_latitude = float(self.get_state(self.gps_lat_sensor))
        current_longitude = float(self.get_state(self.gps_lon_sensor))

        if self.previous_latitude is None or self.previous_longitude is None:
            self.previous_latitude, self.previous_longitude = self.get_last_coordinates()

        if self.previous_latitude is not None and self.previous_longitude is not None:
            distance = self.haversine(current_latitude, current_longitude, self.previous_latitude, self.previous_longitude)
            self.log(f"Calculated distance: {distance} meters from ({self.previous_latitude}, {self.previous_longitude}) to ({current_latitude}, {current_longitude})")

            if distance > self.route_distance:
                self.log(f"Distance greater than {self.route_distance} meters, adding route task to queue.")
                timestamp = datetime.utcnow()
                self.route_queue.put((self.previous_latitude, self.previous_longitude, current_latitude, current_longitude, timestamp))
                # Update previous coordinates immediately after adding the route task
                self.previous_latitude, self.previous_longitude = current_latitude, current_longitude
            elif distance > self.log_distance and distance > self.min_distance_threshold:
                self.log(f"Distance greater than {self.log_distance} meters and above threshold, updating coordinates without routing.")
                self.add_coordinates_to_db([(current_latitude, current_longitude)])
                # Update previous coordinates immediately after adding the new coordinates
                self.previous_latitude, self.previous_longitude = current_latitude, current_longitude
            else:
                self.log(f"Distance less than {self.log_distance} meters or below threshold, no update.")
        else:
            self.log("No previous coordinates found, inserting current coordinates.")
            self.add_coordinates_to_db([(current_latitude, current_longitude)])
            # Update previous coordinates immediately after adding the new coordinates
            self.previous_latitude, self.previous_longitude = current_latitude, current_longitude

    def check_for_new_points(self, kwargs):
        new_latitude, new_longitude, timestamp = self.get_new_coordinates()

        if new_latitude is not None and new_longitude is not None:
            if self.previous_latitude is not None and self.previous_longitude is not None:
                distance = self.haversine(new_latitude, new_longitude, self.previous_latitude, self.previous_longitude)
                if distance > self.route_distance:
                    self.log(f"New far-away point detected at ({new_latitude}, {new_longitude}), queuing route task.")
                    self.route_queue.put((self.previous_latitude, self.previous_longitude, new_latitude, new_longitude, timestamp))
                    self.delete_coordinate(new_latitude, new_longitude, timestamp)

    def get_new_coordinates(self):
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT latitude, longitude, timestamp FROM gps_data ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row and (row[0] != self.previous_latitude or row[1] != self.previous_longitude):
                return row[0], row[1], row[2]
            else:
                return None, None, None
        except Exception as e:
            self.log(f"Error fetching new coordinates: {e}", level="ERROR")
            return None, None, None

    def delete_coordinate(self, lat, lon, timestamp):
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM gps_data WHERE latitude = %s AND longitude = %s AND timestamp = %s", (lat, lon, timestamp))
            conn.commit()
            cursor.close()
            conn.close()
            self.log(f"Deleted coordinate ({lat}, {lon}) with timestamp {timestamp}")
        except Exception as e:
            self.log(f"Error deleting coordinate: {e}", level="ERROR")

    def haversine(self, lat1, lon1, lat2, lon2):
        R = 6371000  # Radius of the Earth in meters
        phi1 = radians(lat1)
        phi2 = radians(lat2)
        delta_phi = radians(lat2 - lat1)
        delta_lambda = radians(lon2 - lon1)

        a = sin(delta_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2.0) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))

        return R * c

    def get_last_coordinates(self):
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT latitude, longitude FROM gps_data ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row:
                return row[0], row[1]
            else:
                return None, None
        except Exception as e:
            self.log(f"Error fetching last coordinates: {e}", level="ERROR")
            return None, None
    def add_coordinates_to_db(self, coordinates, timestamp=None):
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            for coordinate in coordinates:
                if timestamp is None:
                    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                lat, lon = coordinate if len(coordinate) == 2 else coordinate[:2]
                location = f"POINT({lon} {lat})"
                cursor.execute("INSERT INTO gps_data (latitude, longitude, timestamp, location) VALUES (%s, %s, %s, ST_GeomFromText(%s))", (lat, lon, timestamp, location))
            conn.commit()
            cursor.close()
            conn.close()
            self.log(f"Inserted {len(coordinates)} coordinates at {timestamp}")

            # Update the previous coordinates with the most recent logged point
            if coordinates:
                self.previous_latitude = coordinates[-1][0]
                self.previous_longitude = coordinates[-1][1]
                self.log(f"Updated previous coordinates to ({self.previous_latitude}, {self.previous_longitude})")
        except Exception as e:
            self.log(f"Error inserting coordinates: {e}", level="ERROR")

    async def fetch_route(self, start_lat, start_lon, end_lat, end_lon):
        try:
            url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json()
                    route = data['routes'][0]['geometry']['coordinates']
                    return [(lat, lon) for lon, lat in route]
        except Exception as e:
            self.log(f"Error fetching route: {e}", level="ERROR")
            return None

    def process_queue(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            task = self.route_queue.get()
            if task is None:
                break
            start_lat, start_lon, end_lat, end_lon, original_timestamp = task
            distance = self.haversine(start_lat, start_lon, end_lat, end_lon)
            if distance < self.final_distance_threshold:
                self.log(f"Final distance {distance} meters is below the threshold {self.final_distance_threshold}, logging end point only.")
                self.add_coordinates_to_db([(end_lat, end_lon)], original_timestamp.strftime('%Y-%m-%d %H:%M:%S'))
                self.previous_latitude = end_lat
                self.previous_longitude = end_lon
            else:
                route_there = loop.run_until_complete(self.fetch_route(start_lat, start_lon, end_lat, end_lon))
                route_back = loop.run_until_complete(self.fetch_route(end_lat, end_lon, start_lat, start_lon))
                if route_there and route_back:
                    self.log("Logging route points there and back.")
                    intermediate_points = []
                    num_points_there = len(route_there)
                    num_points_back = len(route_back)
                    for i, point in enumerate(route_there):
                        lat, lon = point
                        # Adjust the timestamp for each intermediate point there
                        point_timestamp = (original_timestamp + timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S')
                        intermediate_points.append((lat, lon, point_timestamp))
                    for i, point in enumerate(route_back):
                        lat, lon = point
                        # Adjust the timestamp for each intermediate point back
                        point_timestamp = (original_timestamp + timedelta(seconds=(num_points_there + i))).strftime('%Y-%m-%d %H:%M:%S')
                        intermediate_points.append((lat, lon, point_timestamp))
                    # Add the final point as the current location
                    final_timestamp = (original_timestamp + timedelta(seconds=(num_points_there + num_points_back))).strftime('%Y-%m-%d %H:%M:%S')
                    intermediate_points.append((start_lat, start_lon, final_timestamp))
                    self.add_coordinates_to_db(intermediate_points)
                else:
                    self.log("Failed to get route, logging only end point.")
                    self.add_coordinates_to_db([(end_lat, end_lon)], original_timestamp.strftime('%Y-%m-%d %H:%M:%S'))
            self.route_queue.task_done()
